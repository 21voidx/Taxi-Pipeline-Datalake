"""
DAG : postgres_to_bq_trino_dag
Airflow : 3.x
Engine  : Trino (federation layer — connects Postgres + BigQuery catalogs)

Pipeline
────────
1. [BigQueryCreateTableOperator]  Buat branches_temp di BQ via BQ API
                                  (partitioned MONTH(updated_at) + clustered, if_exists=ignore)
2. [SQLExecuteQueryOperator]      INSERT postgresql.public.branches → branches_temp via Trino
                                  (cross-catalog, window = data_interval_start s/d data_interval_end, dedup ROW_NUMBER)
3. [PythonOperator]               Schema evolution — bandingkan temp vs final,
                                  ALTER TABLE branches ADD COLUMN IF NOT EXISTS kolom baru
4. [BigQueryInsertJobOperator]    MERGE branches_temp → branches (UPSERT + guard dedup)
5. [BigQueryDeleteTableOperator]  DROP branches_temp (trigger_rule=all_done)

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
  writeDisposition dan createDisposition juga tidak valid untuk DML statements.
  Solusi: deteksi kolom baru dari temp table (cerminan Postgres hari ini),
  lalu ALTER TABLE final ADD COLUMN IF NOT EXISTS sebelum MERGE dijalankan.

Kenapa BigQueryInsertJobOperator untuk step 4 (bukan Trino)?
─────────────────────────────────────────────────────────────
  MERGE adalah BQ Standard SQL — lebih efisien dijalankan native di BQ engine.

Best practices applied
──────────────────────
  time_partitioning  — MONTH(updated_at) di temp table via table_resource
  cluster_fields     — province, branch_type, is_active
  schema_evolution   — PythonOperator ALTER TABLE sebelum MERGE (incremental, tidak full refresh)
  date_window        — {{ data_interval_start }} s/d {{ data_interval_end }} (tepat 10 menit per run)
                       manual trigger → window kosong, pakai logical_date untuk override
  dedup              — ROW_NUMBER() OVER (PARTITION BY branch_id ORDER BY updated_at DESC)
  idempotency        — if_exists=ignore (step 1), IF NOT EXISTS (step 3), job_id template (step 4)
  lineage            — outlets = Asset(...) di setiap step
  cleanup            — trigger_rule=all_done pada DROP temp (jalan meski MERGE gagal)

Catatan penting
───────────────
  writeDisposition, createDisposition, schemaUpdateOptions — TIDAK dipakai di MERGE.
  Ketiganya hanya valid untuk job dengan destinationTable (LOAD / INSERT SELECT).
  Menggunakannya pada DML statement akan menyebabkan error BQ 400.

Connections (Airflow UI → Admin → Connections)
───────────────────────────────────────────────
  trino_default        : Trino  (Conn Type=trino, host=banking-trino, port=8080, login=trino)
  google_cloud_default : GCP service account dengan role BigQuery Data Editor + Job User
"""

from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.sdk.definitions.asset import Asset                          # Airflow 3.x Asset
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.providers.google.cloud.operators.bigquery import (
    BigQueryCreateTableOperator,
    BigQueryDeleteTableOperator,
    BigQueryInsertJobOperator,
)
from airflow.timetables.interval import CronDataIntervalTimetable
from airflow.utils.trigger_rule import TriggerRule
# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG  — change these to match your environment
# ══════════════════════════════════════════════════════════════════════════════

TRINO_CONN_ID    = "trino_default"
GCP_CONN_ID      = "google_cloud_default"

# Trino catalog names (as configured in trino/catalog/*.properties)
TRINO_BQ_CATALOG = "bigquery"           # bigquery.properties connector
TRINO_PG_CATALOG = "postgresql"         # postgresql.properties connector

BQ_PROJECT      = "banking-modernstack"  # your GCP project ID
BQ_DATASET      = "raw_core_banking"
BQ_LOCATION     = "US"

# Source (Postgres via Trino postgresql catalog)
PG_SCHEMA       = "public"
PG_SOURCE_TABLE = "customers"

# Target
BQ_FINAL_TABLE  = "customers"
BQ_TEMP_TABLE   = BQ_FINAL_TABLE + "_temp_{{ ds_nodash }}"

# Partition / cluster
PARTITION_FIELD = "updated_at"          # TIMESTAMP / DATE column
CLUSTER_FIELDS  = ["address_city", "customer_segment", "kyc_status"] # up to 4 fields
APPEND_ONLY     = False 
# Surrogate primary key for MERGE (can be composite — see MERGE sql below)
MERGE_KEY       = "customer_id"

# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL TYPE MAP — tidak perlu diubah saat ganti tabel
# ══════════════════════════════════════════════════════════════════════════════
#
#  Root cause TYPE_MISMATCH: Trino memetakan tipe PG dan BQ secara berbeda.
#
#    BQ type    | Trino(BQ) | PG type contoh          | Trino(PG)      | CAST ke
#    ───────────────────────────────────────────────────────────────────────────
#    INTEGER    | bigint    | SERIAL, INT4, INT        | integer        | BIGINT
#    STRING     | varchar   | VARCHAR(N), CHAR(N), UUID| varchar(N)/uuid| VARCHAR
#    DATE       | date      | DATE                    | date           | — ✓
#    TIMESTAMP  | timestamp | TIMESTAMPTZ             | timestamp tz   | — ✓
#    BOOLEAN    | boolean   | BOOLEAN                 | boolean        | — ✓
#    FLOAT      | double    | FLOAT8                  | double         | — ✓
#    NUMERIC    | decimal   | NUMERIC                 | decimal        | — ✓
#
#  Solusi: satu map global BQ type → Trino CAST yang perlu dipaksa.
#  Semua kolom bertipe INTEGER di BQ selalu butuh BIGINT,
#  semua kolom bertipe STRING di BQ selalu butuh VARCHAR — tanpa terkecuali.
#  Cast redundan (varchar → VARCHAR) aman di Trino, tidak error.

_BQ_TO_TRINO_CAST: dict[str, str] = {
    "INTEGER": "BIGINT",   # BQ INT64  ↔ PG INT4/SERIAL  → Trino bigint  vs integer
    "STRING":  "VARCHAR",  # BQ STRING ↔ PG VARCHAR/CHAR/UUID → Trino varchar(unbounded) vs varchar(N)/char/uuid
    # Tipe di bawah sudah cocok antara Trino(PG) dan Trino(BQ) — tidak perlu CAST:
    # "DATE":      sudah date  = date
    # "TIMESTAMP": sudah timestamp with time zone
    # "BOOLEAN":   sudah boolean
    # "FLOAT":     sudah double
    # "NUMERIC":   sudah decimal
}

# ══════════════════════════════════════════════════════════════════════════════
#  SCHEMA — ubah ini saat ganti tabel (name, type, mode saja)
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA_FIELDS = [
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
]

# ══════════════════════════════════════════════════════════════════════════════
#  TABLE_COLUMNS — paste kolom di sini saat ganti tabel, pisahkan dengan koma
# ══════════════════════════════════════════════════════════════════════════════
#
#  Cara pakai:
#    1. Jalankan di psql: \d <table_name>  — copy nama kolom dari output.
#    2. Atau ambil dari CREATE TABLE statement, baris "name TYPE MODE".
#    3. Paste di bawah sebagai string — urutan bebas, spasi/newline diabaikan.
#
#  Kolom yang tidak ada di SCHEMA_FIELDS akan menyebabkan KeyError saat parse,
#  sehingga typo terdeteksi lebih awal (fail-fast) sebelum DAG dijalankan.

TABLE_COLUMNS = """
    customer_id, customer_uuid, full_name, national_id, date_of_birth, gender,
    marital_status, occupation, income_range, email, phone_primary, phone_secondary,
    address_street, address_city, address_province, address_postal_code,
    customer_segment, kyc_status, kyc_verified_at, onboarding_branch_id,
    acquisition_channel, risk_rating, is_politically_exposed,
    created_at, updated_at, deleted_at
"""

# ══════════════════════════════════════════════════════════════════════════════
#  AUTO-DERIVE — tidak perlu diubah
# ══════════════════════════════════════════════════════════════════════════════

# Lookup: nama kolom → BQ type  (dipakai untuk resolve cast dari TABLE_COLUMNS)
_SCHEMA_LOOKUP: dict[str, str] = {f["name"]: f["type"] for f in SCHEMA_FIELDS}

# Parse TABLE_COLUMNS → list nama kolom bersih, fail-fast jika ada typo
_COLUMNS: list[str] = []
for _col in TABLE_COLUMNS.replace("\n", ",").split(","):
    _col = _col.strip()
    if not _col:
        continue
    if _col not in _SCHEMA_LOOKUP:
        raise ValueError(
            f"Kolom '{_col}' tidak ditemukan di SCHEMA_FIELDS. "
            f"Periksa typo atau tambahkan kolom ke SCHEMA_FIELDS terlebih dahulu."
        )
    _COLUMNS.append(_col)

# Loop BQ  — plain column names: INSERT target, MERGE clause, SELECT dari CTE
# Loop Trino — CAST expression: SELECT dari Postgres di CTE ranked
#   Ada di _BQ_TO_TRINO_CAST → CAST(src.col AS TYPE) AS col   (alias wajib)
#   Tidak ada                 → src.col                        (tipe sudah cocok)
_TRINO_COLUMNS: list[str] = [
    (
        f"CAST(src.{col} AS {_BQ_TO_TRINO_CAST[_SCHEMA_LOOKUP[col]]}) AS {col}"
        if _SCHEMA_LOOKUP[col] in _BQ_TO_TRINO_CAST
        else f"src.{col}"
    )
    for col in _COLUMNS
]

# ══════════════════════════════════════════════════════════════════════════════
#  AIRFLOW 3.x — Assets (Outlets) for lineage + dataset-triggered DAGs
# ══════════════════════════════════════════════════════════════════════════════

BQ_FINAL_ASSET = Asset(
    f"bigquery://{BQ_PROJECT}/{BQ_DATASET}/{BQ_FINAL_TABLE}"
)
BQ_TEMP_ASSET  = Asset(
    f"bigquery://{BQ_PROJECT}/{BQ_DATASET}/{BQ_TEMP_TABLE}"
)

# ══════════════════════════════════════════════════════════════════════════════
#  SQL BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _trino_insert_sql() -> str:
    """
    Trino cross-catalog INSERT: postgresql.public.branches → bigquery.raw_core_banking.branches_temp

    Window: data_interval_start/end dikonversi ke WIB sebelum masuk SQL literal.

    Kenapa perlu .in_timezone("Asia/Jakarta")?
    ──────────────────────────────────────────
    Kolom updated_at di Postgres disimpan dalam WIB (+0700).
    Airflow menyimpan data_interval dalam UTC → "0 9 * * * WIB" = "02:00 UTC".

    Tanpa konversi (bug):
      Literal '2026-03-11 02:00:00' masuk SQL
      Trino baca sebagai WIB → window = 02:00 WIB kemarin s/d 02:00 WIB hari ini
      → data jam 02:00–09:00 WIB TERLEWAT

    Dengan .in_timezone("Asia/Jakarta") (fix):
      2026-03-11 02:00:00 UTC → 2026-03-11 09:00:00 WIB
      Literal '2026-03-11 09:00:00' masuk SQL
      → window = 09:00 WIB kemarin s/d 09:00 WIB hari ini ✅ sesuai schedule

    Override via dag_run.conf (nilai harus dalam WIB):
      { "window_start": "2026-03-11 09:00:00", "window_end": "2026-03-12 09:00:00" }
    """
    # Loop 1 — INSERT target: plain column names (BigQuery side)
    insert_cols  = ",\n        ".join(_COLUMNS)

    # Loop 2 — SELECT source: Trino CAST expressions (Postgres side)
    # Setiap ekspresi sudah memiliki alias eksplisit (CAST(... AS T) AS col / src.col)
    # sehingga nama kolom di CTE ranked = nama kolom plain (_COLUMNS).
    trino_exprs  = ",\n            ".join(_TRINO_COLUMNS)

    # SELECT final dari CTE: cukup nama kolom plain (tanpa prefix src.)
    final_cols   = ", ".join(_COLUMNS)

    return f"""
INSERT INTO {TRINO_BQ_CATALOG}.{BQ_DATASET}.{BQ_TEMP_TABLE} (
    {insert_cols}
)
WITH ranked AS (
    SELECT
        {trino_exprs},
        ROW_NUMBER() OVER (
            PARTITION BY src.{MERGE_KEY}
            ORDER BY src.{PARTITION_FIELD} DESC
        ) AS _rn
    FROM {TRINO_PG_CATALOG}.{PG_SCHEMA}.{PG_SOURCE_TABLE} AS src
    -- data_interval_start/end dikonversi ke WIB agar cocok dengan timezone kolom updated_at (+0700).
    -- Contoh: 2026-03-11 02:00:00 UTC → 2026-03-11 09:00:00 WIB
    WHERE src.{PARTITION_FIELD} >= TIMESTAMP '{{{{ dag_run.conf.get("window_start") or data_interval_start.in_timezone("Asia/Jakarta").strftime("%Y-%m-%d %H:%M:%S") }}}}'
      AND src.{PARTITION_FIELD} <  TIMESTAMP '{{{{ dag_run.conf.get("window_end")   or data_interval_end.in_timezone("Asia/Jakarta").strftime("%Y-%m-%d %H:%M:%S") }}}}'
)
SELECT {final_cols}
FROM   ranked
WHERE  _rn = 1
"""


def _sync_final_table_schema(ds_nodash: str, **kwargs) -> str:
    """
    Schema evolution: deteksi kolom baru di temp, ALTER TABLE final untuk menambahnya.

    ✅ FIXED: Menerima ds_nodash via op_kwargs → nama temp table dinamis per run.
    ✅ FIXED: Menggunakan BigQueryHook(gcp_conn_id=GCP_CONN_ID) — bukan bare
              bigquery.Client() yang bypass Airflow connection & service account control.
              Di lingkungan perbankan, service account per connection adalah
              kontrol akses yang wajib dihormati.
    ✅ FIXED: try/except per kolom — error deskriptif saat type conflict / permission issue.

    Hanya menangani ADD COLUMN (safe evolution).
    DROP COLUMN / type change memerlukan DBA sign-off dan change management
    sesuai prosedur perubahan data (compliance perbankan).

    Idempotent: IF NOT EXISTS → aman di-retry tanpa error "duplicate column".
    """
    # ✅ FIXED: BigQueryHook menggunakan GCP_CONN_ID → service account access control dihormati
    from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook
    import logging
    hook   = BigQueryHook(gcp_conn_id=GCP_CONN_ID)
    client = hook.get_client(project_id=BQ_PROJECT)

    # Nama temp table berdasarkan ds_nodash yang diterima dari op_kwargs
    temp_table_name = f"{BQ_FINAL_TABLE}_temp_{ds_nodash}"
    temp_ref        = f"{BQ_PROJECT}.{BQ_DATASET}.{temp_table_name}"
    final_ref       = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_FINAL_TABLE}"

    # ✅ FIXED: try/except untuk get_table — error jelas jika tabel tidak ditemukan
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
        # ✅ FIXED: try/except per ALTER — jangan biarkan satu kolom gagal tanpa keterangan
        try:
            job = client.query(alter_sql)
            job.result(timeout=300)
        except Exception as exc:
            raise RuntimeError(
                f"Schema evolution gagal untuk kolom '{col.name}' (type: {col.field_type}). "
                f"Kemungkinan penyebab: type conflict dengan kolom existing, "
                f"atau insufficient permission pada service account. "
                f"Detail: {exc}"
            ) from exc

    added = [c.name for c in new_cols]
    logging.info("✅ Schema evolution selesai — kolom baru ditambahkan: %s", added)
    return f"added:{','.join(added)}"



def _bq_merge_query() -> dict:
    """
    BigQuery MERGE job: UPSERT branches_temp → branches.
    UPDATE: hanya kalau S.updated_at > T.updated_at (cegah overwrite data lebih baru).
    INSERT: baris baru yang belum ada di final table.
    ROW_NUMBER() di USING subquery sebagai dedup guard kedua.
    writeDisposition / createDisposition / schemaUpdateOptions tidak dipakai —
    tidak valid untuk DML statement, akan error BQ 400.
    """
    non_key_cols = [c for c in _COLUMNS if c != MERGE_KEY]
    # set_clause   = ",\n            ".join(
    #     f"T.{c} = S.{c}" for c in non_key_cols
    # )
    ins_cols     = ", ".join(_COLUMNS)
    ins_vals     = ", ".join(f"S.{c}" for c in _COLUMNS)

    # ✅ WHEN MATCHED hanya dibuat jika bukan append-only
    if APPEND_ONLY:
        # Tabel append-only: tidak ada UPDATE, hanya INSERT baris baru
        when_matched_clause = ""
    else:
        # Tabel yang bisa di-UPDATE: UPDATE hanya jika data lebih baru
        set_clause = ",\n                        ".join(
            f"T.{c} = S.{c}" for c in non_key_cols
        )
        when_matched_clause = f"""
                WHEN MATCHED AND S.{PARTITION_FIELD} > T.{PARTITION_FIELD} THEN
                    UPDATE SET
                        {set_clause}
        """

    return {
        "query": {
            "query": f"""
                MERGE `{BQ_PROJECT}.{BQ_DATASET}.{BQ_FINAL_TABLE}` AS T
                USING (
                    SELECT * EXCEPT(_rn)
                    FROM (
                        SELECT
                            *,
                            ROW_NUMBER() OVER (
                                PARTITION BY {MERGE_KEY}
                                ORDER BY {PARTITION_FIELD} DESC
                            ) AS _rn
                        FROM `{BQ_PROJECT}.{BQ_DATASET}.{BQ_TEMP_TABLE}`
                    )
                    WHERE _rn = 1
                ) AS S
                ON T.{MERGE_KEY} = S.{MERGE_KEY}

                {when_matched_clause}

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


# ══════════════════════════════════════════════════════════════════════════════
#  DAG
# ══════════════════════════════════════════════════════════════════════════════

default_args = {
    "owner":             "data-engineering",
    "depends_on_past":   False,
    "email_on_failure":  True,
    "email_on_retry":    False,
    "retries":           3,
    "retry_delay":       timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "execution_timeout": timedelta(hours=1),
}

with DAG(
    dag_id="postgres_to_bq_trino_customer_v2",
    description="Postgres → BigQuery via Trino cross-catalog (no GCS, no EXTERNAL_QUERY)",
    default_args=default_args,
    schedule=CronDataIntervalTimetable("0 9 * * *", timezone="Asia/Jakarta"),                      # setiap jam 9 pagi
    start_date=pendulum.datetime(2026, 3, 11, tz="Asia/Jakarta"),
    catchup=True,
    max_active_runs=1,
    tags=["postgres", "bigquery", "trino", "ingestion"],
    doc_md=__doc__,
) as dag:

    # ── Step 1: CREATE branches_temp via BigQuery API ────────────────────────
    # Tidak pakai Trino DDL karena Trino BigQuery connector tidak support
    # WITH (partitioning=..., clustering_key=...) — itu properti Iceberg/Hive.
    # BigQueryCreateTableOperator pakai BQ REST API → support timePartitioning + clustering.
    # if_exists="ignore" → idempotent, aman di-retry tanpa error "table already exists".
    create_bq_temp = BigQueryCreateTableOperator(
        task_id="create_bq_temp_table",
        gcp_conn_id=GCP_CONN_ID,
        project_id=BQ_PROJECT,
        dataset_id=BQ_DATASET,
        table_id=BQ_TEMP_TABLE,
        # table_resource = representasi lengkap BQ Table object (BQ REST API format).
        table_resource={
            "tableReference": {
                "projectId": BQ_PROJECT,
                "datasetId": BQ_DATASET,
                "tableId":   BQ_TEMP_TABLE,
            },
            "schema": {
                "fields": SCHEMA_FIELDS,
            },
            "timePartitioning": {
                "type":  "MONTH",
                "field": PARTITION_FIELD,
            },
            "clustering": {
                "fields": CLUSTER_FIELDS,
            },
        },
        if_exists="ignore",                        # idempotent — aman di-retry
        outlets=[BQ_TEMP_ASSET],
        doc_md="""
        Membuat BQ temp table via BigQuery API (BigQueryCreateTableOperator).
        Partitioning MONTH(updated_at) + clustering (province, branch_type, is_active).
        table_resource = full BQ Table JSON representation.
        """,
    )

    # ── Step 2: INSERT Postgres → branches_temp via Trino cross-catalog ────────
    # Trino baca postgresql.public.branches dan INSERT ke bigquery.raw_core_banking.branches_temp
    # dalam satu query — tidak butuh GCS, tidak butuh BQ External Connection.
    # Window: updated_at >= data_interval_start AND < data_interval_end (tepat 10 menit).
    # Manual trigger: start == end → window kosong. Gunakan logical_date saat trigger manual.
    # ROW_NUMBER() CTE dedup: satu baris per branch_id, ambil updated_at terbaru.
    insert_to_bq_temp = SQLExecuteQueryOperator(
        task_id="insert_postgres_to_bq_temp",
        conn_id=TRINO_CONN_ID,
        sql=_trino_insert_sql(),
        autocommit=True,
        outlets=[BQ_TEMP_ASSET],
        doc_md="""
        Trino cross-catalog INSERT: baca dari postgresql.public.branches,
        tulis ke bigquery.dataset.branches_temp.
        ROW_NUMBER() CTE memastikan hanya satu baris per branch_id yang masuk.
        """,
    )

    # ── Step 3: Schema evolution — sync branches schema ──────────────────────
    # MERGE tidak support schemaUpdateOptions/writeDisposition/createDisposition
    # (hanya valid untuk job dengan destinationTable, bukan DML statement).
    # Solusi incremental: bandingkan schema temp vs final, ALTER TABLE final
    # ADD COLUMN IF NOT EXISTS untuk setiap kolom baru — dijalankan SEBELUM MERGE
    # agar MERGE tidak error "Unrecognized name: <col>".
    sync_schema = PythonOperator(
        task_id="sync_final_table_schema",
        python_callable=_sync_final_table_schema,
        doc_md="""
        Bandingkan schema branches_temp_<ds> vs branches.
        Kolom baru → ALTER TABLE branches ADD COLUMN IF NOT EXISTS (idempotent).
        Hanya ADD COLUMN — DROP/type change perlu DBA sign-off & change management.
        Menggunakan BigQueryHook(GCP_CONN_ID) untuk menghormati access control.
        """,
    )

    # ── Step 4: MERGE branches_temp → branches ───────────────────────────────
    # Full UPSERT: UPDATE baris yang sudah ada (hanya kalau updated_at lebih baru),
    # INSERT baris baru yang belum ada di final.
    # ROW_NUMBER() kedua di dalam MERGE source sebagai guard dedup tambahan.
    # writeDisposition / createDisposition / schemaUpdateOptions sengaja tidak dipakai
    # — ketiganya tidak valid untuk DML dan menyebabkan error BQ 400.
    merge_to_final = BigQueryInsertJobOperator(
        task_id="merge_temp_to_final",
        gcp_conn_id=GCP_CONN_ID,
        project_id=BQ_PROJECT,
        location=BQ_LOCATION,
        configuration=_bq_merge_query(),
        job_id="{{ dag.dag_id }}__merge__{{ ds_nodash }}",
        force_rerun=False,
        deferrable=False,
        outlets=[BQ_FINAL_ASSET],                  # marks final table as updated
        doc_md="""
        MERGE (UPSERT) from temp into the final table.
        Only updates rows where the incoming updated_at is newer, preventing
        out-of-order event overwrites.
        """,
    )

    # ── Step 5: DROP branches_temp ───────────────────────────────────────────
    # trigger_rule=all_done → cleanup tetap jalan meski step sebelumnya gagal,
    # mencegah akumulasi stale temp table di dataset.
    drop_bq_temp = BigQueryDeleteTableOperator(
        task_id="drop_bq_temp_table",
        gcp_conn_id=GCP_CONN_ID,
        deletion_dataset_table=f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TEMP_TABLE}",
        ignore_if_missing=True,
        trigger_rule=TriggerRule.ALL_DONE,
        doc_md="Always drops the temp table, even when upstream tasks fail.",
    )

    # ── Pipeline order ─────────────────────────────────────────────────────────
    create_bq_temp >> insert_to_bq_temp >> sync_schema >> merge_to_final >> drop_bq_temp