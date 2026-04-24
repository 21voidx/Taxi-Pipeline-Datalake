# /opt/airflow/dags/hello_world_dag.py

from __future__ import annotations
from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator

# ════════════════════════════════════════════
#  Function to execute
# ════════════════════════════════════════════
def hello_world_task():
    print("Hello, Airflow 3.x World! 🚀")

# ════════════════════════════════════════════
#  Default args for DAG
# ════════════════════════════════════════════
default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=1),
    "email_on_failure": False,
}

# ════════════════════════════════════════════
#  DAG definition
# ════════════════════════════════════════════
with DAG(
    dag_id="hello_world_dag",
    description="Simple Hello World DAG for Airflow 3.x",
    default_args=default_args,
    schedule=None,  # manual trigger
    start_date=datetime(2026, 3, 1),
    catchup=False,
    tags=["example", "hello-world"],
) as dag:

    # Task
    hello_task = PythonOperator(
        task_id="print_hello",
        python_callable=hello_world_task,
        doc_md="""
        This task prints 'Hello, Airflow 3.x World!' to the logs.
        """,
    )

    # DAG flow
    hello_task