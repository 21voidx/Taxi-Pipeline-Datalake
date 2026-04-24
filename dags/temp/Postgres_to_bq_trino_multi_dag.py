"""
DAG : postgres_to_bq_trino_dag_multi_table
Airflow : 3.x
Engine  : Trino (federation layer — connects Postgres + BigQuery catalogs)

Pipeline per tabel (dijalankan paralel via TaskGroup):
────────────────────────────────────────────────────────────────────────────────
1. [BigQueryCreateTableOperator]  Buat <table>_temp di BQ via BQ API
                                  (partitioned MONTH(partition_field) + clustered, if_exists=ignore)
2. [SQLExecuteQueryOperator]      INSERT postgresql.public.<table> → <table>_temp via Trino
                                  (cross-catalog, window = data_interval_start s/d data_interval_end, dedup ROW_NUMBER)
3. [PythonOperator]               Schema evolution — bandingkan temp vs final,
                                  ALTER TABLE <table> ADD COLUMN IF NOT EXISTS kolom baru
4. [BigQueryInsertJobOperator]    MERGE <table>_temp → <table> (UPSERT + guard dedup)
5. [BigQueryDeleteTableOperator]  DROP <table>_temp (trigger_rule=all_done)

Cara menambah tabel baru:
─────────────────────────────────────────────────────────────────────────────
  Tambahkan entry baru di list TABLES_CONFIG di bawah.
  Setiap entry wajib memiliki key:
    - pg_source_table  : nama tabel di Postgres
    - bq_final_table   : nama tabel tujuan di BigQuery
    - schema_fields    : list dict {name, type, mode}  (BQ schema)
    - table_columns    : string kolom dipisah koma  (urutan bebas)
    - partition_field  : kolom untuk time partitioning (MONTH)
    - cluster_fields   : list str, max 4 kolom
    - merge_key        : primary key untuk MERGE

Kenapa BigQueryCreateTableOperator untuk step 1 (bukan Trino DDL)?
───────────────────────────────────────────────────────────────────
  Trino BigQuery connector TIDAK support WITH (partitioning=..., clustering_key=...).
  Property itu milik Iceberg/Hive connector. BigQueryCreateTableOperator pakai
  BQ REST API langsung → full support timePartitioning + clustering natively.

Kenapa Trino untuk step 2 (bukan BigQueryInsertJobOperator)?
─────────────────────────────────────────────────────────────
  Trino federasi postgresql.* → bigquery.* dalam satu query.
  Tidak butuh GCS staging, tidak butuh BQ External Connection.

Kenapa PythonOperator untuk step 3 (schema evolution)?
───────────────────────────────────────────────────────
  MERGE tidak support schemaUpdateOptions (hanya valid untuk LOAD/INSERT SELECT).
  Solusi: deteksi kolom baru dari temp table, lalu ALTER TABLE final
  ADD COLUMN IF NOT EXISTS sebelum MERGE dijalankan.

Kenapa BigQueryInsertJobOperator untuk step 4 (bukan Trino)?
─────────────────────────────────────────────────────────────
  MERGE adalah BQ Standard SQL — lebih efisien dijalankan native di BQ engine.

Connections (Airflow UI → Admin → Connections)
───────────────────────────────────────────────
  trino_default        : Trino  (Conn Type=trino, host=banking-trino, port=8080, login=trino)
  google_cloud_default : GCP service account dengan role BigQuery Data Editor + Job User
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.providers.google.cloud.operators.bigquery import (
    BigQueryCreateTableOperator,
    BigQueryDeleteTableOperator,
    BigQueryInsertJobOperator,
)
from airflow.sdk.definitions.asset import Asset
from airflow.timetables.interval import CronDataIntervalTimetable
from airflow.utils.task_group import TaskGroup
from airflow.utils.trigger_rule import TriggerRule

# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL CONFIG  — sesuaikan dengan environment Anda
# ══════════════════════════════════════════════════════════════════════════════

TRINO_CONN_ID    = "trino_default"
GCP_CONN_ID      = "google_cloud_default"

TRINO_BQ_CATALOG = "bigquery"
TRINO_PG_CATALOG = "postgresql"

BQ_PROJECT  = "banking-modernstack"
BQ_DATASET  = "raw_core_banking"
BQ_LOCATION = "US"
PG_SCHEMA   = "public"

# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL TYPE MAP  — tidak perlu diubah saat ganti tabel
# ══════════════════════════════════════════════════════════════════════════════

_BQ_TO_TRINO_CAST: dict[str, str] = {
    "INTEGER": "BIGINT",
    "STRING":  "VARCHAR",
}

# ══════════════════════════════════════════════════════════════════════════════
#  TABLES CONFIG  — tambahkan entry baru di sini untuk setiap tabel
# ══════════════════════════════════════════════════════════════════════════════

TABLES_CONFIG: list[dict[str, Any]] = [

    # ── Tabel 1: customers ─────────────────────────────────────────────────
    {
        "pg_source_table": "customers",
        "bq_final_table":  "customers",
        "partition_field": "updated_at",
        "cluster_fields":  ["address_city", "customer_segment", "kyc_status"],
        "merge_key":       "customer_id",
        "schema_fields": [
            {"name": "customer_id",            "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "customer_uuid",          "type": "STRING",    "mode": "REQUIRED"},
            {"name": "full_name",              "type": "STRING",    "mode": "REQUIRED"},
            {"name": "national_id",            "type": "STRING",    "mode": "REQUIRED"},
            {"name": "date_of_birth",          "type": "DATE",      "mode": "REQUIRED"},
            {"name": "gender",                 "type": "STRING",    "mode": "REQUIRED"},
            {"name": "marital_status",         "type": "STRING",    "mode": "NULLABLE"},
            {"name": "occupation",             "type": "STRING",    "mode": "NULLABLE"},
            {"name": "income_range",           "type": "STRING",    "mode": "NULLABLE"},
            {"name": "email",                  "type": "STRING",    "mode": "NULLABLE"},
            {"name": "phone_primary",          "type": "STRING",    "mode": "NULLABLE"},
            {"name": "phone_secondary",        "type": "STRING",    "mode": "NULLABLE"},
            {"name": "address_street",         "type": "STRING",    "mode": "NULLABLE"},
            {"name": "address_city",           "type": "STRING",    "mode": "NULLABLE"},
            {"name": "address_province",       "type": "STRING",    "mode": "NULLABLE"},
            {"name": "address_postal_code",    "type": "STRING",    "mode": "NULLABLE"},
            {"name": "customer_segment",       "type": "STRING",    "mode": "REQUIRED"},
            {"name": "kyc_status",             "type": "STRING",    "mode": "REQUIRED"},
            {"name": "kyc_verified_at",        "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "onboarding_branch_id",   "type": "INTEGER",   "mode": "NULLABLE"},
            {"name": "acquisition_channel",    "type": "STRING",    "mode": "NULLABLE"},
            {"name": "risk_rating",            "type": "STRING",    "mode": "NULLABLE"},
            {"name": "is_politically_exposed", "type": "BOOLEAN",   "mode": "REQUIRED"},
            {"name": "created_at",             "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",             "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "deleted_at",             "type": "TIMESTAMP", "mode": "NULLABLE"},
        ],
        "table_columns": """
            customer_id, customer_uuid, full_name, national_id, date_of_birth, gender,
            marital_status, occupation, income_range, email, phone_primary, phone_secondary,
            address_street, address_city, address_province, address_postal_code,
            customer_segment, kyc_status, kyc_verified_at, onboarding_branch_id,
            acquisition_channel, risk_rating, is_politically_exposed,
            created_at, updated_at, deleted_at
        """,
    },

    # ── Tabel 2: branches ──────────────────────────────────────────────────
    # Contoh tabel kedua — sesuaikan schema_fields & table_columns
    {
        "pg_source_table": "branches",
        "bq_final_table":  "branches",
        "partition_field": "updated_at",
        "cluster_fields":  ["province", "branch_type", "is_active"],
        "merge_key":       "branch_id",
        "schema_fields": [
            {"name": "branch_id",   "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "branch_code", "type": "STRING",    "mode": "REQUIRED"},
            {"name": "branch_name", "type": "STRING",    "mode": "REQUIRED"},
            {"name": "branch_type", "type": "STRING",    "mode": "REQUIRED"},
            {"name": "city",        "type": "STRING",    "mode": "REQUIRED"},
            {"name": "province",    "type": "STRING",    "mode": "REQUIRED"},
            {"name": "address",     "type": "STRING",    "mode": "NULLABLE"},
            {"name": "phone",       "type": "STRING",    "mode": "NULLABLE"},
            {"name": "is_active",   "type": "BOOLEAN",   "mode": "REQUIRED"},
            {"name": "opened_date", "type": "DATE",      "mode": "NULLABLE"},
            {"name": "created_at",  "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",  "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "deleted_at",  "type": "TIMESTAMP", "mode": "NULLABLE"},
        ],
        "table_columns": """
            branch_id, branch_code, branch_name, branch_type,
            province, city, address, is_active,
            created_at, updated_at, deleted_at
        """,
    },
    # ── 4. product_types ──────────────────────────────────────────────────────
    {
        "pg_source_table": "product_types",
        "bq_final_table":  "product_types",
        "partition_field": "updated_at",
        "cluster_fields":  ["product_category", "is_active"],
        "merge_key":       "product_type_id",
        "schema_fields": [
            {"name": "product_type_id",   "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "product_code",      "type": "STRING",    "mode": "REQUIRED"},
            {"name": "product_name",      "type": "STRING",    "mode": "REQUIRED"},
            {"name": "product_category",  "type": "STRING",    "mode": "REQUIRED"},
            {"name": "interest_rate",     "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "is_active",         "type": "BOOLEAN",   "mode": "REQUIRED"},
            {"name": "created_at",        "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",        "type": "TIMESTAMP", "mode": "REQUIRED"},
        ],
        "table_columns": """
            product_type_id, product_code, product_name, product_category,
            interest_rate, is_active, created_at, updated_at
        """,
    },
 
    # ── 5. employees ──────────────────────────────────────────────────────────
    {
        "pg_source_table": "employees",
        "bq_final_table":  "employees",
        "partition_field": "updated_at",
        "cluster_fields":  ["role", "is_active", "branch_id"],
        "merge_key":       "employee_id",
        "schema_fields": [
            {"name": "employee_id",   "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "employee_code", "type": "STRING",    "mode": "REQUIRED"},
            {"name": "full_name",     "type": "STRING",    "mode": "REQUIRED"},
            {"name": "email",         "type": "STRING",    "mode": "REQUIRED"},
            {"name": "role",          "type": "STRING",    "mode": "REQUIRED"},
            {"name": "branch_id",     "type": "INTEGER",   "mode": "NULLABLE"},
            {"name": "hire_date",     "type": "DATE",      "mode": "REQUIRED"},
            {"name": "is_active",     "type": "BOOLEAN",   "mode": "REQUIRED"},
            {"name": "created_at",    "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",    "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "deleted_at",    "type": "TIMESTAMP", "mode": "NULLABLE"},
        ],
        "table_columns": """
            employee_id, employee_code, full_name, email, role,
            branch_id, hire_date, is_active,
            created_at, updated_at, deleted_at
        """,
    },
 
    # ── 6. accounts ───────────────────────────────────────────────────────────
    {
        "pg_source_table": "accounts",
        "bq_final_table":  "accounts",
        "partition_field": "updated_at",
        "cluster_fields":  ["account_status", "product_type_id", "currency", "branch_id"],
        "merge_key":       "account_id",
        "schema_fields": [
            {"name": "account_id",          "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "account_number",      "type": "STRING",    "mode": "REQUIRED"},
            {"name": "customer_id",         "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "product_type_id",     "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "branch_id",           "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "currency",            "type": "STRING",    "mode": "REQUIRED"},
            {"name": "balance",             "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "available_balance",   "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "account_status",      "type": "STRING",    "mode": "REQUIRED"},
            {"name": "opened_date",         "type": "DATE",      "mode": "REQUIRED"},
            {"name": "closed_date",         "type": "DATE",      "mode": "NULLABLE"},
            {"name": "last_transaction_at", "type": "TIMESTAMP", "mode": "NULLABLE"},
            # Loan-specific fields (nullable for non-loan accounts)
            {"name": "credit_limit",        "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "outstanding_balance", "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "interest_rate",       "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "tenor_months",        "type": "INTEGER",   "mode": "NULLABLE"},
            {"name": "due_date",            "type": "DATE",      "mode": "NULLABLE"},
            {"name": "dpd",                 "type": "INTEGER",   "mode": "NULLABLE"},
            # Audit
            {"name": "created_at",          "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",          "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "deleted_at",          "type": "TIMESTAMP", "mode": "NULLABLE"},
        ],
        "table_columns": """
            account_id, account_number, customer_id, product_type_id, branch_id,
            currency, balance, available_balance, account_status,
            opened_date, closed_date, last_transaction_at,
            credit_limit, outstanding_balance, interest_rate, tenor_months, due_date, dpd,
            created_at, updated_at, deleted_at
        """,
    },
 
    # ── 7. credit_scores ──────────────────────────────────────────────────────
    # Catatan: credit_scores bersifat append-heavy (satu scoring event = satu baris baru).
    # MERGE dengan score_id tetap aman: INSERT baris baru, UPDATE jika score_id sama di-reprocess.
    # Partition by score_date (natural time dimension for analytics).
    # updated_at tetap dipakai sebagai window filter di Trino INSERT.
    {
        "pg_source_table": "credit_scores",
        "bq_final_table":  "credit_scores",
        "partition_field": "updated_at",      # dipakai untuk window filter incremental load
        "cluster_fields":  ["score_band", "model_version", "data_source"],
        "merge_key":       "score_id",
        "schema_fields": [
            {"name": "score_id",       "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "customer_id",    "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "score_value",    "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "score_band",     "type": "STRING",    "mode": "REQUIRED"},
            {"name": "score_date",     "type": "DATE",      "mode": "REQUIRED"},
            {"name": "model_version",  "type": "STRING",    "mode": "REQUIRED"},
            {"name": "data_source",    "type": "STRING",    "mode": "REQUIRED"},
            {"name": "created_at",     "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",     "type": "TIMESTAMP", "mode": "REQUIRED"},
        ],
        "table_columns": """
            score_id, customer_id, score_value, score_band,
            score_date, model_version, data_source,
            created_at, updated_at
        """,
    },
 
    # ── 8. loan_applications ──────────────────────────────────────────────────
    {
        "pg_source_table": "loan_applications",
        "bq_final_table":  "loan_applications",
        "partition_field": "updated_at",
        "cluster_fields":  ["application_status", "product_type_id", "branch_id"],
        "merge_key":       "application_id",
        "schema_fields": [
            {"name": "application_id",     "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "application_number", "type": "STRING",    "mode": "REQUIRED"},
            {"name": "customer_id",        "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "branch_id",          "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "handled_by",         "type": "INTEGER",   "mode": "NULLABLE"},
            {"name": "product_type_id",    "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "requested_amount",   "type": "NUMERIC",   "mode": "REQUIRED"},
            {"name": "approved_amount",    "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "tenor_months",       "type": "INTEGER",   "mode": "REQUIRED"},
            {"name": "interest_rate",      "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "purpose",            "type": "STRING",    "mode": "NULLABLE"},
            {"name": "collateral_type",    "type": "STRING",    "mode": "NULLABLE"},
            {"name": "collateral_value",   "type": "NUMERIC",   "mode": "NULLABLE"},
            {"name": "application_status", "type": "STRING",    "mode": "REQUIRED"},
            {"name": "submitted_at",       "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "reviewed_at",        "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "decided_at",         "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "disbursed_at",       "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "rejection_reason",   "type": "STRING",    "mode": "NULLABLE"},
            {"name": "created_at",         "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "updated_at",         "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "deleted_at",         "type": "TIMESTAMP", "mode": "NULLABLE"},
        ],
        "table_columns": """
            application_id, application_number, customer_id, branch_id, handled_by,
            product_type_id, requested_amount, approved_amount, tenor_months, interest_rate,
            purpose, collateral_type, collateral_value, application_status,
            submitted_at, reviewed_at, decided_at, disbursed_at, rejection_reason,
            created_at, updated_at, deleted_at
        """,
    },

]

# ══════════════════════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS  — menerima table_cfg sebagai argumen, tidak ada global state
# ══════════════════════════════════════════════════════════════════════════════

def _derive_columns(table_cfg: dict) -> tuple[list[str], list[str]]:
    """
    Parse table_columns string → (_COLUMNS, _TRINO_COLUMNS).
    Fail-fast jika ada typo (kolom tidak ada di schema_fields).
    Dipanggil sekali saat DAG diparse.
    """
    schema_lookup: dict[str, str] = {
        f["name"]: f["type"] for f in table_cfg["schema_fields"]
    }
    columns: list[str] = []
    for col in table_cfg["table_columns"].replace("\n", ",").split(","):
        col = col.strip()
        if not col:
            continue
        if col not in schema_lookup:
            raise ValueError(
                f"[{table_cfg['bq_final_table']}] Kolom '{col}' tidak ada di schema_fields. "
                f"Periksa typo atau tambahkan kolom ke schema_fields."
            )
        columns.append(col)

    trino_columns: list[str] = [
        (
            f"CAST(src.{col} AS {_BQ_TO_TRINO_CAST[schema_lookup[col]]}) AS {col}"
            if schema_lookup[col] in _BQ_TO_TRINO_CAST
            else f"src.{col}"
        )
        for col in columns
    ]
    return columns, trino_columns


def _build_trino_insert_sql(table_cfg: dict, columns: list[str], trino_columns: list[str]) -> str:
    """
    Bangun Trino cross-catalog INSERT SQL untuk satu tabel.
    Window menggunakan data_interval_start/end yang dikonversi ke WIB.
    """
    bq_temp_table   = table_cfg["bq_final_table"] + "_temp_{{ ds_nodash }}"
    pg_source_table = table_cfg["pg_source_table"]
    partition_field = table_cfg["partition_field"]
    merge_key       = table_cfg["merge_key"]

    insert_cols = ",\n        ".join(columns)
    trino_exprs = ",\n            ".join(trino_columns)
    final_cols  = ", ".join(columns)

    return f"""
INSERT INTO {TRINO_BQ_CATALOG}.{BQ_DATASET}.{bq_temp_table} (
    {insert_cols}
)
WITH ranked AS (
    SELECT
        {trino_exprs},
        ROW_NUMBER() OVER (
            PARTITION BY src.{merge_key}
            ORDER BY src.{partition_field} DESC
        ) AS _rn
    FROM {TRINO_PG_CATALOG}.{PG_SCHEMA}.{pg_source_table} AS src
    WHERE src.{partition_field} >= TIMESTAMP '{{{{ dag_run.conf.get("window_start") or data_interval_start.in_timezone("Asia/Jakarta").strftime("%Y-%m-%d %H:%M:%S") }}}}'
      AND src.{partition_field} <  TIMESTAMP '{{{{ dag_run.conf.get("window_end")   or data_interval_end.in_timezone("Asia/Jakarta").strftime("%Y-%m-%d %H:%M:%S") }}}}'
)
SELECT {final_cols}
FROM   ranked
WHERE  _rn = 1
"""


def _build_bq_merge_query(table_cfg: dict, columns: list[str]) -> dict:
    """
    Bangun konfigurasi BigQuery MERGE job untuk satu tabel.
    """
    bq_final_table  = table_cfg["bq_final_table"]
    bq_temp_table   = bq_final_table + "_temp_{{ ds_nodash }}"
    partition_field = table_cfg["partition_field"]
    merge_key       = table_cfg["merge_key"]

    non_key_cols = [c for c in columns if c != merge_key]
    set_clause   = ",\n            ".join(f"T.{c} = S.{c}" for c in non_key_cols)
    ins_cols     = ", ".join(columns)
    ins_vals     = ", ".join(f"S.{c}" for c in columns)

    return {
        "query": {
            "query": f"""
                MERGE `{BQ_PROJECT}.{BQ_DATASET}.{bq_final_table}` AS T
                USING (
                    SELECT * EXCEPT(_rn)
                    FROM (
                        SELECT
                            *,
                            ROW_NUMBER() OVER (
                                PARTITION BY {merge_key}
                                ORDER BY {partition_field} DESC
                            ) AS _rn
                        FROM `{BQ_PROJECT}.{BQ_DATASET}.{bq_temp_table}`
                    )
                    WHERE _rn = 1
                ) AS S
                ON T.{merge_key} = S.{merge_key}

                WHEN MATCHED AND S.{partition_field} > T.{partition_field} THEN
                    UPDATE SET
                        {set_clause}

                WHEN NOT MATCHED BY TARGET THEN
                    INSERT ({ins_cols})
                    VALUES ({ins_vals})
            """,
            "useLegacySql": False,
            "defaultDataset": {
                "projectId": BQ_PROJECT,
                "datasetId": BQ_DATASET,
            },
        }
    }


def _make_sync_schema_callable(table_cfg: dict):
    """
    Factory yang mengembalikan callable untuk PythonOperator schema evolution.
    Menggunakan closure agar table_cfg tidak bocor antar tabel.
    """
    bq_final_table = table_cfg["bq_final_table"]

    def _sync_schema(ds_nodash: str, **kwargs) -> str:
        from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

        hook   = BigQueryHook(gcp_conn_id=GCP_CONN_ID)
        client = hook.get_client(project_id=BQ_PROJECT)

        temp_table_name = f"{bq_final_table}_temp_{ds_nodash}"
        temp_ref        = f"{BQ_PROJECT}.{BQ_DATASET}.{temp_table_name}"
        final_ref       = f"{BQ_PROJECT}.{BQ_DATASET}.{bq_final_table}"

        try:
            temp_table  = client.get_table(temp_ref)
            final_table = client.get_table(final_ref)
        except Exception as exc:
            raise RuntimeError(
                f"[{bq_final_table}] Gagal mengambil schema. "
                f"Pastikan '{temp_ref}' (step 2) dan '{final_ref}' sudah ada. "
                f"Detail: {exc}"
            ) from exc

        temp_fields  = {f.name: f for f in temp_table.schema}
        final_fields = {f.name for f in final_table.schema}
        new_cols     = [f for name, f in temp_fields.items() if name not in final_fields]

        if not new_cols:
            logging.info("[%s] ✅ Schema sudah sinkron — tidak ada kolom baru", bq_final_table)
            return "no_changes"

        for col in new_cols:
            alter_sql = (
                f"ALTER TABLE `{final_ref}` "
                f"ADD COLUMN IF NOT EXISTS `{col.name}` {col.field_type}"
            )
            logging.info("[%s] ⚙️  Menambah kolom: %s (%s)", bq_final_table, col.name, col.field_type)
            try:
                job = client.query(alter_sql)
                job.result(timeout=300)
            except Exception as exc:
                raise RuntimeError(
                    f"[{bq_final_table}] Schema evolution gagal untuk kolom '{col.name}'. "
                    f"Detail: {exc}"
                ) from exc

        added = [c.name for c in new_cols]
        logging.info("[%s] ✅ Kolom baru ditambahkan: %s", bq_final_table, added)
        return f"added:{','.join(added)}"

    return _sync_schema


# ══════════════════════════════════════════════════════════════════════════════
#  TASKGROUP FACTORY  — membuat 5 task untuk satu tabel
# ══════════════════════════════════════════════════════════════════════════════

def build_table_task_group(dag: DAG, table_cfg: dict) -> TaskGroup:
    """
    Buat TaskGroup berisi 5 step pipeline untuk satu tabel.

    TaskGroup ID  : ingest_<bq_final_table>
    Task IDs      : ingest_<table>.create_bq_temp_table
                    ingest_<table>.insert_postgres_to_bq_temp
                    ingest_<table>.sync_final_table_schema
                    ingest_<table>.merge_temp_to_final
                    ingest_<table>.drop_bq_temp_table
    """
    bq_final_table  = table_cfg["bq_final_table"]
    bq_temp_table   = bq_final_table + "_temp_{{ ds_nodash }}"
    partition_field = table_cfg["partition_field"]
    cluster_fields  = table_cfg["cluster_fields"]
    schema_fields   = table_cfg["schema_fields"]
    merge_key       = table_cfg["merge_key"]

    # Derive column lists — fail-fast saat DAG diparse
    columns, trino_columns = _derive_columns(table_cfg)

    # Assets untuk lineage
    bq_final_asset = Asset(f"bigquery://{BQ_PROJECT}/{BQ_DATASET}/{bq_final_table}")
    bq_temp_asset  = Asset(f"bigquery://{BQ_PROJECT}/{BQ_DATASET}/{bq_temp_table}")

    with TaskGroup(group_id=f"ingest_{bq_final_table}", dag=dag) as tg:

        # ── Step 1: CREATE <table>_temp via BigQuery API ─────────────────────
        create_bq_temp = BigQueryCreateTableOperator(
            task_id="create_bq_temp_table",
            gcp_conn_id=GCP_CONN_ID,
            project_id=BQ_PROJECT,
            dataset_id=BQ_DATASET,
            table_id=bq_temp_table,
            table_resource={
                "tableReference": {
                    "projectId": BQ_PROJECT,
                    "datasetId": BQ_DATASET
                },
                "schema":           {"fields": schema_fields},
                "timePartitioning": {"type": "MONTH", "field": partition_field},
                "clustering":       {"fields": cluster_fields},
            },
            if_exists="ignore",
            outlets=[bq_temp_asset],
            doc_md=f"Membuat BQ temp table `{bq_temp_table}` via BigQuery API.",
        )

        # ── Step 2: INSERT Postgres → <table>_temp via Trino ────────────────
        insert_to_bq_temp = SQLExecuteQueryOperator(
            task_id="insert_postgres_to_bq_temp",
            conn_id=TRINO_CONN_ID,
            sql=_build_trino_insert_sql(table_cfg, columns, trino_columns),
            autocommit=True,
            outlets=[bq_temp_asset],
            doc_md=f"Trino cross-catalog INSERT: `{PG_SCHEMA}.{table_cfg['pg_source_table']}` → `{bq_temp_table}`.",
        )

        # ── Step 3: Schema evolution ─────────────────────────────────────────
        sync_schema = PythonOperator(
            task_id="sync_final_table_schema",
            python_callable=_make_sync_schema_callable(table_cfg),
            doc_md=f"Schema evolution: kolom baru di temp → ALTER TABLE `{bq_final_table}`.",
        )

        # ── Step 4: MERGE <table>_temp → <table> ────────────────────────────
        merge_to_final = BigQueryInsertJobOperator(
            task_id="merge_temp_to_final",
            gcp_conn_id=GCP_CONN_ID,
            project_id=BQ_PROJECT,
            location=BQ_LOCATION,
            configuration=_build_bq_merge_query(table_cfg, columns),
            # job_id unik per tabel + ds_nodash → idempotent
            job_id=f"{{{{ dag.dag_id }}}}__{bq_final_table}__merge__{{{{ ds_nodash }}}}",
            force_rerun=True,
            deferrable=False,
            outlets=[bq_final_asset],
            doc_md=f"MERGE (UPSERT) `{bq_temp_table}` → `{bq_final_table}`.",
        )

        # ── Step 5: DROP <table>_temp ────────────────────────────────────────
        drop_bq_temp = BigQueryDeleteTableOperator(
            task_id="drop_bq_temp_table",
            gcp_conn_id=GCP_CONN_ID,
            deletion_dataset_table=f"{BQ_PROJECT}.{BQ_DATASET}.{bq_temp_table}",
            ignore_if_missing=True,
            trigger_rule=TriggerRule.ALL_DONE,   # cleanup meski MERGE gagal
            doc_md=f"Drop `{bq_temp_table}` — trigger_rule=ALL_DONE.",
        )

        # ── Pipeline order di dalam TaskGroup ────────────────────────────────
        create_bq_temp >> insert_to_bq_temp >> sync_schema >> merge_to_final >> drop_bq_temp

    return tg


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
    "execution_timeout":         timedelta(hours=2),  # lebih lama karena banyak tabel
}

with DAG(
    dag_id="postgres_to_bq_trino_multi_table",
    description="Postgres → BigQuery via Trino — multi-table dengan TaskGroup paralel",
    default_args=default_args,
    schedule=CronDataIntervalTimetable("0 0 1 * *", timezone="Asia/Jakarta"), #setiap awal bulan
    start_date=pendulum.datetime(2025, 1, 1, tz="Asia/Jakarta"),
    end_date=pendulum.datetime(2026, 1, 2, tz="Asia/Jakarta"),
    catchup=True,
    max_active_runs=1,
    tags=["postgres", "bigquery", "trino", "ingestion", "multi-table"],
    doc_md=__doc__,
) as dag:

    # Buat TaskGroup untuk setiap tabel di TABLES_CONFIG — berjalan PARALEL
    task_groups = [
        build_table_task_group(dag, table_cfg)
        for table_cfg in TABLES_CONFIG
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # OPTIONAL: jika ingin ada urutan antar tabel (sequential, bukan paralel),
    # uncomment baris berikut dan comment baris di atas:
    #
    # task_groups = [build_table_task_group(dag, cfg) for cfg in TABLES_CONFIG]
    # for i in range(len(task_groups) - 1):
    #     task_groups[i] >> task_groups[i + 1]
    # ─────────────────────────────────────────────────────────────────────────