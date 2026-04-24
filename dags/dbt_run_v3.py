from __future__ import annotations

import os
from datetime import datetime, timedelta
import pendulum
from airflow import DAG
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount

# ─── Paths & constants ────────────────────────────────────────────────────────

# Path service account di host machine
SA_KEY_HOST      = os.getenv("SA_KEY_HOST", "/home/void/taxi-pipeline/credentials/service-account.json")

# Path di dalam container (sesuai Dockerfile)
SA_KEY_CONTAINER = "/opt/gcp/service-account.json"

# dbt project ada di /app di dalam container (sesuai Dockerfile: COPY dbt_project/ /app)
# profiles ada di /root/.dbt (sesuai Dockerfile: COPY dbt_profiles/ /root/.dbt)

DBT_TARGET = os.getenv("DBT_TARGET", "dev")

DBT_BASE = f"--project-dir /app --profiles-dir /root/.dbt --target {DBT_TARGET}"

default_args = {
    "owner":             "data-engineering",
    "depends_on_past":   False,
    "email":             [os.getenv("ALERT_EMAIL", "data-engineering@company.co.id")],
    "email_on_failure":  True,
    "email_on_retry":    False,
    "retries":           1,
    "retry_delay":       timedelta(minutes=5),
    "execution_timeout": timedelta(hours=1),
}

# ─── Shared config ────────────────────────────────────────────────────────────

DOCKER_COMMON = dict(
    image          = "dbt-project-taxi:1.0",
    auto_remove    = "force",
    mount_tmp_dir  = False,           # ← fix: cegah error /tmp/airflowtmp tidak ada
    network_mode   = "host",
    environment    = {
        "GCP_PROJECT_ID":                "dbt-taxi-explore",
        "GOOGLE_APPLICATION_CREDENTIALS": SA_KEY_CONTAINER,
    },
    mounts = [
        Mount(
            source = SA_KEY_HOST,
            target = SA_KEY_CONTAINER,
            type   = "bind",
        ),
        # Uncomment baris di bawah untuk development (mount volume = tidak perlu rebuild image)
        Mount(
            source = "/home/void/taxi-pipeline/dbt/dbt_project",  # ← folder dbt_project di host
            target = "/app",                                   # ← WORKDIR di container
            type   = "bind",
        ),
    ],
)

# ─── DAG ──────────────────────────────────────────────────────────────────────

with DAG(
    dag_id      = "dbt_run_v3",
    description = "dbt pipeline: seed → snapshot → silver → gold",
    schedule    = "0 */2 * * *",  # At minute 0 past every 2nd hour
    start_date  = pendulum.datetime(2026, 3, 29, tz="Asia/Jakarta"),
    catchup     = False,
    max_active_runs = 1,
    tags        = ["dbt", "bigquery", "transformation"],
    default_args = default_args,
) as dag:

    dbt_debug = DockerOperator(
        task_id = "dbt_debug",
        command = f"dbt debug {DBT_BASE}",
        **DOCKER_COMMON,
    )

    dbt_deps = DockerOperator(
        task_id = "dbt_deps",
        command = f"dbt deps {DBT_BASE}",
        **DOCKER_COMMON,
    )

    # dbt_seed = DockerOperator(
    #     task_id = "dbt_seed",
    #     command = f"dbt seed {DBT_BASE}",
    #     **DOCKER_COMMON,
    # )

    dbt_snapshot = DockerOperator(
        task_id = "dbt_snapshot",
        command = f"dbt snapshot --select tag:snapshot {DBT_BASE}",
        **DOCKER_COMMON,
    )

    dbt_run_silver = DockerOperator(
        task_id = "dbt_run_silver",
        command = f"dbt run --select tag:silver {DBT_BASE}",
        **DOCKER_COMMON,
    )

    dbt_run_gold = DockerOperator(
        task_id = "dbt_run_gold",
        command = f"dbt run --select tag:gold {DBT_BASE}",
        **DOCKER_COMMON,
    )

    dbt_debug >> dbt_deps >> dbt_snapshot >> dbt_run_silver >> dbt_run_gold