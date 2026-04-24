from __future__ import annotations

import os

import pendulum
from airflow.exceptions import AirflowSkipException
from airflow.sdk import dag, task
from airflow.providers.google.cloud.operators.gcs import GCSListObjectsOperator
from airflow.providers.google.cloud.transfers.gcs_to_bigquery import GCSToBigQueryOperator

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
# This DAG is aligned with the Kafka Connect GCS sink in this repository:
#   topics.dir  = raw/cdc
#   path.format = topic=<topic>/year=YYYY/month=MM/day=dd/hour=HH
#   format      = Parquet
#
# The DAG loads one hour of files per topic into BigQuery raw tables.
# If no files exist for a topic in the selected hour, that branch is skipped.
# -----------------------------------------------------------------------------

DAG_ID = "gcs_to_bigquery_cdc_hourly"
TZ = os.getenv("AIRFLOW_TIMEZONE", "Asia/Jakarta")
GCS_BUCKET = os.getenv("GCS_BUCKET_NAME", "dbt-taxi-explore-bucket")
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "dbt-taxi-explore")
BQ_DATASET = os.getenv("BQ_DATASET_RAW", "raw_cdc")
GCP_CONN_ID = os.getenv("AIRFLOW_GCP_CONN_ID", "google_cloud_default")
BQ_LOCATION = os.getenv("BQ_LOCATION", "us")
SCHEDULE = os.getenv("AIRFLOW_GCS_TO_BQ_SCHEDULE", "0 * * * *")

TOPIC_TABLE_MAPPINGS = [
    {"topic": "cdc.public.drivers", "table": "public_drivers"},
    {"topic": "cdc.public.passengers", "table": "public_passengers"},
    {"topic": "cdc.public.rides", "table": "public_rides"},
    {"topic": "cdc.public.vehicle_types", "table": "public_vehicle_types"},
    {"topic": "cdc.public.zones", "table": "public_zones"},
    {"topic": "cdc.ride_ops_mg.ride_events", "table": "mongo_ride_events"},
    {"topic": "cdc.ride_ops_mg.driver_location_stream", "table": "mongo_driver_location_stream"},
]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def build_hourly_prefix(topic: str) -> str:
    """Build GCS prefix that matches the Kafka Connect sink folder layout."""
    return f"raw/cdc/{topic}/"
    # return (
    #     f"raw/cdc/topic={topic}/"
    #     "year={{ data_interval_start.in_timezone('Asia/Jakarta').strftime('%Y') }}/"
    #     "month={{ data_interval_start.in_timezone('Asia/Jakarta').strftime('%m') }}/"
    #     "day={{ data_interval_start.in_timezone('Asia/Jakarta').strftime('%d') }}/"
    #     "hour={{ data_interval_start.in_timezone('Asia/Jakarta').strftime('%H') }}/"
    # )


@task
def ensure_files_exist(objects: list[str] | None, topic: str) -> list[str]:
    """Skip downstream load task if the hourly prefix does not contain files."""
    if not objects:
        raise AirflowSkipException(f"No parquet files found in GCS for topic={topic}")
    return objects


@dag(
    dag_id=DAG_ID,
    schedule=SCHEDULE,
    start_date=pendulum.datetime(2026, 4, 13, tz=TZ),
    catchup=True,
    max_active_runs=1,
    default_args={"retries": 1},
    tags=["cdc", "gcs", "bigquery", "raw"],
    doc_md="""
    ### GCS to BigQuery raw CDC loader

    DAG ini membaca file Parquet hasil Kafka Connect GCS Sink pada prefix per jam,
    lalu memuatnya ke BigQuery menggunakan `GCSToBigQueryOperator`.

    Asumsi folder GCS:
    `raw/cdc/topic=<topic>/year=YYYY/month=MM/day=dd/hour=HH/...parquet`
    """,
)
def gcs_to_bigquery_cdc_hourly():
    for cfg in TOPIC_TABLE_MAPPINGS:
        topic = cfg["topic"]
        table = cfg["table"]
        prefix = build_hourly_prefix(topic)

        list_files = GCSListObjectsOperator(
            task_id=f"list_{table}_files",
            bucket=GCS_BUCKET,
            prefix=prefix,
            gcp_conn_id=GCP_CONN_ID,
        )

        validated_files = ensure_files_exist.override(task_id=f"ensure_{table}_files_exist")(
            objects=list_files.output,
            topic=topic,
        )

        GCSToBigQueryOperator(
            task_id=f"load_{table}_to_bq",
            bucket=GCS_BUCKET,
            source_objects=validated_files,
            destination_project_dataset_table=f"{GCP_PROJECT_ID}.{BQ_DATASET}.{table}",
            source_format="PARQUET",
            autodetect=True,
            create_disposition="CREATE_IF_NEEDED",
            write_disposition="WRITE_APPEND",
            time_partitioning={"type": "DAY"},
            gcp_conn_id=GCP_CONN_ID,
            location=BQ_LOCATION,
            labels={
                "layer": "raw",
                "source": "gcs",
                "pipeline": "cdc",
            },
            description=(
                f"Raw CDC load from GCS prefix for topic {topic}. "
                "Loaded by Airflow GCSToBigQueryOperator."
            ),
            # HAPUS BARIS job_id DI SINI
            force_rerun=False,
        )


dag = gcs_to_bigquery_cdc_hourly()
