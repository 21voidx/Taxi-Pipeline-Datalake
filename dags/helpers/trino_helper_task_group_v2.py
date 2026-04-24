"""
helpers/trino_helper_task_group.py
═══════════════════════════════════════════════════════════════════════════════
Factory function untuk membuat Airflow TaskGroup pipeline source database →
BigQuery via Trino cross-catalog. Mendukung sumber relasional apapun yang
tersedia sebagai Trino catalog (PostgreSQL, MySQL, dll.).

Dipisah dari trino_helper.py agar:
  • trino_helper.py  → pure utility (SQL builder, schema tools), bisa di-test
                       tanpa Airflow runtime
  • file ini         → Airflow-specific (Operators, TaskGroup, Asset lineage),
                       hanya dipakai dalam konteks DAG

Cara pakai di DAG:
    from helpers.trino_helper_task_group import make_table_task_group, TableConfig

    with DAG(...) as dag:
        groups = [make_table_task_group(cfg, **SHARED) for cfg in TABLE_CONFIGS]

Struktur task dalam setiap TaskGroup:
    create_bq_temp_table
        → insert_source_to_bq_temp
            → sync_final_table_schema
                → merge_temp_to_final
                    → drop_bq_temp_table
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from airflow.operators.python import PythonOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.providers.google.cloud.operators.bigquery import (
    BigQueryCreateTableOperator,
    BigQueryDeleteTableOperator,
    BigQueryInsertJobOperator,
)
from airflow.sdk.definitions.asset import Asset
from airflow.utils.task_group import TaskGroup
from airflow.utils.trigger_rule import TriggerRule

from helpers.trino_helper_v2 import (
    build_bq_merge_query,
    build_metadata_exprs,
    build_table_resource,
    build_trino_columns,
    build_trino_insert_sql,
    parse_columns,
    sync_final_table_schema,
)

if TYPE_CHECKING:
    pass


# ══════════════════════════════════════════════════════════════════════════════
#  TableConfig — dataclass untuk validasi config per tabel
#
#  Gunakan ini sebagai pengganti plain dict agar typo pada key terdeteksi
#  saat import DAG (bukan saat runtime).
#
#  Contoh pemakaian:
#      from helpers.trino_helper_task_group import TableConfig, make_table_task_group
#
#      cfg = TableConfig(
#          pg_table        = "payments",
#          bq_final_table  = "payments",
#          merge_key       = "payment_id",
#          partition_field = "created_at",
#          source_system   = "ride_ops_pg",       # atau "ride_marketing_mysql"
#          schema_fields   = [...],
#          table_columns   = "payment_id, amount, ...",
#      )
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TableConfig:
    pg_table        : str
    bq_final_table  : str
    merge_key       : str
    partition_field : str
    schema_fields   : list[dict]
    table_columns   : str
    source_system   : str               = "source"
    partition_type  : str               = "MONTH"      # DAY | MONTH | YEAR
    cluster_fields  : list[str]         = field(default_factory=list)
    append_only     : bool              = False
    json_fields     : list[str]         = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
#  make_table_task_group
# ══════════════════════════════════════════════════════════════════════════════

def make_table_task_group(
    cfg             : TableConfig | dict,
    *,
    bq_project      : str,
    bq_dataset      : str,
    bq_location     : str,
    pg_schema       : str,
    trino_conn_id   : str,
    gcp_conn_id     : str,
    trino_bq_cat    : str,
    trino_pg_cat    : str,
    source_tz       : str = "Asia/Jakarta",
) -> TaskGroup:
    """
    Buat TaskGroup berisi 5 task untuk satu tabel sumber.
    Harus dipanggil dari dalam konteks ``with DAG(...) as dag:``.

    Parameters
    ──────────
    cfg : TableConfig | dict
        Konfigurasi tabel. Bisa berupa TableConfig dataclass atau plain dict
        dengan key yang sama.

    bq_project, bq_dataset, bq_location : str
        Koordinat BigQuery tujuan.

    pg_schema : str
        Schema / database sumber (contoh: "public" untuk PostgreSQL,
        nama database untuk MySQL).

    trino_conn_id, gcp_conn_id : str
        Airflow Connection ID untuk Trino dan GCP.

    trino_bq_cat, trino_pg_cat : str
        Nama catalog Trino untuk BigQuery dan tabel sumber
        (PostgreSQL, MySQL, atau database relasional lain).

    source_tz : str
        Timezone kolom timestamp di tabel sumber (default "Asia/Jakarta").
        Dipakai untuk:
          1. AT TIME ZONE cast pada setiap kolom TIMESTAMP di Trino SELECT
             (timestamp naive dari sumber → timestamp(6) with time zone untuk BQ).
          2. Konversi window UTC → local pada Trino WHERE filter.

    Returns
    ───────
    TaskGroup
        Airflow TaskGroup yang sudah berisi semua task dan dependency-nya.
        Bisa langsung disambung dengan ``>>`` ke TaskGroup lain jika diperlukan.

    Raises
    ──────
    ValueError
        Jika ada nama kolom di table_columns yang tidak ada di schema_fields.
        Fail-fast saat parse time — error muncul saat DAG di-load, bukan saat run.
    """
    # ── Normalise: terima dict atau dataclass ──────────────────────────────────
    if isinstance(cfg, dict):
        cfg = TableConfig(**cfg)

    pg_table        = cfg.pg_table
    bq_final_table  = cfg.bq_final_table
    merge_key       = cfg.merge_key
    partition_field = cfg.partition_field
    partition_type  = cfg.partition_type
    cluster_fields  = cfg.cluster_fields
    source_system   = cfg.source_system
    append_only     = cfg.append_only
    schema_fields   = cfg.schema_fields
    table_columns   = cfg.table_columns
    json_fields     = cfg.json_fields

    # Template BQ temp table — unik per tabel per run
    bq_temp_table = f"{bq_final_table}_temp_{{{{ ds_nodash }}}}"

    # Pre-compute column metadata — fail-fast sebelum DAG berjalan
    _schema_lookup  = {f["name"]: f["type"] for f in schema_fields}
    _columns        = parse_columns(table_columns, _schema_lookup)

    # FIX: source_tz diteruskan ke build_trino_columns agar TIMESTAMP columns
    # mendapat AT TIME ZONE cast yang benar (timestamp naive dari sumber →
    # timestamp(6) with time zone untuk BQ).
    _trino_columns  = build_trino_columns(
        _columns,
        _schema_lookup,
        json_columns=json_fields,
        source_tz=source_tz,            # ← tambahan parameter fix
    )
    _metadata_exprs = build_metadata_exprs(source_system)

    # Asset lineage
    bq_final_asset = Asset(f"bigquery://{bq_project}/{bq_dataset}/{bq_final_table}")
    bq_temp_asset  = Asset(f"bigquery://{bq_project}/{bq_dataset}/{bq_temp_table}")

    with TaskGroup(group_id=f"load_{pg_table}") as tg:

        # ── Step 1: CREATE temp table via BigQuery API ─────────────────────────
        create_temp = BigQueryCreateTableOperator(
            task_id="create_bq_temp_table",
            gcp_conn_id=gcp_conn_id,
            project_id=bq_project,
            dataset_id=bq_dataset,
            table_id=bq_temp_table,
            table_resource=build_table_resource(
                bq_project=bq_project,
                bq_dataset=bq_dataset,
                table_id=bq_temp_table,
                schema_fields=schema_fields,
                partition_field=partition_field,
                partition_type=partition_type,
                cluster_fields=cluster_fields,
            ),
            if_exists="ignore",
            outlets=[bq_temp_asset],
        )

        # ── Step 2: INSERT source → temp via Trino cross-catalog ─────────────
        insert_to_temp = SQLExecuteQueryOperator(
            task_id="insert_source_to_bq_temp",
            conn_id=trino_conn_id,
            sql=build_trino_insert_sql(
                trino_bq_catalog=trino_bq_cat,
                trino_pg_catalog=trino_pg_cat,
                bq_dataset=bq_dataset,
                bq_temp_table=bq_temp_table,
                pg_schema=pg_schema,
                pg_source_table=pg_table,
                merge_key=merge_key,
                partition_field=partition_field,
                columns=_columns,
                trino_columns=_trino_columns,
                metadata_exprs=_metadata_exprs,
                source_tz=source_tz,
            ),
            autocommit=True,
            do_xcom_push=False,
            outlets=[bq_temp_asset],
        )

        # ── Step 3: Schema evolution — ADD COLUMN IF NOT EXISTS ───────────────
        sync_schema = PythonOperator(
            task_id="sync_final_table_schema",
            python_callable=sync_final_table_schema,
            op_kwargs={
                "gcp_conn_id":    gcp_conn_id,
                "bq_project":     bq_project,
                "bq_dataset":     bq_dataset,
                "bq_final_table": bq_final_table,
            },
        )

        # ── Step 4: MERGE temp → final (UPSERT) ───────────────────────────────
        merge_to_final = BigQueryInsertJobOperator(
            task_id="merge_temp_to_final",
            gcp_conn_id=gcp_conn_id,
            project_id=bq_project,
            location=bq_location,
            configuration=build_bq_merge_query(
                bq_project=bq_project,
                bq_dataset=bq_dataset,
                bq_final_table=bq_final_table,
                bq_temp_table=bq_temp_table,
                merge_key=merge_key,
                partition_field=partition_field,
                columns=_columns,
                append_only=append_only,
            ),
            # job_id menyertakan nama tabel agar unik di BQ job history
            job_id=f"{{{{ dag.dag_id }}}}__merge__{pg_table}__{{{{ ds_nodash }}}}",
            force_rerun=True,
            deferrable=False,
            outlets=[bq_final_asset],
        )

        # ── Step 5: DROP temp (selalu jalan meski step sebelumnya gagal) ──────
        drop_temp = BigQueryDeleteTableOperator(
            task_id="drop_bq_temp_table",
            gcp_conn_id=gcp_conn_id,
            deletion_dataset_table=(
                f"{bq_project}.{bq_dataset}.{bq_temp_table}"
            ),
            ignore_if_missing=True,
            trigger_rule=TriggerRule.ALL_DONE,
        )

        # ── Dependency dalam group ─────────────────────────────────────────────
        create_temp >> insert_to_temp >> sync_schema >> merge_to_final >> drop_temp

    return tg