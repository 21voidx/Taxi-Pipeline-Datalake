-- ══════════════════════════════════════════════════════════════
--  postgres-catalog  —  Iceberg JDBC Catalog bootstrap
--  Trino and the Iceberg Kafka Connect sink both use this DB
--  as the Iceberg metadata / catalog store.
--  The iceberg_tables and iceberg_namespace_properties tables
--  are created automatically by the Iceberg JDBC catalog on
--  first use, but we pre-create the schema and grant here.
-- ══════════════════════════════════════════════════════════════

-- Namespace for CDC tables
CREATE SCHEMA IF NOT EXISTS cdc;

-- Pre-create Iceberg catalog tables so both Trino and
-- kafka-connect can find them immediately.
CREATE TABLE IF NOT EXISTS iceberg_tables (
    catalog_name   VARCHAR(255)  NOT NULL,
    table_namespace VARCHAR(255) NOT NULL,
    table_name     VARCHAR(255)  NOT NULL,
    metadata_location VARCHAR(1000),
    previous_metadata_location VARCHAR(1000),
    CONSTRAINT iceberg_tables_pk PRIMARY KEY (catalog_name, table_namespace, table_name)
);

CREATE TABLE IF NOT EXISTS iceberg_namespace_properties (
    catalog_name   VARCHAR(255)  NOT NULL,
    namespace      VARCHAR(255)  NOT NULL,
    property_key   VARCHAR(255)  NOT NULL,
    property_value VARCHAR(1000),
    CONSTRAINT iceberg_namespace_properties_pk PRIMARY KEY (catalog_name, namespace, property_key)
);

-- Allow the iceberg user full access
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO iceberg;
GRANT ALL PRIVILEGES ON SCHEMA cdc TO iceberg;