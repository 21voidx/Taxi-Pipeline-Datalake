"""
DAG : mysql_to_bq_trino_multi_table
Airflow : 3.x
Engine  : Trino (federation layer — connects Mysql + BigQuery catalogs)

Pipeline per tabel (dalam TaskGroup paralel)
────────────────────────────────────────────
1. [BigQueryCreateTableOperator]  Buat <table>_temp di BQ via BQ API
2. [SQLExecuteQueryOperator]      INSERT mysql.<schema>.<table> → <table>_temp via Trino
3. [PythonOperator]               Schema evolution — ALTER TABLE ADD COLUMN IF NOT EXISTS
4. [BigQueryInsertJobOperator]    MERGE <table>_temp → <table> (UPSERT)
5. [BigQueryDeleteTableOperator]  DROP <table>_temp (trigger_rule=all_done)

Cara menambah / menghapus tabel
────────────────────────────────
Cukup tambah / hapus entry di TABLE_CONFIGS.
DAG otomatis membuat 1 TaskGroup per tabel, semua berjalan paralel.
"""

from __future__ import annotations
from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.models.param import Param
from airflow.timetables.interval import CronDataIntervalTimetable
from helpers.trino_helper_task_group_v2 import TableConfig, make_table_task_group

# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DAG_ID        = "Mysql_to_bq_trino_multi_table"
SOURCE_TZ     = "Asia/Jakarta"

TRINO_CONN_ID = "trino_default"
GCP_CONN_ID   = "google_cloud_default"
TRINO_BQ_CAT  = "bigquery"
TRINO_PG_CAT  = "mysql"

BQ_PROJECT    = "dbt-taxi-explore"
BQ_DATASET    = "dev_bronze_mysql"
BQ_LOCATION   = "US"
PG_SCHEMA     = "ride_marketing_mysql"

# ── Shared kwargs diteruskan ke setiap make_table_task_group() ────────────────
# Definisikan sekali di sini agar tidak repeat di setiap entry TABLE_CONFIGS.
SHARED = dict(
    bq_project    = BQ_PROJECT,
    bq_dataset    = BQ_DATASET,
    bq_location   = BQ_LOCATION,
    pg_schema     = PG_SCHEMA,
    trino_conn_id = TRINO_CONN_ID,
    gcp_conn_id   = GCP_CONN_ID,
    trino_bq_cat  = TRINO_BQ_CAT,
    trino_pg_cat  = TRINO_PG_CAT,
    source_tz     = SOURCE_TZ,
)

# ══════════════════════════════════════════════════════════════════════════════
#  TABLE_CONFIGS  ← tambah / hapus entry di sini
# ══════════════════════════════════════════════════════════════════════════════

TABLE_CONFIGS = [

    # ── payment_methods ───────────────────────────────────────────────────
    TableConfig(
        pg_table        = "payment_methods",
        bq_final_table  = "payment_methods",
        merge_key       = "method_id",
        partition_field = "created_at",
        partition_type  = "MONTH",
        cluster_fields  = ["method_type", "is_active"],
        source_system   = "ride_marketing_mysql",
        append_only     = False,
        schema_fields   = [
            {"name": "method_id",        "type": "INT64",     "mode": "REQUIRED"},
            {"name": "method_code",      "type": "STRING",    "mode": "REQUIRED"},
            {"name": "method_name",      "type": "STRING",    "mode": "REQUIRED"},
            {"name": "method_type",      "type": "STRING",    "mode": "REQUIRED"},
            {"name": "provider",         "type": "STRING",    "mode": "NULLABLE"},
            {"name": "is_active",        "type": "BOOL",      "mode": "REQUIRED"},
            {"name": "created_at",       "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",     "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",   "type": "STRING",    "mode": "REQUIRED"},
        ],
        table_columns   = """
            method_id, method_code, method_name, method_type,
            provider, is_active, created_at
        """,
    ),

    # ── payments ──────────────────────────────────────────────────────────
    TableConfig(
        pg_table        = "payments",
        bq_final_table  = "payments",
        merge_key       = "payment_id",
        partition_field = "created_at",
        partition_type  = "DAY",
        cluster_fields  = ["payment_status", "ride_id", "passenger_id", "method_id"],
        source_system   = "ride_marketing_mysql",
        append_only     = False,
        schema_fields   = [
            {"name": "payment_id",            "type": "STRING",    "mode": "REQUIRED"},
            {"name": "payment_code",          "type": "STRING",    "mode": "REQUIRED"},
            {"name": "ride_id",               "type": "STRING",    "mode": "REQUIRED"},
            {"name": "passenger_id",          "type": "STRING",    "mode": "REQUIRED"},
            {"name": "method_id",             "type": "INT64",     "mode": "REQUIRED"},
            {"name": "amount",                "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "platform_fee",          "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "driver_earning",        "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "promo_discount",        "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "currency",              "type": "STRING",    "mode": "REQUIRED"},
            {"name": "payment_status",        "type": "STRING",    "mode": "REQUIRED"},
            {"name": "payment_gateway",       "type": "STRING",    "mode": "NULLABLE"},
            {"name": "gateway_ref",           "type": "STRING",    "mode": "NULLABLE"},
            {"name": "gateway_raw_response",  "type": "STRING",    "mode": "NULLABLE"},
            {"name": "paid_at",               "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "expired_at",            "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "refunded_at",           "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "refund_reason",         "type": "STRING",    "mode": "NULLABLE"},
            {"name": "created_at",            "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",            "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",          "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",        "type": "STRING",    "mode": "NULLABLE"},
        ],
        table_columns   = """
            payment_id, payment_code, ride_id, passenger_id, method_id,
            amount, platform_fee, driver_earning, promo_discount, currency,
            payment_status, payment_gateway, gateway_ref, gateway_raw_response,
            paid_at, expired_at, refunded_at, refund_reason, created_at, updated_at
        """,
    ),

    # ── promotions ────────────────────────────────────────────────────────
    TableConfig(
        pg_table        = "promotions",
        bq_final_table  = "promotions",
        merge_key       = "promo_id",
        partition_field = "valid_from",
        partition_type  = "MONTH",
        cluster_fields  = ["promo_type", "is_active"],
        source_system   = "ride_marketing_mysql",
        append_only     = False,
        schema_fields   = [
            {"name": "promo_id",                   "type": "INT64",     "mode": "REQUIRED"},
            {"name": "promo_code",                 "type": "STRING",    "mode": "REQUIRED"},
            {"name": "promo_name",                 "type": "STRING",    "mode": "REQUIRED"},
            {"name": "description",                "type": "STRING",    "mode": "NULLABLE"},
            {"name": "promo_type",                 "type": "STRING",    "mode": "REQUIRED"},
            {"name": "discount_value",             "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "max_discount",               "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "min_fare",                   "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "applicable_vehicle_types",   "type": "STRING",    "mode": "NULLABLE"},
            {"name": "applicable_zones",           "type": "STRING",    "mode": "NULLABLE"},
            {"name": "max_usage_total",            "type": "INT64",     "mode": "NULLABLE"},
            {"name": "max_usage_per_user",         "type": "INT64",     "mode": "NULLABLE"},
            {"name": "current_usage",              "type": "INT64",     "mode": "NULLABLE"},
            {"name": "valid_from",                 "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "valid_until",                "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "is_active",                  "type": "BOOL",      "mode": "NULLABLE"},
            {"name": "created_at",                 "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",               "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",             "type": "STRING",    "mode": "REQUIRED"},
        ],
        table_columns   = """
            promo_id, promo_code, promo_name, description, promo_type,
            discount_value, max_discount, min_fare, applicable_vehicle_types,
            applicable_zones, max_usage_total, max_usage_per_user, current_usage,
            valid_from, valid_until, is_active, created_at
        """,
    ),

    # ── promo_usage ───────────────────────────────────────────────────────
    TableConfig(
        pg_table        = "promo_usage",
        bq_final_table  = "promo_usage",
        merge_key       = "usage_id",
        partition_field = "used_at",
        partition_type  = "DAY",
        cluster_fields  = ["promo_id", "passenger_id", "ride_id"],
        source_system   = "ride_marketing_mysql",
        append_only     = True,
        schema_fields   = [
            {"name": "usage_id",          "type": "INT64",     "mode": "REQUIRED"},
            {"name": "promo_id",          "type": "INT64",     "mode": "REQUIRED"},
            {"name": "passenger_id",      "type": "STRING",    "mode": "REQUIRED"},
            {"name": "ride_id",           "type": "STRING",    "mode": "REQUIRED"},
            {"name": "discount_given",    "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "used_at",           "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",      "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",    "type": "STRING",    "mode": "NULLABLE"},
        ],
        table_columns   = """
            usage_id, promo_id, passenger_id, ride_id,
            discount_given, used_at
        """,
    ),

    # ── driver_incentives ────────────────────────────────────────────────
    TableConfig(
        pg_table        = "driver_incentives",
        bq_final_table  = "driver_incentives",
        merge_key       = "incentive_id",
        partition_field = "period_date",
        partition_type  = "MONTH",
        cluster_fields  = ["driver_id", "incentive_type", "is_paid"],
        source_system   = "ride_marketing_mysql",
        append_only     = False,
        schema_fields   = [
            {"name": "incentive_id",      "type": "INT64",     "mode": "REQUIRED"},
            {"name": "driver_id",         "type": "STRING",    "mode": "REQUIRED"},
            {"name": "incentive_type",    "type": "STRING",    "mode": "REQUIRED"},
            {"name": "reference_id",      "type": "STRING",    "mode": "NULLABLE"},
            {"name": "amount",            "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "description",       "type": "STRING",    "mode": "NULLABLE"},
            {"name": "period_date",       "type": "DATE",      "mode": "REQUIRED"},
            {"name": "is_paid",           "type": "BOOL",      "mode": "NULLABLE"},
            {"name": "paid_at",           "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "created_at",        "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",      "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",    "type": "STRING",    "mode": "NULLABLE"},
        ],
        table_columns   = """
            incentive_id, driver_id, incentive_type, reference_id,
            amount, description, period_date, is_paid, paid_at,
            created_at
        """,
    ),
]

# ══════════════════════════════════════════════════════════════════════════════
#  DAG
# ══════════════════════════════════════════════════════════════════════════════

default_args = {
    "owner":                     "data-engineering",
    "depends_on_past":           False,
    "email_on_failure":          True,
    "email_on_retry":            False,
    "retries":                   3,
    "retry_delay":               timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "execution_timeout":         timedelta(hours=1),
}

with DAG(
    dag_id=DAG_ID,
    description=(
        f"Mysql → BigQuery via Trino | "
        f"multi-table ingestion ke {BQ_DATASET} "
        f"({len(TABLE_CONFIGS)} tabel)"
    ),
    default_args=default_args,
    schedule=CronDataIntervalTimetable("0 9 * * *", timezone="Asia/Jakarta"),
    start_date=pendulum.datetime(2026, 3, 29, tz="Asia/Jakarta"),
    catchup=True,
    max_active_runs=2,
    tags=["mysql", "bigquery", "trino", "ingestion", "multi-table"],
    doc_md=__doc__,
    params={
        "window_start": Param(
            default=None,
            type=["null", "string"],
            description=f"Window start (inklusif) dalam {SOURCE_TZ}. Contoh: 2026-03-11 09:00:00",
        ),
        "window_end": Param(
            default=None,
            type=["null", "string"],
            description=f"Window end (eksklusif) dalam {SOURCE_TZ}. Contoh: 2026-03-12 09:00:00",
        ),
    },
) as dag:

    # Buat satu TaskGroup per tabel — semua berjalan paralel
    table_groups = [make_table_task_group(cfg, **SHARED) for cfg in TABLE_CONFIGS]

    # ── Opsional: tambahkan dependency antar group jika urutan penting ─────────
    # Contoh urutan sequential:  table_groups[0] >> table_groups[1]
    # Default (tidak ada >>)  :  semua TaskGroup berjalan paralel