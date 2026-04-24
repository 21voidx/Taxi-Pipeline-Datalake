"""
helpers/trino_helper.py
═══════════════════════════════════════════════════════════════════════════════
Gabungan utility + Airflow TaskGroup factory untuk pipeline:
    source database (PostgreSQL / MySQL / dll.) → BigQuery via Trino cross-catalog.

File ini terbagi menjadi DUA lapisan yang jelas dipisah oleh baris pembatas:

  LAPISAN 1 — Pure utilities  (baris ~60–430)
  ─────────────────────────────────────────────
  Semua fungsi SQL builder dan helper murni. Tidak bergantung pada Airflow
  runtime sehingga bisa di-import dan di-unit-test tanpa DAG context.

    • BQ_TO_TRINO_CAST          — peta type BQ → Trino CAST
    • METADATA_NAMES            — nama kolom pipeline metadata
    • build_metadata_exprs()    — SQL literal untuk _ingested_at / _source_system
    • parse_columns()           — parse & validasi TABLE_COLUMNS string
    • build_trino_columns()     — build SELECT expressions Trino dengan CAST
    • build_trino_insert_sql()  — build Trino cross-catalog INSERT SQL (CTE + window)
    • build_table_resource()    — build BQ table_resource dict (partitioning, labels)
    • build_bq_merge_query()    — build BQ MERGE job config dict
    • sync_final_table_schema() — schema evolution callable (PythonOperator)

  LAPISAN 2 — Airflow TaskGroup factory  (baris ~430–EOF)
  ─────────────────────────────────────────────────────────
  Import Airflow hanya di sini. Bergantung pada Lapisan 1 secara langsung
  (tidak ada circular import).

    • TableConfig               — dataclass config per tabel (fail-fast typo detection)
    • make_table_task_group()   — factory: 5-step TaskGroup siap pakai di DAG

Cara pakai di DAG:
    from helpers.trino_helper import TableConfig, make_table_task_group

    with DAG(...) as dag:
        groups = [make_table_task_group(cfg, **SHARED) for cfg in TABLE_CONFIGS]

Struktur TaskGroup per tabel:
    create_bq_temp_table
        → insert_source_to_bq_temp
            → sync_final_table_schema
                → merge_temp_to_final
                    → drop_bq_temp_table
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations
import logging


# ══════════════════════════════════════════════════════════════════════════════
#
#  LAPISAN 1 — PURE UTILITIES
#  Tidak ada import Airflow di blok ini.
#
# ══════════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
#  TYPE MAP  — Trino type mismatch antara source connector dan BQ connector
# ─────────────────────────────────────────────────────────────────────────────
#
#   BQ type     │ Trino(BQ)            │ Source type contoh                   │ Trino(source)    │ CAST ke
#   ────────────────────────────────────────────────────────────────────────────────────────────────────────
#   INT64       │ bigint               │ PG: INT4/SERIAL  | MySQL: INT        │ integer          │ BIGINT
#   STRING      │ varchar              │ PG: VARCHAR/UUID | MySQL: VARCHAR(N) │ varchar(N)       │ VARCHAR
#   BOOL        │ boolean              │ PG: BOOLEAN      | MySQL: TINYINT(1) │ boolean/tinyint  │ BOOLEAN
#   NUMERIC     │ decimal(38,9)        │ PG: NUMERIC(p,s) | MySQL: DECIMAL    │ decimal(p,s)     │ DECIMAL(38,9)
#   TIMESTAMP   │ timestamp(6) with tz │ PG: TIMESTAMPTZ  | MySQL: DATETIME   │ timestamp(0/6)   │ lihat build_trino_columns
#   DATE        │ date                 │ DATE                                 │ date             │ — ✓
#   FLOAT       │ double               │ PG: FLOAT8       | MySQL: DOUBLE     │ double           │ — ✓
#
#   Notes
#   ─────
#   • TIMESTAMP ditangani terpisah di build_trino_columns (perlu AT TIME ZONE).
#   • "INTEGER" dan "INT64" keduanya dimasukkan:
#       "INTEGER" untuk backward-compat schema_fields lama,
#       "INT64"   untuk BQ-native type name.
#   • BOOLEAN di-alias dari "BOOL" (BQ alias) maupun "BOOLEAN" (standard).

BQ_TO_TRINO_CAST: dict[str, str] = {
    # ── Integer / BigInt ──────────────────────────────────────────────────────
    "INTEGER":    "BIGINT",          # legacy BQ alias, kept for backward compat
    "INT64":      "BIGINT",          # BQ-native INT64 → source integer → CAST BIGINT

    # ── String ────────────────────────────────────────────────────────────────
    "STRING":     "VARCHAR",         # strip varchar(N) length suffix

    # ── Boolean ───────────────────────────────────────────────────────────────
    "BOOL":       "BOOLEAN",         # BQ alias → PG boolean / MySQL TINYINT(1)
    "BOOLEAN":    "BOOLEAN",         # standard name (defensive duplicate)

    # ── Numeric / Decimal ─────────────────────────────────────────────────────
    # BQ NUMERIC = DECIMAL(38,9); source DECIMAL(p,s) presisi bervariasi → normalise
    "NUMERIC":    "DECIMAL(38,9)",
    "BIGNUMERIC": "DECIMAL(38,9)",   # BQ BIGNUMERIC guard
}

# Trino type yang dipakai untuk semua kolom TIMESTAMP (ekspektasi sisi BQ)
_TRINO_TIMESTAMP_TYPE = "TIMESTAMP(6) WITH TIME ZONE"


# ─────────────────────────────────────────────────────────────────────────────
#  PIPELINE METADATA COLUMNS
# ─────────────────────────────────────────────────────────────────────────────
#  Kolom ini TIDAK berasal dari tabel sumber — tidak masuk TABLE_COLUMNS.
#  Diisi sebagai SQL literal di Trino SELECT, diinjeksi di tiga titik:
#    a. INSERT target list  → METADATA_NAMES
#    b. SELECT expressions  → build_metadata_exprs()  (literal SQL)
#    c. SELECT final CTE    → METADATA_NAMES
#  Dan juga di MERGE set_clause + ins_cols / ins_vals.
#
#  Fix yang diterapkan
#  ───────────────────
#  • CURRENT_TIMESTAMP di Trino → timestamp(3) with time zone,
#    sedangkan BQ mengharapkan timestamp(6) with time zone → perlu CAST eksplisit.
#  • String literal '{source_system}' diketik sebagai varchar(N) oleh Trino
#    tergantung panjang → CAST ke VARCHAR (tanpa length) agar cocok kolom BQ STRING.

METADATA_NAMES: list[str] = ["_ingested_at", "_source_system"]


def build_metadata_exprs(source_system: str) -> list[str]:
    """
    Generate literal SQL expressions untuk dua kolom metadata pipeline.

    Parameters
    ──────────
    source_system : str
        Identifier sistem sumber. Contoh: 'ride_ops_pg', 'ride_marketing_mysql'.

    Returns
    ───────
    list[str]
        Dua ekspresi SQL Trino siap pakai di SELECT:
          • CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6) WITH TIME ZONE) AS _ingested_at
          • CAST('{source_system}' AS VARCHAR)                     AS _source_system
    """
    return [
        f"CAST(CURRENT_TIMESTAMP AS {_TRINO_TIMESTAMP_TYPE}) AS _ingested_at",
        f"CAST('{source_system}' AS VARCHAR)                  AS _source_system",
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  COLUMN PARSING & CAST BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def parse_columns(table_columns_str: str, schema_lookup: dict[str, str]) -> list[str]:
    """
    Parse string TABLE_COLUMNS menjadi list nama kolom yang bersih.

    Fail-fast: raise ValueError jika ada kolom yang tidak ditemukan di
    SCHEMA_FIELDS, sehingga typo terdeteksi saat DAG di-load, bukan saat run.

    Parameters
    ──────────
    table_columns_str : str
        Multi-line string nama kolom dipisah koma. Whitespace dan baris kosong
        diabaikan.
    schema_lookup : dict[str, str]
        Dict {col_name: bq_type} yang dibangun dari SCHEMA_FIELDS.

    Returns
    ───────
    list[str]
        List nama kolom bersih sesuai urutan di table_columns_str.

    Raises
    ──────
    ValueError
        Jika ada nama kolom yang tidak ada di schema_lookup.
    """
    columns: list[str] = []
    for col in table_columns_str.replace("\n", ",").split(","):
        col = col.strip()
        if not col:
            continue
        if col not in schema_lookup:
            raise ValueError(
                f"Kolom '{col}' tidak ditemukan di SCHEMA_FIELDS. "
                f"Periksa typo atau tambahkan kolom ke SCHEMA_FIELDS terlebih dahulu."
            )
        columns.append(col)
    return columns


def build_trino_columns(
    columns      : list[str],
    schema_lookup: dict[str, str],
    json_columns : list[str] | None = None,
    source_tz    : str = "Asia/Jakarta",
) -> list[str]:
    """
    Build list Trino SELECT expressions dengan CAST yang tepat per tipe kolom.

    Urutan pemeriksaan per kolom:
    ─────────────────────────────
    1. JSON columns
           → json_format(src.col) AS col
             (menghindari INVALID_CAST_ARGUMENT saat kolom berisi JSONB/JSON object)

    2. TIMESTAMP / DATETIME
           → CAST(src.col AT TIME ZONE '{source_tz}' AS TIMESTAMP(6) WITH TIME ZONE) AS col
             Timestamp dari sumber (PG/MySQL) bisa naive atau ber-timezone berbeda;
             BQ connector mengharapkan timestamp(6) with time zone.
             AT TIME ZONE menambahkan offset, lalu CAST menaikkan presisi ke (6).

    3. Tipe yang butuh CAST eksplisit (INT64, BOOL, NUMERIC, STRING, dll.)
           → CAST(src.col AS {BQ_TO_TRINO_CAST[bq_type]}) AS col

    4. Tipe yang sudah cocok (DATE, FLOAT, DOUBLE, dll.)
           → src.col  (tanpa CAST)

    Parameters
    ──────────
    columns       : list[str]  — output dari parse_columns()
    schema_lookup : dict[str, str]  — {col_name: bq_type} dari SCHEMA_FIELDS
    json_columns  : list[str] | None  — nama kolom bertipe JSON di sumber (opsional)
    source_tz     : str  — timezone kolom timestamp di sumber (default "Asia/Jakarta")

    Returns
    ───────
    list[str]
        Satu expression Trino per kolom, panjang sama dengan `columns`.
    """
    if json_columns is None:
        json_columns = []

    result: list[str] = []
    for col in columns:
        bq_type = schema_lookup.get(col, "")

        # ── 1. Kolom JSON dari tabel sumber ───────────────────────────────────
        if col in json_columns:
            result.append(f"json_format(src.{col}) AS {col}")

        # ── 2. Timestamp — perlu AT TIME ZONE + presisi fix ───────────────────
        elif bq_type in ("TIMESTAMP", "DATETIME"):
            result.append(
                f"CAST(src.{col} AT TIME ZONE '{source_tz}'"
                f" AS {_TRINO_TIMESTAMP_TYPE}) AS {col}"
            )

        # ── 3. Tipe lain yang butuh CAST eksplisit ────────────────────────────
        elif bq_type in BQ_TO_TRINO_CAST:
            trino_type = BQ_TO_TRINO_CAST[bq_type]
            result.append(f"CAST(src.{col} AS {trino_type}) AS {col}")

        # ── 4. Tipe sudah cocok (DATE, FLOAT, DOUBLE, dll.) ───────────────────
        else:
            result.append(f"src.{col}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  SQL BUILDER — Trino cross-catalog INSERT
# ─────────────────────────────────────────────────────────────────────────────

def build_trino_insert_sql(
    *,
    trino_bq_catalog : str,
    trino_pg_catalog : str,
    bq_dataset       : str,
    bq_temp_table    : str,
    pg_schema        : str,
    pg_source_table  : str,
    merge_key        : str,
    partition_field  : str,
    columns          : list[str],
    trino_columns    : list[str],
    metadata_exprs   : list[str],
    source_tz        : str = "Asia/Jakarta",
) -> str:
    """
    Build Trino cross-catalog INSERT SQL lengkap dengan:

      • CTE ranked      — ROW_NUMBER() OVER (PARTITION BY merge_key) dedup,
                          hanya baris terbaru (ORDER BY partition_field DESC) yang diambil.
      • Window filter   — data_interval_start/end (Airflow UTC) dikonversi ke source_tz
                          sebelum dibandingkan dengan kolom partition_field di sumber.
                          Kenapa? Airflow menyimpan interval dalam UTC. Jika kolom sumber
                          dalam WIB (+0700), literal UTC akan menggeser window dan melewatkan data.
      • Manual override — dag_run.conf["window_start"] / ["window_end"] (nilai dalam source_tz)
                          memungkinkan backfill ad-hoc tanpa harus mengubah jadwal DAG.
      • Metadata exprs  — CURRENT_TIMESTAMP + source_system literal diinjeksi di SELECT.

    Parameters
    ──────────
    trino_bq_catalog, trino_pg_catalog : str
        Nama catalog Trino untuk BQ (tujuan) dan sumber (PG/MySQL).
    bq_dataset, bq_temp_table : str
        Target INSERT di BQ side Trino.
    pg_schema, pg_source_table : str
        Sumber di source side Trino.
    merge_key, partition_field : str
        Dipakai untuk dedup (ROW_NUMBER PARTITION BY) dan window filter (WHERE).
    columns : list[str]
        Output parse_columns() — nama kolom tanpa metadata.
    trino_columns : list[str]
        Output build_trino_columns() — SELECT expressions dengan CAST.
    metadata_exprs : list[str]
        Output build_metadata_exprs() — literal SQL untuk _ingested_at/_source_system.
    source_tz : str
        Timezone sumber; dipakai di AT TIME ZONE dan konversi window.

    Returns
    ───────
    str
        SQL string siap diteruskan ke SQLExecuteQueryOperator (berisi Airflow template).
    """
    all_names  = columns + METADATA_NAMES
    all_exprs  = trino_columns + metadata_exprs

    insert_cols = ",\n        ".join(all_names)
    trino_exprs = ",\n            ".join(all_exprs)
    final_cols  = ", ".join(all_names)

    return f"""
INSERT INTO {trino_bq_catalog}.{bq_dataset}.{bq_temp_table} (
    {insert_cols}
)
WITH ranked AS (
    SELECT
        {trino_exprs},
        ROW_NUMBER() OVER (
            PARTITION BY src.{merge_key}
            ORDER BY src.{partition_field} DESC
        ) AS _rn
    FROM {trino_pg_catalog}.{pg_schema}.{pg_source_table} AS src
    WHERE src.{partition_field} >= TIMESTAMP '{{{{ dag_run.conf.get("window_start") or data_interval_start.in_timezone("{source_tz}").strftime("%Y-%m-%d %H:%M:%S") }}}}'
      AND src.{partition_field} <  TIMESTAMP '{{{{ dag_run.conf.get("window_end")   or data_interval_end.in_timezone("{source_tz}").strftime("%Y-%m-%d %H:%M:%S") }}}}'
)
SELECT {final_cols}
FROM   ranked
WHERE  _rn = 1
"""


# ─────────────────────────────────────────────────────────────────────────────
#  TABLE RESOURCE BUILDER — BigQuery REST API format
# ─────────────────────────────────────────────────────────────────────────────

def build_table_resource(
    *,
    bq_project      : str,
    bq_dataset      : str,
    table_id        : str,
    schema_fields   : list[dict],
    partition_field : str,
    partition_type  : str = "MONTH",
    cluster_fields  : list[str] | None = None,
    labels          : dict[str, str] | None = None,
) -> dict:
    """
    Build BQ table_resource dict untuk BigQueryCreateTableOperator.

    Kenapa tidak pakai Trino DDL?
    ──────────────────────────────
    Trino BigQuery connector tidak support WITH (partitioning=..., clustering_key=...).
    Property itu milik Iceberg/Hive connector. BQ REST API (via table_resource) adalah
    satu-satunya cara set timePartitioning + clustering secara programatik dari Airflow.

    Parameters
    ──────────
    partition_type  : "DAY" | "MONTH" | "YEAR"  (default "MONTH")
    cluster_fields  : list kolom untuk clustering BQ, maksimal 4 kolom.
    labels          : dict resource-level labels untuk BQ billing.
                      Muncul di billing export kolom `resource_labels` dan di
                      INFORMATION_SCHEMA.TABLE_OPTIONS.option_value.
                      Key/value: lowercase, maks 63 karakter, hanya [a-z0-9_-].

    Returns
    ───────
    dict
        table_resource dict sesuai format BQ REST API Tables.insert.
    """
    resource: dict = {
        "tableReference": {
            "projectId": bq_project,
            "datasetId": bq_dataset,
            "tableId":   table_id,
        },
        "schema": {
            "fields": schema_fields,
        },
        "timePartitioning": {
            "type":  partition_type,
            "field": partition_field,
        },
    }
    if cluster_fields:
        resource["clustering"] = {"fields": cluster_fields}
    if labels:
        resource["labels"] = labels
    return resource


# ─────────────────────────────────────────────────────────────────────────────
#  SQL BUILDER — BigQuery MERGE
# ─────────────────────────────────────────────────────────────────────────────

def build_bq_merge_query(
    *,
    bq_project      : str,
    bq_dataset      : str,
    bq_final_table  : str,
    bq_temp_table   : str,
    merge_key       : str,
    partition_field : str,
    columns         : list[str],
    append_only     : bool = False,
    job_labels      : dict[str, str] | None = None,
) -> dict:
    """
    Build BigQuery MERGE job configuration dict untuk BigQueryInsertJobOperator.

    Dua mode:
    ─────────
    append_only = False  (default) — tabel mutable (customers, trips, payments)
      • WHEN MATCHED AND incoming lebih baru → UPDATE SET semua kolom non-key
      • WHEN NOT MATCHED                     → INSERT baris baru

    append_only = True — tabel immutable / event log (trip_status_logs, dll.)
      • WHEN MATCHED                         → skip (tidak ada UPDATE clause)
      • WHEN NOT MATCHED                     → INSERT baris baru
      Cocok untuk audit trail yang immutable by design.

    ROW_NUMBER() guard di dalam MERGE USING → safety net dedup di kedua mode,
    sehingga MERGE aman meski ada duplikat di temp table.

    Parameters
    ──────────
    job_labels : dict[str, str] | None
        Job-level labels yang ditempelkan pada BQ query job (MERGE statement).
        Muncul di INFORMATION_SCHEMA.JOBS_BY_PROJECT.labels dan billing export
        — sangat berguna untuk analisis biaya per tabel, per DAG, per environment.
        Key/value: lowercase, maks 63 karakter, hanya [a-z0-9_-].
        Contoh: {"env": "prod", "team": "data-eng", "table": "rides", "dag-id": "..."}

    Catatan teknis:
        writeDisposition, createDisposition, schemaUpdateOptions TIDAK dipakai
        untuk DML statement → akan menyebabkan BQ error 400 jika disertakan.
        labels ditempatkan di top-level configuration dict (bukan di dalam "query")
        — ini level yang diindeks oleh BQ billing export.

    Returns
    ───────
    dict
        configuration dict sesuai format BigQueryInsertJobOperator.
    """
    all_cols = columns + METADATA_NAMES
    ins_cols = ", ".join(all_cols)
    ins_vals = ", ".join(f"S.{c}" for c in all_cols)

    if append_only:
        # Tabel immutable — WHEN MATCHED tidak dibuat sama sekali.
        when_matched_clause = ""
    else:
        # Tabel mutable — UPDATE hanya jika data yang masuk lebih baru dari yang ada.
        non_key_cols = [c for c in columns if c != merge_key]
        all_non_key  = non_key_cols + METADATA_NAMES
        set_clause   = ",\n                        ".join(
            f"T.{c} = S.{c}" for c in all_non_key
        )
        when_matched_clause = f"""
                WHEN MATCHED AND S.{partition_field} > T.{partition_field} THEN
                    UPDATE SET
                        {set_clause}
"""

    config: dict = {
        "query": {
            "query": f"""
                MERGE `{bq_project}.{bq_dataset}.{bq_final_table}` AS T
                USING (
                    SELECT * EXCEPT(_rn)
                    FROM (
                        SELECT
                            *,
                            ROW_NUMBER() OVER (
                                PARTITION BY {merge_key}
                                ORDER BY {partition_field} DESC
                            ) AS _rn
                        FROM `{bq_project}.{bq_dataset}.{bq_temp_table}`
                    )
                    WHERE _rn = 1
                ) AS S
                ON T.{merge_key} = S.{merge_key}
{when_matched_clause}
                WHEN NOT MATCHED BY TARGET THEN
                    INSERT ({ins_cols})
                    VALUES ({ins_vals})

                -- WHEN NOT MATCHED BY SOURCE THEN DELETE  ← aktifkan jika butuh hard-delete
            """,
            "useLegacySql": False,
            "defaultDataset": {
                "projectId": bq_project,
                "datasetId": bq_dataset,
            },
        }
    }

    # Job-level labels di top-level configuration (bukan di dalam "query").
    # Ini yang diindeks BQ billing export dan INFORMATION_SCHEMA.JOBS_BY_PROJECT.labels.
    # Jangan taruh di dalam "query" dict — BQ akan menolak dengan error 400.
    if job_labels:
        config["labels"] = job_labels
    return config


# ─────────────────────────────────────────────────────────────────────────────
#  SCHEMA EVOLUTION — callable untuk PythonOperator
# ─────────────────────────────────────────────────────────────────────────────

def sync_final_table_schema(
    ds_nodash       : str,
    gcp_conn_id     : str,
    bq_project      : str,
    bq_dataset      : str,
    bq_final_table  : str,
    **kwargs,
) -> str:
    """
    Schema evolution: deteksi kolom baru di temp table, lalu ALTER TABLE final
    untuk menambahkan kolom tersebut (ADD COLUMN IF NOT EXISTS).

    Dipanggil via PythonOperator — ds_nodash diterima otomatis dari Airflow
    context injection; semua parameter lain diteruskan via op_kwargs.

    Kenapa PythonOperator dan bukan schemaUpdateOptions di MERGE?
    ─────────────────────────────────────────────────────────────
    schemaUpdateOptions (ALLOW_FIELD_ADDITION) hanya valid untuk LOAD dan
    INSERT SELECT — tidak valid untuk MERGE / DML statement.
    Solusi: bandingkan schema temp table (cerminan sumber hari ini) dengan
    final table, lalu ALTER TABLE ADD COLUMN sebelum MERGE dijalankan.

    Batasan (by design):
      • Hanya menangani ADD COLUMN — aman dan tidak destruktif.
      • DROP COLUMN / perubahan tipe memerlukan DBA sign-off sesuai compliance.

    Idempotency:
      IF NOT EXISTS → aman di-retry tanpa error "duplicate column".

    Parameters
    ──────────
    ds_nodash       : str   — tanggal run tanpa dash (contoh "20260329"), dari Airflow context
    gcp_conn_id     : str   — Airflow Connection ID GCP
    bq_project      : str   — BQ project ID
    bq_dataset      : str   — BQ dataset ID
    bq_final_table  : str   — nama tabel final BQ (tanpa project/dataset prefix)

    Returns
    ───────
    str
        "no_changes"              jika tidak ada kolom baru, atau
        "added:col1,col2,..."     jika ada kolom yang ditambahkan.

    Raises
    ──────
    RuntimeError
        Jika tabel tidak dapat diakses atau ALTER TABLE gagal.
    """
    from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

    hook   = BigQueryHook(gcp_conn_id=gcp_conn_id)
    client = hook.get_client(project_id=bq_project)

    temp_ref  = f"{bq_project}.{bq_dataset}.{bq_final_table}_temp_{ds_nodash}"
    final_ref = f"{bq_project}.{bq_dataset}.{bq_final_table}"

    try:
        temp_table  = client.get_table(temp_ref)
        final_table = client.get_table(final_ref)
    except Exception as exc:
        raise RuntimeError(
            f"Gagal mengambil schema tabel. "
            f"Pastikan '{temp_ref}' (step 2) dan '{final_ref}' sudah ada. "
            f"Detail: {exc}"
        ) from exc

    temp_fields  = {f.name: f for f in temp_table.schema}
    final_fields = {f.name for f in final_table.schema}
    new_cols     = [f for name, f in temp_fields.items() if name not in final_fields]

    if not new_cols:
        logging.info("✅ Schema sudah sinkron — tidak ada kolom baru")
        return "no_changes"

    for col in new_cols:
        alter_sql = (
            f"ALTER TABLE `{final_ref}` "
            f"ADD COLUMN IF NOT EXISTS `{col.name}` {col.field_type}"
        )
        logging.info("⚙️  Menambah kolom: %s (%s)", col.name, col.field_type)
        try:
            job = client.query(alter_sql)
            job.result(timeout=300)
        except Exception as exc:
            raise RuntimeError(
                f"Schema evolution gagal untuk kolom '{col.name}' (type: {col.field_type}). "
                f"Kemungkinan penyebab: type conflict atau insufficient permission. "
                f"Detail: {exc}"
            ) from exc

    added = [c.name for c in new_cols]
    logging.info("✅ Schema evolution selesai — kolom baru ditambahkan: %s", added)
    return f"added:{','.join(added)}"


# ══════════════════════════════════════════════════════════════════════════════
#
#  LAPISAN 2 — AIRFLOW TASK GROUP FACTORY
#  Import Airflow hanya di blok ini, agar Lapisan 1 tetap bisa di-test mandiri.
#
# ══════════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field

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


# ─────────────────────────────────────────────────────────────────────────────
#  TableConfig — dataclass config per tabel
# ─────────────────────────────────────────────────────────────────────────────
#
#  Gunakan dataclass ini (bukan plain dict) agar typo pada key terdeteksi
#  saat import DAG — fail di parse time, bukan saat task berjalan.
#
#  Contoh pemakaian:
#      from helpers.trino_helper import TableConfig, make_table_task_group
#
#      cfg = TableConfig(
#          pg_table        = "payments",
#          bq_final_table  = "payments",
#          merge_key       = "payment_id",
#          partition_field = "created_at",
#          source_system   = "ride_ops_pg",
#          schema_fields   = [...],
#          table_columns   = "payment_id, amount, ...",
#      )

@dataclass
class TableConfig:
    pg_table        : str
    bq_final_table  : str
    merge_key       : str
    partition_field : str
    schema_fields   : list[dict]
    table_columns   : str
    source_system   : str            = "source"
    partition_type  : str            = "MONTH"     # DAY | MONTH | YEAR
    cluster_fields  : list[str]      = field(default_factory=list)
    append_only     : bool           = False
    json_fields     : list[str]      = field(default_factory=list)
    labels          : dict[str, str] = field(default_factory=lambda: {"env": "dev", "team": "data-eng"})


# ─────────────────────────────────────────────────────────────────────────────
#  make_table_task_group — factory utama
# ─────────────────────────────────────────────────────────────────────────────

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
    dag_labels      : dict[str, str] | None = None,
) -> TaskGroup:
    """
    Buat Airflow TaskGroup berisi 5 task untuk satu tabel sumber.
    Harus dipanggil dari dalam konteks ``with DAG(...) as dag:``.

    5 Task dalam grup (berjalan berurutan):
    ────────────────────────────────────────
    1. create_bq_temp_table      — CREATE temp table via BigQuery API
    2. insert_source_to_bq_temp  — INSERT source → temp via Trino cross-catalog
    3. sync_final_table_schema   — Schema evolution (ADD COLUMN IF NOT EXISTS)
    4. merge_temp_to_final       — MERGE temp → final (UPSERT atau append-only)
    5. drop_bq_temp_table        — DROP temp (trigger_rule=ALL_DONE, selalu berjalan)

    Parameters
    ──────────
    cfg : TableConfig | dict
        Konfigurasi tabel. Bisa berupa TableConfig dataclass atau plain dict
        dengan key yang sama (akan otomatis dikonversi ke TableConfig).

    bq_project, bq_dataset, bq_location : str
        Koordinat BigQuery tujuan.

    pg_schema : str
        Schema / database sumber.
        Contoh: "public" untuk PostgreSQL, nama database untuk MySQL.

    trino_conn_id, gcp_conn_id : str
        Airflow Connection ID untuk Trino dan GCP.

    trino_bq_cat, trino_pg_cat : str
        Nama catalog Trino untuk BigQuery (tujuan) dan sumber (PG/MySQL/dll.).

    source_tz : str
        Timezone kolom timestamp di tabel sumber (default "Asia/Jakarta").
        Dipakai untuk:
          1. AT TIME ZONE cast pada kolom TIMESTAMP di Trino SELECT
             (timestamp naive dari sumber → timestamp(6) with time zone untuk BQ).
          2. Konversi window UTC → local pada Trino WHERE filter.

    dag_labels : dict[str, str] | None
        Label tingkat DAG yang diwarisi oleh semua tabel.
        Digabung dengan label per-tabel dari TableConfig.labels.
        Contoh: {"env": "prod", "team": "data-eng", "dag-id": "...", "layer": "bronze"}

        Urutan merge (nilai kanan override nilai kiri):
            dag_labels  →  auto-derived (table, source-system)  →  TableConfig.labels

        Semua key/value harus: lowercase, maks 63 karakter, hanya [a-z0-9_-].
        Label ini muncul di:
          • BQ Table OPTIONS  (resource_labels billing export)
          • INFORMATION_SCHEMA.JOBS_BY_PROJECT.labels  (job-level, per query)

    Returns
    ───────
    TaskGroup
        Airflow TaskGroup siap pakai. Bisa langsung disambung dengan ``>>``
        ke TaskGroup lain jika urutan antar tabel diperlukan.

    Raises
    ──────
    ValueError
        Jika ada nama kolom di table_columns yang tidak ada di schema_fields.
        Fail-fast saat DAG di-load — error muncul sebelum pipeline berjalan.
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
    table_labels    = cfg.labels

    # ── Effective labels: gabungan dag_labels + auto-derived + per-tabel ──────
    # Urutan merge (nilai kanan override nilai kiri):
    #   dag_labels  →  auto-derived (table, source-system)  →  table_labels
    # Ini memastikan per-tabel config bisa override dag-level jika diperlukan.
    def _norm_label(v: str) -> str:
        """Normalisasi value sesuai BQ label constraint: lowercase, max 63 chars, only [a-z0-9_-]."""
        return v.lower().replace("_", "-")[:63]

    effective_labels: dict[str, str] = {
        **(dag_labels or {}),
        "table":         _norm_label(pg_table),
        "source-system": _norm_label(source_system),
        **table_labels,                              # per-tabel bisa override dag_labels
    }

    # Template BQ temp table — unik per tabel per run date
    bq_temp_table = f"{bq_final_table}_temp_{{{{ ds_nodash }}}}"

    # ── Pre-compute column metadata — fail-fast sebelum DAG berjalan ──────────
    _schema_lookup  = {f["name"]: f["type"] for f in schema_fields}
    _columns        = parse_columns(table_columns, _schema_lookup)
    _trino_columns  = build_trino_columns(
        _columns,
        _schema_lookup,
        json_columns=json_fields,
        source_tz=source_tz,
    )
    _metadata_exprs = build_metadata_exprs(source_system)

    # ── Asset lineage (Airflow 3.x data-aware scheduling) ─────────────────────
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
                labels=effective_labels,             # resource-level labels untuk billing
            ),
            if_exists="ignore",
            outlets=[bq_temp_asset],
        )

        # ── Step 2: INSERT source → temp via Trino cross-catalog ──────────────
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

        # ── Step 4: MERGE temp → final (UPSERT atau append-only) ─────────────
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
                job_labels=effective_labels,         # job-level labels untuk billing per query
            ),
            job_id=f"{{{{ dag.dag_id }}}}__merge__{pg_table}__{{{{ ds_nodash }}}}",
            force_rerun=True,
            deferrable=False,
            outlets=[bq_final_asset],
        )

        # ── Step 5: DROP temp — selalu jalan meski step sebelumnya gagal ──────
        drop_temp = BigQueryDeleteTableOperator(
            task_id="drop_bq_temp_table",
            gcp_conn_id=gcp_conn_id,
            deletion_dataset_table=(
                f"{bq_project}.{bq_dataset}.{bq_temp_table}"
            ),
            ignore_if_missing=True,
            trigger_rule=TriggerRule.ALL_DONE,       # cleanup tetap berjalan meski MERGE gagal
        )

        # ── Dependency antar task dalam group ─────────────────────────────────
        create_temp >> insert_to_temp >> sync_schema >> merge_to_final >> drop_temp

    return tg