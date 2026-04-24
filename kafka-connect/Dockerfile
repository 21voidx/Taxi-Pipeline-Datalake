FROM confluentinc/cp-kafka-connect:7.6.0

# Debezium PostgreSQL Connector
RUN confluent-hub install --no-prompt \
    debezium/debezium-connector-postgresql:2.5.4

# Debezium MongoDB Connector
RUN confluent-hub install --no-prompt \
    debezium/debezium-connector-mongodb:2.5.4

# Confluent GCS Sink Connector
RUN confluent-hub install --no-prompt \
    iceberg/iceberg-kafka-connect:1.9.1

# # Confluent GCS Sink Connector
# RUN confluent-hub install --no-prompt \
#     confluentinc/kafka-connect-gcs:11.0.5

# # Confluent S3 Sink Connector (compatible with MinIO via S3 API)
# RUN confluent-hub install --no-prompt \
#     confluentinc/kafka-connect-s3:10.5.13

# Avro Converter for Schema Registry
RUN confluent-hub install --no-prompt \
    confluentinc/kafka-connect-avro-converter:7.6.0

USER appuser