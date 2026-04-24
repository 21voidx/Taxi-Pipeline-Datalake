"""
DAG : postgres_to_bq_trino_multi_table
Airflow : 3.x
Engine  : Trino (federation layer — connects Postgres + BigQuery catalogs)

Pipeline per tabel (dalam TaskGroup paralel)
────────────────────────────────────────────
1. [BigQueryCreateTableOperator]  Buat <table>_temp di BQ via BQ API
2. [SQLExecuteQueryOperator]      INSERT postgresql.<schema>.<table> → <table>_temp via Trino
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
from airflow.sdk import DAG, Param
from airflow.timetables.interval import CronDataIntervalTimetable

# ── Sekarang cukup satu import dari satu file helper ──────────────────────────
from helpers.trino_helper import TableConfig, make_table_task_group


# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DAG_ID        = "postgres_to_bq_trino_multi_table_V3_with_label"
SOURCE_TZ     = "Asia/Jakarta"

TRINO_CONN_ID = "trino_default"
GCP_CONN_ID   = "google_cloud_default"
TRINO_BQ_CAT  = "bigquery"
TRINO_PG_CAT  = "postgresql"

BQ_PROJECT    = "dbt-taxi-explore"
BQ_DATASET    = "dev_bronze_pg"
BQ_LOCATION   = "US"
PG_SCHEMA     = "public"

# ── Base labels: dipakai untuk billing tracking di BQ Cost Table ──────────────
# Key/value constraint: lowercase, max 63 chars, hanya [a-z0-9_-].
# Dua label otomatis ditambahkan per-tabel oleh make_table_task_group:
#   "table"         → nama tabel sumber (contoh: "rides", "drivers")
#   "source-system" → dari TableConfig.source_system (contoh: "ride-ops-pg")
# Untuk query billing: SELECT labels.value, SUM(total_bytes_billed)
#   FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT, UNNEST(labels) AS labels
#   WHERE labels.key = 'table' GROUP BY 1
BASE_LABELS: dict[str, str] = {
    "env":      "dev",                                          # ganti: staging / prod
    "team":     "data-eng",
    "dag-id":   DAG_ID.lower().replace("_", "-")[:63],         # postgres-to-bq-trino-multi-table-v3...
    "layer":    "bronze",                                       # bronze / silver / gold
    "pipeline": "ingestion",                                    # ingestion / transformation
}

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
    dag_labels    = BASE_LABELS,    # ← semua tabel mewarisi label ini
)


# ══════════════════════════════════════════════════════════════════════════════
#  TABLE_CONFIGS  ← tambah / hapus entry di sini
# ══════════════════════════════════════════════════════════════════════════════

TABLE_CONFIGS = [

    # ── zones ──────────────────────────────────────────────────────────────
    TableConfig(
        pg_table        = "zones",
        bq_final_table  = "zones",
        merge_key       = "zone_id",
        partition_field = "created_at",
        partition_type  = "MONTH",
        cluster_fields  = ["city", "is_active"],
        source_system   = "ride_ops_pg",
        append_only     = False,
        schema_fields   = [
            {"name": "zone_id",         "type": "INT64",     "mode": "REQUIRED"},
            {"name": "zone_code",       "type": "STRING",    "mode": "REQUIRED"},
            {"name": "zone_name",       "type": "STRING",    "mode": "REQUIRED"},
            {"name": "city",            "type": "STRING",    "mode": "REQUIRED"},
            {"name": "latitude",        "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "longitude",       "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "is_active",       "type": "BOOL",      "mode": "REQUIRED"},
            {"name": "created_at",      "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",    "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",  "type": "STRING",    "mode": "REQUIRED"},
        ],
        table_columns   = """
            zone_id, zone_code, zone_name, city, latitude,
            longitude, is_active, created_at
        """,
    ),

    # ── vehicle_types ──────────────────────────────────────────────────────
    TableConfig(
        pg_table        = "vehicle_types",
        bq_final_table  = "vehicle_types",
        merge_key       = "vehicle_type_id",
        partition_field = "created_at",
        partition_type  = "MONTH",
        cluster_fields  = ["type_code"],
        source_system   = "ride_ops_pg",
        append_only     = False,
        schema_fields   = [
            {"name": "vehicle_type_id", "type": "INT64",     "mode": "REQUIRED"},
            {"name": "type_code",       "type": "STRING",    "mode": "REQUIRED"},
            {"name": "type_name",       "type": "STRING",    "mode": "REQUIRED"},
            {"name": "base_fare",       "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "per_km_rate",     "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "per_minute_rate", "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "capacity",        "type": "INT64",     "mode": "REQUIRED"},
            {"name": "created_at",      "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",    "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",  "type": "STRING",    "mode": "REQUIRED"},
        ],
        table_columns   = """
            vehicle_type_id, type_code, type_name, base_fare,
            per_km_rate, per_minute_rate, capacity, created_at
        """,
    ),

    # ── drivers ────────────────────────────────────────────────────────────
    TableConfig(
        pg_table        = "drivers",
        bq_final_table  = "drivers",
        merge_key       = "driver_id",
        partition_field = "updated_at",
        partition_type  = "MONTH",
        cluster_fields  = ["status", "home_zone_id", "vehicle_type_id"],
        source_system   = "ride_ops_pg",
        append_only     = False,
        schema_fields   = [
            {"name": "driver_id",        "type": "STRING",    "mode": "REQUIRED"},
            {"name": "driver_code",      "type": "STRING",    "mode": "REQUIRED"},
            {"name": "full_name",        "type": "STRING",    "mode": "REQUIRED"},
            {"name": "phone_number",     "type": "STRING",    "mode": "REQUIRED"},
            {"name": "email",            "type": "STRING",    "mode": "NULLABLE"},
            {"name": "nik",              "type": "STRING",    "mode": "NULLABLE"},
            {"name": "sim_number",       "type": "STRING",    "mode": "NULLABLE"},
            {"name": "license_plate",    "type": "STRING",    "mode": "REQUIRED"},
            {"name": "vehicle_type_id",  "type": "INT64",     "mode": "NULLABLE"},
            {"name": "vehicle_brand",    "type": "STRING",    "mode": "NULLABLE"},
            {"name": "vehicle_model",    "type": "STRING",    "mode": "NULLABLE"},
            {"name": "vehicle_year",     "type": "INT64",     "mode": "NULLABLE"},
            {"name": "vehicle_color",    "type": "STRING",    "mode": "NULLABLE"},
            {"name": "home_zone_id",     "type": "INT64",     "mode": "NULLABLE"},
            {"name": "rating",           "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "total_trips",      "type": "INT64",     "mode": "NULLABLE"},
            {"name": "status",           "type": "STRING",    "mode": "NULLABLE"},
            {"name": "joined_at",        "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",       "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",     "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",   "type": "STRING",    "mode": "REQUIRED"},
        ],
        table_columns   = """
            driver_id, driver_code, full_name, phone_number, email,
            nik, sim_number, license_plate, vehicle_type_id, vehicle_brand,
            vehicle_model, vehicle_year, vehicle_color, home_zone_id,
            rating, total_trips, status, joined_at, updated_at
        """,
    ),

    # ── passengers ─────────────────────────────────────────────────────────
    TableConfig(
        pg_table        = "passengers",
        bq_final_table  = "passengers",
        merge_key       = "passenger_id",
        partition_field = "updated_at",
        partition_type  = "MONTH",
        cluster_fields  = ["status", "home_zone_id"],
        source_system   = "ride_ops_pg",
        append_only     = False,
        schema_fields   = [
            {"name": "passenger_id",     "type": "STRING",    "mode": "REQUIRED"},
            {"name": "passenger_code",   "type": "STRING",    "mode": "REQUIRED"},
            {"name": "full_name",        "type": "STRING",    "mode": "REQUIRED"},
            {"name": "phone_number",     "type": "STRING",    "mode": "REQUIRED"},
            {"name": "email",            "type": "STRING",    "mode": "NULLABLE"},
            {"name": "birth_date",       "type": "DATE",      "mode": "NULLABLE"},
            {"name": "gender",           "type": "STRING",    "mode": "NULLABLE"},
            {"name": "home_zone_id",     "type": "INT64",     "mode": "NULLABLE"},
            {"name": "referral_code",    "type": "STRING",    "mode": "NULLABLE"},
            {"name": "is_verified",      "type": "BOOL",      "mode": "NULLABLE"},
            {"name": "total_trips",      "type": "INT64",     "mode": "NULLABLE"},
            {"name": "status",           "type": "STRING",    "mode": "NULLABLE"},
            {"name": "registered_at",    "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",       "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",     "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",   "type": "STRING",    "mode": "REQUIRED"},
        ],
        table_columns   = """
            passenger_id, passenger_code, full_name, phone_number, email,
            birth_date, gender, home_zone_id, referral_code, is_verified,
            total_trips, status, registered_at, updated_at
        """,
    ),

    # ── rides ──────────────────────────────────────────────────────────────
    # label "priority" di-override di sini karena rides adalah tabel terbesar
    # dan perlu dimonitor secara terpisah di billing dashboard.
    TableConfig(
        pg_table        = "rides",
        bq_final_table  = "rides",
        merge_key       = "ride_id",
        partition_field = "requested_at",
        partition_type  = "MONTH",
        cluster_fields  = ["ride_status", "driver_id", "passenger_id", "pickup_zone_id"],
        source_system   = "ride_ops_pg",
        append_only     = False,
        labels          = {"env": "dev", "team": "data-eng", "priority": "high"},
        schema_fields   = [
            {"name": "ride_id",              "type": "STRING",    "mode": "REQUIRED"},
            {"name": "ride_code",            "type": "STRING",    "mode": "REQUIRED"},
            {"name": "driver_id",            "type": "STRING",    "mode": "NULLABLE"},
            {"name": "passenger_id",         "type": "STRING",    "mode": "NULLABLE"},
            {"name": "vehicle_type_id",      "type": "INT64",     "mode": "NULLABLE"},
            {"name": "pickup_zone_id",       "type": "INT64",     "mode": "NULLABLE"},
            {"name": "dropoff_zone_id",      "type": "INT64",     "mode": "NULLABLE"},
            {"name": "pickup_address",       "type": "STRING",    "mode": "NULLABLE"},
            {"name": "dropoff_address",      "type": "STRING",    "mode": "NULLABLE"},
            {"name": "pickup_lat",           "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "pickup_lon",           "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "dropoff_lat",          "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "dropoff_lon",          "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "requested_at",         "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "accepted_at",          "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "picked_up_at",         "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "completed_at",         "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "cancelled_at",         "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "distance_km",          "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "duration_minutes",     "type": "INT64",     "mode": "NULLABLE"},
            {"name": "base_fare",            "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "distance_fare",        "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "time_fare",            "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "surge_multiplier",     "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "promo_discount",       "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "total_fare",           "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "ride_status",          "type": "STRING",    "mode": "NULLABLE"},
            {"name": "cancellation_reason",  "type": "STRING",    "mode": "NULLABLE"},
            {"name": "passenger_rating",     "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "driver_rating",        "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "notes",                "type": "STRING",    "mode": "NULLABLE"},
            {"name": "created_at",           "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",           "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_ingested_at",         "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "_source_system",       "type": "STRING",    "mode": "REQUIRED"},
        ],
        table_columns   = """
            ride_id, ride_code, driver_id, passenger_id, vehicle_type_id,
            pickup_zone_id, dropoff_zone_id, pickup_address, dropoff_address,
            pickup_lat, pickup_lon, dropoff_lat, dropoff_lon, requested_at,
            accepted_at, picked_up_at, completed_at, cancelled_at,
            distance_km, duration_minutes, base_fare, distance_fare,
            time_fare, surge_multiplier, promo_discount, total_fare,
            ride_status, cancellation_reason, passenger_rating, driver_rating,
            notes, created_at, updated_at
        """,
    ),

    # # ── ride_events ────────────────────────────────────────────────────────
    # TableConfig(
    #     pg_table        = "ride_events",
    #     bq_final_table  = "ride_events",
    #     merge_key       = "event_id",
    #     partition_field = "occurred_at",
    #     partition_type  = "MONTH",
    #     cluster_fields  = ["event_type", "ride_id"],
    #     json_fields     = ["event_payload"],
    #     source_system   = "ride_ops_pg",
    #     append_only     = True,
    #     schema_fields   = [
    #         {"name": "event_id",         "type": "STRING",    "mode": "REQUIRED"},
    #         {"name": "ride_id",          "type": "STRING",    "mode": "REQUIRED"},
    #         {"name": "event_type",       "type": "STRING",    "mode": "REQUIRED"},
    #         {"name": "event_payload",    "type": "STRING",    "mode": "NULLABLE"},
    #         {"name": "occurred_at",      "type": "TIMESTAMP", "mode": "REQUIRED"},
    #         {"name": "_ingested_at",     "type": "TIMESTAMP", "mode": "REQUIRED"},
    #         {"name": "_source_system",   "type": "STRING",    "mode": "REQUIRED"},
    #     ],
    #     table_columns   = """
    #         event_id, ride_id, event_type, event_payload, occurred_at
    #     """,
    # ),
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
        f"Postgres → BigQuery via Trino | "
        f"multi-table ingestion ke {BQ_DATASET} "
        f"({len(TABLE_CONFIGS)} tabel)"
    ),
    default_args=default_args,
    schedule=CronDataIntervalTimetable("30 14 * * *", timezone="Asia/Jakarta"), # setiap hari pada pukul 14:30 (2:30 siang)
    start_date=pendulum.datetime(2026, 4, 19, tz="Asia/Jakarta"),
    catchup=True,
    max_active_runs=1,
    tags=["postgres", "bigquery", "trino", "ingestion", "multi-table"],
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