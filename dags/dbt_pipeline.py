"""
dbt_pipeline
============
Pipeline transformasi dbt secara berurutan dengan DockerOperator:

    dbt seed
        └─► dbt snapshot
                └─► dbt run --select tag:dim
                        └─► dbt run --select tag:fact
                                └─► dbt run --select tag:gold
                                        └─► dbt test

Semua step menggunakan target ``dev``.
Image Docker  : dbt-project-taxi:1.0
GCP Project   : taxi-pipeline-123
Jadwal        : @once  (jalankan manual atau ubah sesuai kebutuhan)
"""

from __future__ import annotations

import os
from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.operators.empty import EmptyOperator
from airflow.providers.docker.operators.docker import DockerOperator
from airflow.utils.trigger_rule import TriggerRule
from docker.types import Mount

# ─── Constants ────────────────────────────────────────────────────────────────

DBT_IMAGE   = os.getenv("DBT_IMAGE",   "dbt-project-taxi:1.0")
DBT_TARGET  = os.getenv("DBT_TARGET",  "dev")
GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "taxi-pipeline-123")
SA_HOST     = os.getenv(
    "GCP_SA_HOST_PATH",
    "/home/void/taxi-pipeline/credentials/service-account.json",
)
SA_CONTAINER = "/opt/gcp/service-account.json"

# ─── Shared DockerOperator kwargs ─────────────────────────────────────────────

_DOCKER_KWARGS = dict(
    image=DBT_IMAGE,
    auto_remove="force",
    environment={
        "GCP_PROJECT_ID": GCP_PROJECT,
        "GOOGLE_APPLICATION_CREDENTIALS": SA_CONTAINER,
    },
    mounts=[
        Mount(
            source=SA_HOST,
            target=SA_CONTAINER,
            type="bind",
        ),
    ],
    # Agar log dbt ter-stream ke Airflow
    retrieve_output=True,
    retrieve_output_path="/tmp/dbt_output",
)

# ─── Default args ─────────────────────────────────────────────────────────────

default_args = {
    "owner":             "data-engineering",
    "depends_on_past":   False,
    "email":             [os.getenv("ALERT_EMAIL", "data-engineering@bank.co.id")],
    "email_on_failure":  True,
    "email_on_retry":    False,
    "retries":           1,
    "retry_delay":       timedelta(minutes=5),
    "execution_timeout": timedelta(hours=2),
}

# ─── DAG ──────────────────────────────────────────────────────────────────────

with DAG(
    dag_id="dbt_pipeline",
    description=(
        "dbt transformation pipeline: seed → snapshot → dim → fact → gold → test. "
        "Semua step berjalan dengan target dev."
    ),
    schedule="@once",
    start_date=pendulum.datetime(2026, 3, 11, tz="Asia/Jakarta"),
    catchup=False,
    max_active_runs=1,
    tags=["dbt", "bigquery", "transformation", "dev"],
    default_args=default_args,
    doc_md=__doc__,
) as dag:

    # ── Start / End markers ───────────────────────────────────────────────────

    start = EmptyOperator(task_id="start")
    end   = EmptyOperator(
        task_id="end",
        trigger_rule=TriggerRule.ALL_DONE,   # tetap jalan meski ada failure, untuk logging
    )

    # ── Step 1: dbt seed ──────────────────────────────────────────────────────
    # Memuat file CSV dari folder seeds/ ke BigQuery (silver_core):
    #   - cancel_reason_lookup
    #   - national_holidays
    #   - city_master

    dbt_seed = DockerOperator(
        task_id="dbt_seed",
        command=f"dbt seed --target {DBT_TARGET} --full-refresh",
        **_DOCKER_KWARGS,
    )

    # ── Step 2: dbt snapshot ──────────────────────────────────────────────────
    # Menjalankan SCD Type-2 snapshot ke silver_core:
    #   - snapshot_dim_customer
    #   - snapshot_dim_driver

    dbt_snapshot = DockerOperator(
        task_id="dbt_snapshot",
        command=f"dbt snapshot --target {DBT_TARGET}",
        **_DOCKER_KWARGS,
    )

    # # ── Step 3: dbt run dim ───────────────────────────────────────────────────
    # # Membangun semua model bertag "dim" di silver/dim/:
    # #   static  : dim_date, dim_time, dim_payment_method, dim_trip_status
    # #   incremental: dim_vehicle, dim_location, dim_promo
    # #   scd2    : dim_customer, dim_driver  (merge dari snapshot)

    # dbt_run_dim = DockerOperator(
    #     task_id="dbt_run_dim",
    #     command=f"dbt run --select tag:dim --target {DBT_TARGET}",
    #     **_DOCKER_KWARGS,
    # )

    # # ── Step 4: dbt run fact ──────────────────────────────────────────────────
    # # Membangun semua model bertag "fact" di silver/fact/:
    # #   - fct_trip, fct_payment, fct_driver_payout, fct_rating, fct_promo_redemption
    # # Semua incremental + partitioned by created_at (daily)

    # dbt_run_fact = DockerOperator(
    #     task_id="dbt_run_fact",
    #     command=f"dbt run --select tag:fact --target {DBT_TARGET}",
    #     **_DOCKER_KWARGS,
    # )

    # # ── Step 5: dbt run gold ──────────────────────────────────────────────────
    # # Membangun semua model bertag "gold" (data mart):
    # #   operations : dm_trip_daily_city, dm_trip_hourly_city
    # #   finance    : dm_finance_daily_city, dm_payment_method_daily
    # #   marketing  : dm_promo_daily, dm_campaign_daily_channel, dm_customer_segment_daily
    # #   driver     : dm_driver_daily_performance, dm_driver_monthly_summary

    # dbt_run_gold = DockerOperator(
    #     task_id="dbt_run_gold",
    #     command=f"dbt run --select tag:gold --target {DBT_TARGET}",
    #     **_DOCKER_KWARGS,
    # )

    # # ── Step 6: dbt test ──────────────────────────────────────────────────────
    # # Menjalankan semua test (schema tests + singular tests):
    # #   singular: assert_no_orphan_payments, assert_payment_amount_logic,
    # #             assert_gold_ops_coverage, assert_completed_trip_has_one_payment,
    # #             assert_completion_rate_by_city, assert_platform_revenue_sanity,
    # #             assert_scd2_no_overlap_customer, assert_trip_dates_in_range,
    # #             assert_no_orphan_payouts
    # #   generic : not_negative, mutually_exclusive_flags, accepted_range

    dbt_test = DockerOperator(
        task_id="dbt_test",
        command=f"dbt test --target {DBT_TARGET}",
        **_DOCKER_KWARGS,
    )

    # ── Dependency chain ──────────────────────────────────────────────────────

    (
        start
        >> dbt_seed
        >> dbt_snapshot
        # >> dbt_run_dim
        # >> dbt_run_fact
        # >> dbt_run_gold
        >> dbt_test
        >> end
    )