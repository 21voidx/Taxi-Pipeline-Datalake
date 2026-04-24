# Debezium PostgreSQL Source Connector Configuration

This document contains the configuration for the Debezium PostgreSQL CDC connector. The configuration has been updated to improve data integrity and error handling.

## Configuration Details

Below is the structured and annotated JSON configuration file.

```json
{
  "name": "debezium-postgres-source",
  "config": {
    // ==========================================
    // CONNECTOR METADATA & SYSTEM CONFIGURATION
    // ==========================================
    "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
    // PostgreSQL CDC reads from a single WAL replication slot, so this must always be 1.
    "tasks.max": "1",

    // ==========================================
    // DATABASE CONNECTION & AUTHENTICATION
    // ==========================================
    // NOTE FOR PRODUCTION: Hardcoded IPs and plaintext passwords should be replaced 
    // with environment variables or external secrets management.
    "database.hostname": "127.0.0.1",
    "database.port": "5433",
    "database.user": "debezium",
    "database.password": "debezium",
    "database.dbname": "ride_ops_pg",
    // Logical identifier for this database server/cluster. Used for topic routing.
    "database.server.name": "postgres-ops",

    // ==========================================
    // CDC & REPLICATION SLOT CONFIGURATION
    // ==========================================
    // Native logical replication plugin for PostgreSQL 10+.
    "plugin.name": "pgoutput",
    "publication.name": "debezium_pub",
    "publication.autocreate.mode": "disabled",
    // The name of the logical replication slot created in Postgres. Must be unique.
    "slot.name": "debezium_slot",
    // Whitelist of tables to capture. Reduces load by ignoring unnecessary tables.
    "table.include.list": "public.drivers,public.passengers,public.rides,public.vehicle_types,public.zones",

    // ==========================================
    // TOPIC ROUTING & AUTO-CREATION
    // ==========================================
    "topic.prefix": "cdc",
    "topic.creation.enable": "true",
    // NOTE FOR PRODUCTION: Change replication factor to "3" if running a multi-node KRaft cluster.
    "topic.creation.default.replication.factor": "1",
    "topic.creation.default.partitions": "3",
    "topic.creation.default.cleanup.policy": "delete",
    "topic.creation.default.retention.ms": "604800000",

    // ==========================================
    // SERIALIZATION & SCHEMA REGISTRY
    // ==========================================
    // Uses Avro for efficient compression and schema evolution management.
    "key.converter": "io.confluent.connect.avro.AvroConverter",
    "key.converter.schema.registry.url": "[http://127.0.0.1:8081](http://127.0.0.1:8081)",
    "value.converter": "io.confluent.connect.avro.AvroConverter",
    "value.converter.schema.registry.url": "[http://127.0.0.1:8081](http://127.0.0.1:8081)",

    // ==========================================
    // SINGLE MESSAGE TRANSFORMATIONS (SMT)
    // ==========================================
    "transforms": "unwrap",
    // Flattens the complex Debezium payload into a simpler structure.
    "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState",
    "transforms.unwrap.drop.tombstones": "false",
    "transforms.unwrap.delete.handling.mode": "rewrite",
    // Appends critical metadata to the flattened payload.
    "transforms.unwrap.add.fields": "op,source.ts_ms,table",
    "transforms.unwrap.add.headers": "db",

    // ==========================================
    // SNAPSHOT & BEHAVIORAL TUNING
    // ==========================================
    "snapshot.mode": "initial",
    "snapshot.isolation.mode": "repeatable_read",
    // [UPDATED] Uses 'precise' mode (java.math.BigDecimal) to prevent precision loss for monetary/numeric values.
    "decimal.handling.mode": "precise",
    "time.precision.mode": "connect",
    // Sends a heartbeat query every 10s to keep the WAL from growing endlessly during low traffic.
    "heartbeat.interval.ms": "10000",

    // ==========================================
    // PERFORMANCE & MEMORY LIMITS
    // ==========================================
    "max.batch.size": "2048",
    "max.queue.size": "16384",
    "poll.interval.ms": "1000",

    // ==========================================
    // ERROR HANDLING
    // ==========================================
    "errors.log.enable": "true",
    "errors.log.include.messages": "true",
    // [UPDATED] Set to 'none' to ensure the connector fails fast and stops on error, preventing silent data loss.
    "errors.tolerance": "none"
  }
}
```