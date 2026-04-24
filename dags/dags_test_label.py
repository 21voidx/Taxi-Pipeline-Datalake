##===========================================================##
##                        Library                            ##
##===========================================================##
from datetime import datetime, timedelta
# Asumsi kamu menggunakan custom operator atau standard bigquery operator
# dari airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator 
from helpers.BigQueryExecuteQueryOperator import BigQueryExecuteQueryOperator
from airflow.sdk import TaskGroup
# from airflow.sdk import WeightRule
from pendulum import timezone
from airflow.sdk import Asset
from airflow.sdk import Variable
from airflow.sdk import DAG
from airflow.providers.standard.operators.python import PythonOperator
from airflow.timetables.interval import CronDataIntervalTimetable

doc = """
## DAG: Datamart Ride Ops - Dimension Zones
DAG ini bertugas untuk melakukan proses MERGE (Upsert) data CDC tabel `zones` 
dari staging ke tabel dimensi `dim_zones` di BigQuery.

### How To Trigger DAG Manually
Use specific DATETIME as a STRING for the start_date and end_date
inside the JSON key, for example:
{"start_date":"2024-06-01", "end_date":"2024-06-02"}
"""

##===========================================================##
##                        Variable                           ##
##===========================================================##
PARENT_DAG_NAME = "datamart_rides"
project_id = Variable.get("gcp_project", default="dbt-taxi-explore")

env_name = Variable.get("environment_name", default="development")
# alert_email = Variable.get('alert_email').split(";")
ymd = "{{logical_date.strftime('%Y%m%d')}}"

##===========================================================##
##                          Table                            ##
##===========================================================##
# Target table untuk Asset/Lineage
datamart_dim_zones = f"{project_id}.dev_label.rides"

if env_name == "production":
    start_date = datetime(2026, 6, 1, tzinfo=timezone('Asia/Jakarta'))
    end_date = None
else:
    start_date = datetime(2026, 3, 31, tzinfo=timezone('Asia/Jakarta'))
    end_date = datetime(2026, 6, 5, tzinfo=timezone('Asia/Jakarta'))
    
default_args = {
    'owner': 'Data Engineering Team',
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 3,
    'retry_delay': timedelta(minutes=5)
}

dag = DAG(
    dag_id=PARENT_DAG_NAME,
    default_args=default_args,
    doc_md=doc,
    schedule=CronDataIntervalTimetable("30 6 * * *", timezone='Asia/Jakarta'), # Jalan jam 06:30 pagi
    catchup=True, 
    start_date=start_date, 
    end_date=end_date,
    max_active_runs=2,
    tags=['datamart', 'ride_ops', 'dimension', 'cdc']
)

def params_eval(**kwargs):
    for _date in ['start_date', 'end_date']:
        kwargs['task_instance'].xcom_push(
            key=_date,
            value=[
                kwargs['dag_run'].conf.get(_date, "") if _date in kwargs['dag_run'].conf
                else (kwargs['logical_date'] + timedelta(days=1)).strftime("%Y-%m-%d")
            ][0]
        )

    kwargs['task_instance'].xcom_push(
        key='ymd',
        value=[
            kwargs['dag_run'].conf.get("start_date", "").replace("-", "") + "_" + kwargs['dag_run'].conf.get("end_date", "").replace("-", "")
            if kwargs['dag_run'].conf.get("start_date", "")
            else (kwargs['logical_date'] + timedelta(days=1)).strftime("%Y%m%d")
        ][0]
    )
    
task_params_eval = PythonOperator(
    task_id='params_eval',
    python_callable=params_eval,
    dag=dag
)
    
with TaskGroup(dag=dag, group_id="process_dim_zones", prefix_group_id=False,
               default_args={'weight_rule': 'upstream'}) as group_dim_zones:

    # Memanggil script SQL merge yang sudah kita buat sebelumnya
    task_merge_zones = BigQueryExecuteQueryOperator(
        task_id='merge_data_rides',
        sql='sql/merge_data.sql',
        params=dict(
            project_id=project_id
        ),
        outlets=[Asset(datamart_dim_zones)],
        use_legacy_sql=False,
        labels={"table": "zones", "source": "dev_bronze_pg", "target": "dev_label"},
        dag=dag
    )
    
task_params_eval >> group_dim_zones