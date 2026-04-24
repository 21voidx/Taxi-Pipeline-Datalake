from typing import Sequence, Optional, List
from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

class BigQueryExecuteQueryOperator(BigQueryInsertJobOperator):
    """
    Custom operator that extends BigQueryInsertJobOperator for executing queries with Jinja support
    """
    
    template_fields: Sequence[str] = ('sql', 'destination_dataset_table')
    template_ext: Sequence[str] = ('.sql',)
    ui_color = '#e8f7e4'
    
    def __init__(
        self,
        *,
        sql: str,
        destination_dataset_table: str = None,
        gcp_conn_id: str = 'google_cloud_default',
        write_disposition: str = 'WRITE_TRUNCATE',
        create_disposition: str = 'CREATE_IF_NEEDED',
        use_legacy_sql: bool = False,
        location: str = None,
        time_partitioning: dict = None,
        cluster_fields: list = None,
        schema_update_options: Optional[List[str]] = None,
        labels: dict = None,
        **kwargs
    ) -> None:
        self.sql = sql
        self.destination_dataset_table = destination_dataset_table
        self.gcp_conn_id = gcp_conn_id
        self.write_disposition = write_disposition
        self.create_disposition = create_disposition
        self.use_legacy_sql = use_legacy_sql
        self.location = location
        self.time_partitioning = time_partitioning
        self.cluster_fields = cluster_fields
        self.schema_update_options = schema_update_options
        self.labels = labels
        
        super().__init__(
            configuration=None,  # We'll set this in execute()
            location=location,
            gcp_conn_id=gcp_conn_id,
            **kwargs
        )

    def _get_configuration(self):
        """Builds the configuration dictionary for the query job"""
        hook = BigQueryHook(
            gcp_conn_id=self.gcp_conn_id,
            use_legacy_sql=self.use_legacy_sql,
            location=self.location,
        )
        
        configuration = {
            'query': {
                'query': self.sql,
                'useLegacySql': self.use_legacy_sql,
            }
        }

        project_id = hook.project_id

        if self.schema_update_options:
            configuration['query']['schemaUpdateOptions'] = self.schema_update_options
        
        if self.labels:
            configuration['labels'] = self.labels

        if self.destination_dataset_table:
            dataset_parts = self.destination_dataset_table.split('.')
            if len(dataset_parts) == 3:
                # If project.dataset.table format is provided
                dest_project_id, dataset_id, table_id = dataset_parts
            elif len(dataset_parts) == 2:
                # If dataset.table format is provided
                dest_project_id = project_id
                dataset_id, table_id = dataset_parts
            else:
                raise Exception(f"Invalid destination_dataset_table format: {self.destination_dataset_table}")

            configuration['query']['destinationTable'] = {
                'projectId': dest_project_id,
                'datasetId': dataset_id,
                'tableId': table_id,
            }
            configuration['query']['writeDisposition'] = self.write_disposition
            configuration['query']['createDisposition'] = self.create_disposition

            if self.time_partitioning:
                configuration['query']['timePartitioning'] = self.time_partitioning

            if self.cluster_fields:
                configuration['query']['clustering'] = {
                    'fields': self.cluster_fields
                }

        return configuration, hook

    def execute(self, context):
        if isinstance(self.sql, str):
            queries = [self.sql]
        else:
            queries = self.sql

        self.job_id = []
        configuration, hook = self._get_configuration()
        
        for sql_query in queries:
            configuration['query']['query'] = sql_query
            
            # Use insert_job instead of execute
            job = hook.insert_job(
                configuration=configuration,
                project_id=hook.project_id,
                location=self.location
            )
            self.job_id.append(job.job_id)
            
        return self.job_id