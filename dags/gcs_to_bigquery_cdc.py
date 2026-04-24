from __future__ import annotations

from datetime import datetime
from typing import List, Dict

from airflow.decorators import dag, task
from airflow.models import Variable
from airflow.operators.python import ShortCircuitOperator, PythonOperator
from airflow.providers.google.cloud.hooks.gcs import GCSHook
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook
from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator
from airflow.providers.google.cloud.transfers.gcs_to_bigquery import GCSToBigQueryOperator
import pendulum

from google.cloud import bigquery


GCP_CONN_ID = "google_cloud_default"

PROJECT_ID = Variable.get("GCP_PROJECT_ID")
GCS_BUCKET = Variable.get("CDC_GCS_BUCKET")
BQ_DATASET = Variable.get("CDC_BQ_DATASET")
GCS_ROOT_PREFIX = Variable.get("CDC_GCS_ROOT_PREFIX")
BQ_LOCATION = Variable.get("GCP_LOCATION")

MANIFEST_TABLE = "_cdc_load_manifest"


CDC_TABLES: List[Dict[str, str]] = [
    {
        "name": "pg_drivers",
        "topic": "cdc.public.drivers",
        "gcs_prefix": f"{GCS_ROOT_PREFIX}/cdc.public.drivers/",
        "bq_table": "raw_pg_drivers",
    },
    {
        "name": "pg_passengers",
        "topic": "cdc.public.passengers",
        "gcs_prefix": f"{GCS_ROOT_PREFIX}/cdc.public.passengers/",
        "bq_table": "raw_pg_passengers",
    },
    {
        "name": "pg_rides",
        "topic": "cdc.public.rides",
        "gcs_prefix": f"{GCS_ROOT_PREFIX}/cdc.public.rides/",
        "bq_table": "raw_pg_rides",
    },
    {
        "name": "pg_vehicle_types",
        "topic": "cdc.public.vehicle_types",
        "gcs_prefix": f"{GCS_ROOT_PREFIX}/cdc.public.vehicle_types/",
        "bq_table": "raw_pg_vehicle_types",
    },
    {
        "name": "pg_zones",
        "topic": "cdc.public.zones",
        "gcs_prefix": f"{GCS_ROOT_PREFIX}/cdc.public.zones/",
        "bq_table": "raw_pg_zones",
    },
    {
        "name": "mg_ride_events",
        "topic": "cdc.ride_ops_mg.ride_events",
        "gcs_prefix": f"{GCS_ROOT_PREFIX}/cdc.ride_ops_mg.ride_events/",
        "bq_table": "raw_mg_ride_events",
    },
    {
        "name": "mg_driver_location_stream",
        "topic": "cdc.ride_ops_mg.driver_location_stream",
        "gcs_prefix": f"{GCS_ROOT_PREFIX}/cdc.ride_ops_mg.driver_location_stream/",
        "bq_table": "raw_mg_driver_location_stream",
    },
]


def _has_new_files(list_task_id: str, **context) -> bool:
    files = context["ti"].xcom_pull(task_ids=list_task_id)
    return bool(files)


def _mark_files_loaded(
    list_task_id: str,
    topic: str,
    destination_table: str,
    **context,
) -> None:
    ti = context["ti"]
    files = ti.xcom_pull(task_ids=list_task_id) or []

    if not files:
        return

    hook = BigQueryHook(gcp_conn_id=GCP_CONN_ID, location=BQ_LOCATION)
    client = hook.get_client(project_id=PROJECT_ID, location=BQ_LOCATION)

    table_id = f"{PROJECT_ID}.{BQ_DATASET}.{MANIFEST_TABLE}"

    rows = [
        {
            "topic": topic,
            "object_name": object_name,
            "destination_table": destination_table,
            "loaded_at": datetime.now().isoformat(),
            "dag_run_id": context["dag_run"].run_id,
        }
        for object_name in files
    ]

    errors = client.insert_rows_json(table_id, rows)

    if errors:
        raise RuntimeError(f"Failed to insert manifest rows: {errors}")


@dag(
    dag_id="cdc_gcs_to_bigquery_10min",
    description="Incremental CDC load from GCS Parquet files to BigQuery every 10 minutes",
    start_date=pendulum.datetime(2026, 4, 24, tz="Asia/Jakarta"),
    schedule="*/10 * * * *",
    catchup=False,
    max_active_runs=1,
    render_template_as_native_obj=True,
    tags=["cdc", "gcs", "bigquery", "taxi-pipeline"],
)
def cdc_gcs_to_bigquery_10min():

    ensure_manifest_table = BigQueryInsertJobOperator(
        task_id="ensure_manifest_table",
        gcp_conn_id=GCP_CONN_ID,
        location=BQ_LOCATION,
        configuration={
            "query": {
                "query": f"""
                CREATE TABLE IF NOT EXISTS `{PROJECT_ID}.{BQ_DATASET}.{MANIFEST_TABLE}` (
                    topic STRING,
                    object_name STRING,
                    destination_table STRING,
                    loaded_at TIMESTAMP,
                    dag_run_id STRING
                )
                PARTITION BY DATE(loaded_at)
                CLUSTER BY topic, destination_table
                """,
                "useLegacySql": False,
            }
        },
    )

    @task
    def list_new_gcs_files(topic: str, gcs_prefix: str) -> List[str]:
        gcs_hook = GCSHook(gcp_conn_id=GCP_CONN_ID)

        all_objects = gcs_hook.list(
            bucket_name=GCS_BUCKET,
            prefix=gcs_prefix,
        ) or []

        parquet_objects = sorted(
            object_name
            for object_name in all_objects
            if object_name.endswith(".parquet")
        )

        if not parquet_objects:
            return []

        bq_hook = BigQueryHook(gcp_conn_id=GCP_CONN_ID, location=BQ_LOCATION)
        client = bq_hook.get_client(project_id=PROJECT_ID, location=BQ_LOCATION)

        query = f"""
        SELECT object_name
        FROM `{PROJECT_ID}.{BQ_DATASET}.{MANIFEST_TABLE}`
        WHERE topic = @topic
          AND object_name IN UNNEST(@object_names)
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("topic", "STRING", topic),
                bigquery.ArrayQueryParameter(
                    "object_names",
                    "STRING",
                    parquet_objects,
                ),
            ]
        )

        result = client.query(query, job_config=job_config).result()
        loaded_objects = {row.object_name for row in result}

        new_objects = [
            object_name
            for object_name in parquet_objects
            if object_name not in loaded_objects
        ]

        return new_objects

    for table_cfg in CDC_TABLES:
        safe_name = table_cfg["name"]

        list_task = list_new_gcs_files.override(
            task_id=f"list_new_files_{safe_name}"
        )(
            topic=table_cfg["topic"],
            gcs_prefix=table_cfg["gcs_prefix"],
        )

        check_task = ShortCircuitOperator(
            task_id=f"has_new_files_{safe_name}",
            python_callable=_has_new_files,
            op_kwargs={
                "list_task_id": f"list_new_files_{safe_name}",
            },
        )

        load_task = GCSToBigQueryOperator(
            task_id=f"load_{safe_name}_to_bigquery",
            gcp_conn_id=GCP_CONN_ID,
            bucket=GCS_BUCKET,
            source_objects=(
                "{{ ti.xcom_pull(task_ids='list_new_files_"
                + safe_name
                + "') }}"
            ),
            destination_project_dataset_table=(
                f"{PROJECT_ID}.{BQ_DATASET}.{table_cfg['bq_table']}"
            ),
            source_format="PARQUET",
            autodetect=True,
            create_disposition="CREATE_IF_NEEDED",
            write_disposition="WRITE_APPEND",
            schema_update_options=[
                "ALLOW_FIELD_ADDITION",
                "ALLOW_FIELD_RELAXATION",
            ],
        )

        mark_loaded_task = PythonOperator(
            task_id=f"mark_loaded_{safe_name}",
            python_callable=_mark_files_loaded,
            op_kwargs={
                "list_task_id": f"list_new_files_{safe_name}",
                "topic": table_cfg["topic"],
                "destination_table": table_cfg["bq_table"],
            },
        )

        ensure_manifest_table >> list_task >> check_task >> load_task >> mark_loaded_task


cdc_gcs_to_bigquery_10min()