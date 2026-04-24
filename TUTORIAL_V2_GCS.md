# Tutorial: Setup Data Pipeline Kafka → GCS (Manual CLI)

> **Stack:** PostgreSQL + MongoDB → Debezium CDC → Kafka (KRaft) → Schema Registry → Kafka Connect → Google Cloud Storage  
> **Format sink:** Parquet + Snappy (partitioned by topic/year/month/day/hour)  
> Semua langkah dikerjakan **murni lewat terminal** — tanpa file `.sh`.

---

## Daftar Isi

1. [Arsitektur](#1-arsitektur)
2. [Prerequisites](#2-prerequisites)
3. [Setup Google Cloud Storage (GCS)](#3-setup-google-cloud-storage-gcs)
4. [Siapkan Struktur Project](#4-siapkan-struktur-project)
5. [Modifikasi Dockerfile Kafka Connect untuk GCS](#5-modifikasi-dockerfile-kafka-connect-untuk-gcs)
6. [Buat GCS Sink Connector Config](#6-buat-gcs-sink-connector-config)
7. [Update docker-compose untuk GCS](#7-update-docker-compose-untuk-gcs)
8. [Build dan Jalankan Stack](#8-build-dan-jalankan-stack)
9. [Init MongoDB Replica Set](#9-init-mongodb-replica-set)
10. [Verifikasi PostgreSQL & Debezium User](#10-verifikasi-postgresql--debezium-user)
11. [Tunggu Kafka Connect Ready](#11-tunggu-kafka-connect-ready)
12. [Register Connectors via REST API (curl)](#12-register-connectors-via-rest-api-curl)
13. [Verifikasi Pipeline Berjalan](#13-verifikasi-pipeline-berjalan)
14. [Monitoring](#14-monitoring)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Arsitektur

```
┌──────────────┐     CDC (pgoutput)      ┌─────────────────────────┐
│  PostgreSQL  │ ──────────────────────► │                         │
│  ride_ops_pg │                         │     Kafka Connect        │
│  5 tables    │                         │  (Debezium + GCS Sink)   │
└──────────────┘                         │                         │
                                         │  ┌─────────────────┐    │
┌──────────────┐   Change Streams (rs0)  │  │  Kafka (KRaft)  │    │
│   MongoDB    │ ──────────────────────► │  │  Schema Registry│    │
│  ride_ops_mg │                         │  └─────────────────┘    │
│  2 collections│                        └──────────┬──────────────┘
└──────────────┘                                    │
                                                    │ GCS Sink
                                                    ▼
                                         ┌─────────────────────┐
                                         │  Google Cloud Storage│
                                         │  gs://YOUR_BUCKET/   │
                                         │  raw/cdc/topic=.../  │
                                         │  year=.../month=...  │
                                         └─────────────────────┘
```

**Topics yang dibuat Debezium:**
| Source | Topic Kafka |
|--------|-------------|
| pg: `public.drivers` | `cdc.public.drivers` |
| pg: `public.passengers` | `cdc.public.passengers` |
| pg: `public.rides` | `cdc.public.rides` |
| pg: `public.vehicle_types` | `cdc.public.vehicle_types` |
| pg: `public.zones` | `cdc.public.zones` |
| mg: `ride_events` | `cdc.ride_ops_mg.ride_events` |
| mg: `driver_location_stream` | `cdc.ride_ops_mg.driver_location_stream` |

---

## 2. Prerequisites

### Software yang harus terinstall:

```bash
# Cek versi
docker --version          # >= 24.x
docker compose version    # >= 2.x (plugin, bukan docker-compose lama)
gcloud --version          # Google Cloud SDK
curl --version
jq --version              # untuk format output JSON
```

### Install Google Cloud SDK (jika belum):

```bash
# Ubuntu / Debian
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz
tar -xf google-cloud-cli-linux-x86_64.tar.gz
./google-cloud-sdk/install.sh
source ~/.bashrc

# Verifikasi
gcloud --version
```

### Login ke gcloud:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

---

## 3. Setup Google Cloud Storage (GCS)

Semua langkah ini dikerjakan di terminal menggunakan `gcloud` CLI.

### 3.1 Buat GCS Bucket

```bash
# Ganti variabel sesuai kebutuhan
export GCS_BUCKET="cdc-raw-pipeline"
export GCP_PROJECT_ID=$(gcloud config get-value project)
export GCS_REGION="asia-southeast2"   # Jakarta

# Buat bucket
gcloud storage buckets create gs://${GCS_BUCKET} \
  --project=${GCP_PROJECT_ID} \
  --location=${GCS_REGION} \
  --uniform-bucket-level-access

# Verifikasi bucket terbuat
gcloud storage buckets describe gs://${GCS_BUCKET}
```

### 3.2 Buat Service Account untuk Kafka Connect

```bash
export SA_NAME="kafka-connect-gcs"
export SA_EMAIL="${SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# Buat service account
gcloud iam service-accounts create ${SA_NAME} \
  --display-name="Kafka Connect GCS Sink" \
  --description="Service account for Kafka Connect to write to GCS"

# Verifikasi
gcloud iam service-accounts describe ${SA_EMAIL}
```

### 3.3 Berikan Permissions ke Service Account

```bash
# Grant Storage Object Creator & Viewer pada bucket
gcloud storage buckets add-iam-policy-binding gs://${GCS_BUCKET} \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectCreator"

gcloud storage buckets add-iam-policy-binding gs://${GCS_BUCKET} \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectViewer"

# Verifikasi IAM binding
gcloud storage buckets get-iam-policy gs://${GCS_BUCKET}
```

### 3.4 Buat dan Download JSON Key

```bash
# Buat direktori credentials di root project
mkdir -p ./credentials

# Generate dan download key
gcloud iam service-accounts keys create ./credentials/gcs-keyfile.json \
  --iam-account=${SA_EMAIL}

# Verifikasi file terbuat
ls -la ./credentials/gcs-keyfile.json
cat ./credentials/gcs-keyfile.json | jq .client_email
```

> **⚠️ PENTING:** Jangan pernah commit file `credentials/` ke git.  
> Tambahkan ke `.gitignore`:

```bash
echo "credentials/" >> .gitignore
echo "*.json" >> .gitignore
```

### 3.5 Buat Folder Struktur di GCS (Opsional)

GCS tidak perlu pre-create folder, tapi ini berguna untuk verifikasi akses:

```bash
# Test tulis ke bucket
echo "test" | gcloud storage cp - gs://${GCS_BUCKET}/raw/cdc/.init

# Verifikasi
gcloud storage ls gs://${GCS_BUCKET}/raw/cdc/

# Hapus test file
gcloud storage rm gs://${GCS_BUCKET}/raw/cdc/.init
```

---

## 4. Siapkan Struktur Project

```bash
# Masuk ke folder project
cd data-pipeline-kafka-GCS

# Lihat struktur saat ini
ls -la
```

Struktur yang ada:
```
data-pipeline-kafka-GCS/
├── connectors/
│   ├── debezium-mongodb-source.json
│   ├── debezium-postgres.json
│   └── minio-sink.json             ← akan diganti dengan gcs-sink.json
├── data-generator/
│   ├── generator/
│   ├── mongodb/
│   └── postgres/
├── kafka-connect/
│   └── Dockerfile                  ← perlu dimodifikasi
└── docker-compose-fix.yaml         ← perlu dimodifikasi
```

---

## 5. Modifikasi Dockerfile Kafka Connect untuk GCS

File asli menginstall `confluentinc/kafka-connect-s3`. Ganti dengan plugin GCS:

```bash
cat > kafka-connect/Dockerfile << 'EOF'
FROM confluentinc/cp-kafka-connect:7.6.0

# Debezium PostgreSQL Connector
RUN confluent-hub install --no-prompt \
    debezium/debezium-connector-postgresql:2.5.4

# Debezium MongoDB Connector
RUN confluent-hub install --no-prompt \
    debezium/debezium-connector-mongodb:2.5.4

# Confluent GCS Sink Connector
RUN confluent-hub install --no-prompt \
    confluentinc/kafka-connect-gcs:5.5.15

# Avro Converter for Schema Registry
RUN confluent-hub install --no-prompt \
    confluentinc/kafka-connect-avro-converter:7.6.0

USER appuser
EOF
```

Verifikasi isi Dockerfile:

```bash
cat kafka-connect/Dockerfile
```

---

## 6. Buat GCS Sink Connector Config

```bash
cat > connectors/gcs-sink.json << 'EOF'
{
  "name": "gcs-sink-parquet",
  "config": {
    "connector.class": "io.confluent.connect.gcs.GcsSinkConnector",
    "tasks.max": "3",
    "topics.regex": "cdc\\.(public\\.(drivers|passengers|rides|vehicle_types|zones)|ride_ops_mg\\.(ride_events|driver_location_stream))",

    "gcs.bucket.name": "cdc-raw-pipeline",
    "gcs.part.size": "5242880",
    "gcs.credentials.path": "/opt/kafka/secrets/gcs-keyfile.json",

    "storage.class": "io.confluent.connect.gcs.storage.GcsStorage",
    "format.class": "io.confluent.connect.gcs.format.parquet.ParquetFormat",
    "parquet.codec": "snappy",

    "flush.size": "1000",
    "rotate.interval.ms": "60000",
    "rotate.schedule.interval.ms": "600000",

    "topics.dir": "raw/cdc",
    "path.format": "'topic='${topic}/'year='YYYY/'month='MM/'day='dd/'hour='HH",
    "locale": "id_ID",
    "timezone": "Asia/Jakarta",
    "timestamp.extractor": "Wallclock",

    "schema.compatibility": "FULL",
    "key.converter": "io.confluent.connect.avro.AvroConverter",
    "key.converter.schema.registry.url": "http://schema-registry:8081",
    "value.converter": "io.confluent.connect.avro.AvroConverter",
    "value.converter.schema.registry.url": "http://schema-registry:8081",

    "confluent.topic.bootstrap.servers": "kafka:29092",
    "confluent.topic.replication.factor": "1",

    "errors.tolerance": "all",
    "errors.log.enable": "true",
    "errors.log.include.messages": "true",
    "errors.deadletterqueue.topic.name": "gcs-sink-dlq",
    "errors.deadletterqueue.topic.replication.factor": "1",
    "errors.deadletterqueue.context.headers.enable": "true",

    "max.retries": "10",
    "retry.backoff.ms": "3000",

    "consumer.override.fetch.min.bytes": "50000",
    "consumer.override.fetch.max.wait.ms": "500"
  }
}
EOF
```

> **Catatan:** Ganti `"gcs.bucket.name"` dengan nama bucket GCS kamu yang sebenarnya.

---

## 7. Update docker-compose untuk GCS

Buat file `docker-compose.yaml` baru (berbasis `docker-compose-fix.yaml`) dengan:
- Hapus service `minio`
- Mount GCS keyfile ke container `kafka-connect`
- Hapus volume `minio-data`

```bash
cat > docker-compose.yaml << 'EOF'
---
services:
  kafka:
    image: confluentinc/cp-kafka:7.6.0
    container_name: kafka
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_LISTENERS: PLAINTEXT://0.0.0.0:29092,PLAINTEXT_HOST://0.0.0.0:9092,CONTROLLER://0.0.0.0:9093
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:29092,PLAINTEXT_HOST://localhost:9092
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT,CONTROLLER:PLAINTEXT
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      CLUSTER_ID: MkU3OEVBNTcwNTJENDM2Qk
      KAFKA_LOG_DIRS: /var/lib/kafka/data
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: 0
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_NUM_PARTITIONS: 3
      KAFKA_DEFAULT_REPLICATION_FACTOR: 1
      KAFKA_MIN_INSYNC_REPLICAS: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"
      KAFKA_LOG_RETENTION_HOURS: 168
      KAFKA_COMPRESSION_TYPE: lz4
      KAFKA_MESSAGE_MAX_BYTES: 10485760
      KAFKA_REPLICA_FETCH_MAX_BYTES: 10485760
    ports:
      - "9092:9092"
      - "29092:29092"
    volumes:
      - kafka-data:/var/lib/kafka/data
    networks:
      - cdc-net
    healthcheck:
      test: ["CMD-SHELL", "kafka-broker-api-versions --bootstrap-server localhost:9092"]
      interval: 15s
      timeout: 10s
      retries: 10
      start_period: 30s

  schema-registry:
    image: confluentinc/cp-schema-registry:7.6.0
    container_name: schema-registry
    depends_on:
      kafka:
        condition: service_healthy
    environment:
      SCHEMA_REGISTRY_HOST_NAME: schema-registry
      SCHEMA_REGISTRY_KAFKASTORE_BOOTSTRAP_SERVERS: kafka:29092
      SCHEMA_REGISTRY_LISTENERS: http://0.0.0.0:8081
      SCHEMA_REGISTRY_KAFKASTORE_TOPIC_REPLICATION_FACTOR: 1
      SCHEMA_REGISTRY_SCHEMA_COMPATIBILITY_LEVEL: full
    ports:
      - "8081:8081"
    networks:
      - cdc-net
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8081/subjects || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 15
      start_period: 20s

  kafka-connect:
    build:
      context: ./kafka-connect
      dockerfile: Dockerfile
    container_name: kafka-connect
    depends_on:
      kafka:
        condition: service_healthy
      schema-registry:
        condition: service_healthy
      postgres-ops:
        condition: service_healthy
      mongodb:
        condition: service_started
    environment:
      CONNECT_BOOTSTRAP_SERVERS: kafka:29092
      CONNECT_REST_ADVERTISED_HOST_NAME: kafka-connect
      CONNECT_REST_PORT: 8083
      CONNECT_GROUP_ID: cdc-connect-cluster
      CONNECT_CONFIG_STORAGE_TOPIC: _connect-configs
      CONNECT_OFFSET_STORAGE_TOPIC: _connect-offsets
      CONNECT_STATUS_STORAGE_TOPIC: _connect-status
      CONNECT_CONFIG_STORAGE_REPLICATION_FACTOR: 1
      CONNECT_OFFSET_STORAGE_REPLICATION_FACTOR: 1
      CONNECT_STATUS_STORAGE_REPLICATION_FACTOR: 1
      CONNECT_OFFSET_FLUSH_INTERVAL_MS: 10000
      CONNECT_KEY_CONVERTER: io.confluent.connect.avro.AvroConverter
      CONNECT_KEY_CONVERTER_SCHEMA_REGISTRY_URL: http://schema-registry:8081
      CONNECT_VALUE_CONVERTER: io.confluent.connect.avro.AvroConverter
      CONNECT_VALUE_CONVERTER_SCHEMA_REGISTRY_URL: http://schema-registry:8081
      CONNECT_PLUGIN_PATH: >-
        /usr/share/java,
        /usr/share/confluent-hub-components,
        /data/connect-jars
      CONNECT_LOG4J_LOGGERS: >-
        org.apache.zookeeper=ERROR,
        org.I0Itec.zkclient=ERROR,
        org.reflections=ERROR
      CONNECT_PRODUCER_COMPRESSION_TYPE: lz4
      CONNECT_PRODUCER_BATCH_SIZE: 65536
      CONNECT_PRODUCER_LINGER_MS: 100
      CONNECT_PRODUCER_MAX_REQUEST_SIZE: 10485760
      CONNECT_CONSUMER_FETCH_MIN_BYTES: 50000
      CONNECT_CONSUMER_FETCH_MAX_WAIT_MS: 500
      # GCS credentials via env (alternatif dari file mount)
      GOOGLE_APPLICATION_CREDENTIALS: /opt/kafka/secrets/gcs-keyfile.json
    ports:
      - "8083:8083"
    volumes:
      # Mount GCS keyfile ke dalam container
      - ./credentials/gcs-keyfile.json:/opt/kafka/secrets/gcs-keyfile.json:ro
    networks:
      - cdc-net
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8083/connectors || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 20
      start_period: 90s

  kafka-ui:
    image: provectuslabs/kafka-ui:latest
    container_name: kafka-ui
    depends_on:
      kafka:
        condition: service_healthy
      schema-registry:
        condition: service_healthy
    environment:
      KAFKA_CLUSTERS_0_NAME: cdc-cluster
      KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS: kafka:29092
      KAFKA_CLUSTERS_0_SCHEMAREGISTRY: http://schema-registry:8081
      KAFKA_CLUSTERS_0_KAFKACONNECT_0_NAME: cdc-connect
      KAFKA_CLUSTERS_0_KAFKACONNECT_0_ADDRESS: http://kafka-connect:8083
      DYNAMIC_CONFIG_ENABLED: "true"
    ports:
      - "8080:8080"
    networks:
      - cdc-net

  postgres-ops:
    image: postgres:15-alpine
    container_name: postgres-ops
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-ride_ops_pg}
      POSTGRES_USER: ${POSTGRES_USER:-postgres}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
    command: >
      postgres
        -c wal_level=logical
        -c max_replication_slots=10
        -c max_wal_senders=10
        -c wal_keep_size=1024
        -c shared_preload_libraries=pgoutput
        -c log_replication_commands=on
    ports:
      - "5435:5432"
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./data-generator/postgres/init_postgres.sql:/docker-entrypoint-initdb.d/01-init.sql:ro
    networks:
      - cdc-net
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-postgres} -d ${POSTGRES_DB:-ride_ops_pg}"]
      interval: 5s
      timeout: 3s
      retries: 20

  mongodb:
    image: mongo:latest
    container_name: mongodb
    command: ["mongod", "--replSet", "rs0", "--bind_ip_all"]
    ports:
      - "27017:27017"
    volumes:
      - mongodb-data:/data/db
    networks:
      - cdc-net

  mongo-express:
    image: mongo-express:latest
    container_name: mongo-express
    restart: always
    environment:
      ME_CONFIG_MONGODB_SERVER: mongodb
      ME_CONFIG_MONGODB_PORT: 27017
      ME_CONFIG_BASICAUTH_USERNAME: ${MONGO_EXPRESS_USERNAME:-express_user}
      ME_CONFIG_BASICAUTH_PASSWORD: ${MONGO_EXPRESS_PASSWORD:-express_password}
    ports:
      - "8082:8081"
    networks:
      - cdc-net
    depends_on:
      - mongodb

  generator:
    build:
      context: ./data-generator/generator
    container_name: taxi-generator
    depends_on:
      postgres-ops:
        condition: service_healthy
      mongodb:
        condition: service_started
    environment:
      DB_HOST: postgres-ops
      DB_PORT: 5432
      DB_NAME: ride_ops_pg
      DB_USER: postgres
      DB_PASSWORD: postgres
      TZ: Asia/Jakarta
      SIM_START_AT: '2026-01-01T00:00:00+07:00'
      SIM_SPEED: '60'
      TICK_SECONDS: '1'
      RIDES_PER_TICK_WEEKDAY_BASE: '2'
      RIDES_PER_TICK_WEEKEND_BASE: '3'
      INITIAL_DRIVERS: '150'
      INITIAL_PASSENGERS: '600'
      MAX_OPEN_RIDES: '400'
      RANDOM_SEED: '42'
      MONGO_URI: 'mongodb://mongodb:27017/?replicaSet=rs0'
      MONGO_DB: 'ride_ops_mg'
      DRIVER_LOCATIONS_PER_MINUTE: '2'
    networks:
      - cdc-net

networks:
  cdc-net:
    driver: bridge

volumes:
  kafka-data:
  postgres-data:
  mongodb-data:
EOF
```

---

## 8. Build dan Jalankan Stack

### 8.1 Build image Kafka Connect (butuh waktu ±5 menit karena download plugin)

```bash
docker compose build kafka-connect
```

Pantau progress download plugin di output. Pastikan keempat plugin terinstall:
- `debezium/debezium-connector-postgresql:2.5.4` ✓
- `debezium/debezium-connector-mongodb:2.5.4` ✓
- `confluentinc/kafka-connect-gcs:5.5.15` ✓
- `confluentinc/kafka-connect-avro-converter:7.6.0` ✓

### 8.2 Jalankan semua service

```bash
docker compose up -d
```

### 8.3 Pantau status startup

```bash
# Lihat semua container dan statusnya
docker compose ps

# Stream logs semua service
docker compose logs -f

# Atau per service
docker compose logs -f kafka
docker compose logs -f kafka-connect
docker compose logs -f postgres-ops
docker compose logs -f mongodb
```

### 8.4 Tunggu semua container healthy

```bash
# Cek sampai semua berstatus healthy (jalankan beberapa kali)
docker compose ps --format "table {{.Name}}\t{{.Status}}"
```

Output yang diharapkan (setelah ±2-3 menit):
```
NAME              STATUS
kafka             Up X minutes (healthy)
schema-registry   Up X minutes (healthy)
kafka-connect     Up X minutes (healthy)
postgres-ops      Up X minutes (healthy)
mongodb           Up X minutes
mongo-express     Up X minutes
kafka-ui          Up X minutes
generator         Up X minutes
```

---

## 9. Init MongoDB Replica Set

MongoDB harus di-init sebagai replica set sebelum Debezium bisa membaca change streams.

### 9.1 Inisialisasi replica set rs0

```bash
docker exec -it mongodb mongosh --eval "
rs.initiate({
  _id: 'rs0',
  members: [{ _id: 0, host: 'mongodb:27017' }]
})
"
```

Output yang diharapkan:
```json
{ "ok": 1 }
```

### 9.2 Verifikasi status replica set

```bash
docker exec -it mongodb mongosh --eval "rs.status().ok"
```

Harus return `1`.

```bash
# Lihat detail status
docker exec -it mongodb mongosh --eval "
rs.status().members.forEach(function(m) {
  print(m.name + ' -> ' + m.stateStr)
})
"
```

Output: `mongodb:27017 -> PRIMARY`

### 9.3 Init collections dan indexes MongoDB

```bash
docker exec -it mongodb mongosh ride_ops_mg --eval "
// Buat collections
db.createCollection('ride_events');
db.createCollection('driver_location_stream');

// Indexes untuk ride_events
db.ride_events.createIndex({ ride_id: 1 });
db.ride_events.createIndex({ event_time: 1 });
db.ride_events.createIndex({ event_type: 1 });
db.ride_events.createIndex({ driver_id: 1 });

// Indexes untuk driver_location_stream
db.driver_location_stream.createIndex({ driver_id: 1 });
db.driver_location_stream.createIndex({ ride_id: 1 });
db.driver_location_stream.createIndex({ event_time: 1 });
db.driver_location_stream.createIndex({ status_context: 1 });

print('MongoDB collections & indexes berhasil dibuat');
"
```

---

## 10. Verifikasi PostgreSQL & Debezium User

Script `init_postgres.sql` sudah otomatis dijalankan saat container pertama kali start. Tapi verifikasi dulu:

### 10.1 Cek user debezium dan permission

```bash
docker exec -it postgres-ops psql -U postgres -d ride_ops_pg -c "
SELECT usename, userepl FROM pg_user WHERE usename = 'debezium';
"
```

Output yang diharapkan:
```
 usename  | userepl
----------+---------
 debezium | t
```

### 10.2 Cek publikasi debezium_pub

```bash
docker exec -it postgres-ops psql -U postgres -d ride_ops_pg -c "
SELECT pubname, puballtables, pubinsert, pubupdate, pubdelete
FROM pg_publication
WHERE pubname = 'debezium_pub';
"
```

### 10.3 Jika publikasi belum ada, buat manual

```bash
docker exec -it postgres-ops psql -U postgres -d ride_ops_pg -c "
CREATE PUBLICATION debezium_pub
FOR TABLE public.drivers, public.passengers, public.rides,
         public.vehicle_types, public.zones;
"
```

### 10.4 Cek tabel sudah ada

```bash
docker exec -it postgres-ops psql -U postgres -d ride_ops_pg -c "
SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;
"
```

Output:
```
    tablename
-----------------
 drivers
 passengers
 rides
 vehicle_types
 zones
```

### 10.5 Cek WAL level = logical

```bash
docker exec -it postgres-ops psql -U postgres -d ride_ops_pg -c "
SHOW wal_level;
"
```

Output harus: `logical`

---

## 11. Tunggu Kafka Connect Ready

Kafka Connect butuh waktu lebih lama untuk start (±1-2 menit setelah Kafka ready).

### 11.1 Poll sampai ready

```bash
# Jalankan ini berulang sampai dapat response JSON
until curl -sf http://localhost:8083/connectors; do
  echo "Menunggu Kafka Connect..."
  sleep 5
done
echo "Kafka Connect READY!"
```

### 11.2 Cek plugin GCS tersedia

```bash
curl -s http://localhost:8083/connector-plugins | jq '[.[] | .class] | sort'
```

Pastikan ada:
```json
"io.confluent.connect.gcs.GcsSinkConnector"
"io.debezium.connector.mongodb.MongoDbConnector"
"io.debezium.connector.postgresql.PostgresConnector"
```

---

## 12. Register Connectors via REST API (curl)

Semua connector didaftarkan ke Kafka Connect REST API di port `8083`.

### 12.1 Register Debezium PostgreSQL Source Connector

```bash
curl -s -X POST http://localhost:8083/connectors \
  -H "Content-Type: application/json" \
  -d @connectors/debezium-postgres.json \
  | jq .
```

Output yang diharapkan:
```json
{
  "name": "debezium-postgres-source",
  "config": { ... },
  "tasks": [],
  "type": "source"
}
```

### 12.2 Register Debezium MongoDB Source Connector

```bash
curl -s -X POST http://localhost:8083/connectors \
  -H "Content-Type: application/json" \
  -d @connectors/debezium-mongodb-source.json \
  | jq .
```

### 12.3 Register GCS Sink Connector

```bash
curl -s -X POST http://localhost:8083/connectors \
  -H "Content-Type: application/json" \
  -d @connectors/gcs-sink.json \
  | jq .
```

### 12.4 Verifikasi semua connector terdaftar

```bash
curl -s http://localhost:8083/connectors | jq .
```

Output:
```json
[
  "debezium-postgres-source",
  "debezium-mongodb-source",
  "gcs-sink-parquet"
]
```

### 12.5 Cek status tiap connector

```bash
# Cek status Postgres connector
curl -s http://localhost:8083/connectors/debezium-postgres-source/status | jq .

# Cek status MongoDB connector
curl -s http://localhost:8083/connectors/debezium-mongodb-source/status | jq .

# Cek status GCS sink
curl -s http://localhost:8083/connectors/gcs-sink-parquet/status | jq .
```

Untuk setiap connector, output yang diharapkan:
```json
{
  "name": "...",
  "connector": {
    "state": "RUNNING",
    "worker_id": "..."
  },
  "tasks": [
    {
      "id": 0,
      "state": "RUNNING",
      "worker_id": "..."
    }
  ],
  "type": "source"
}
```

Jika `state` bukan `RUNNING`, lihat bagian [Troubleshooting](#15-troubleshooting).

---

## 13. Verifikasi Pipeline Berjalan

### 13.1 Cek Kafka topics terbuat

```bash
docker exec -it kafka kafka-topics \
  --bootstrap-server localhost:9092 \
  --list \
  | grep "^cdc\."
```

Output:
```
cdc.public.drivers
cdc.public.passengers
cdc.public.rides
cdc.public.vehicle_types
cdc.public.zones
cdc.ride_ops_mg.driver_location_stream
cdc.ride_ops_mg.ride_events
```

### 13.2 Consume pesan dari topic (spot-check)

```bash
# Lihat beberapa pesan dari topic rides
docker exec -it kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic cdc.public.rides \
  --from-beginning \
  --max-messages 3
```

### 13.3 Cek data masuk ke GCS

```bash
# Tunggu beberapa menit, lalu cek
gcloud storage ls gs://cdc-raw-pipeline/raw/cdc/ --recursive | head -20
```

Output yang diharapkan (setelah ±1-2 menit ada data):
```
gs://cdc-raw-pipeline/raw/cdc/topic=cdc.public.drivers/year=2026/month=04/day=07/hour=12/
gs://cdc-raw-pipeline/raw/cdc/topic=cdc.public.rides/year=2026/month=04/day=07/hour=12/
...
```

### 13.4 Verifikasi file Parquet di GCS

```bash
# List file parquet
gcloud storage ls "gs://cdc-raw-pipeline/raw/cdc/**" | grep ".parquet"

# Download satu file untuk verifikasi
gcloud storage cp \
  $(gcloud storage ls "gs://cdc-raw-pipeline/raw/cdc/topic=cdc.public.drivers/**" | head -1) \
  /tmp/test.parquet

# Baca dengan Python (install pyarrow jika belum)
python3 -c "
import pyarrow.parquet as pq
table = pq.read_table('/tmp/test.parquet')
print(table.schema)
print(table.to_pandas().head())
"
```

---

## 14. Monitoring

### UI yang tersedia:

| Service | URL | Keterangan |
|---------|-----|------------|
| Kafka UI | http://localhost:8080 | Monitor topics, consumers, connectors |
| Kafka Connect REST | http://localhost:8083 | REST API connectors |
| Schema Registry | http://localhost:8081/subjects | Daftar Avro schemas |
| Mongo Express | http://localhost:8082 | Browser MongoDB (user: express_user / express_password) |

### Command monitoring berguna:

```bash
# Lag consumer per topic
docker exec -it kafka kafka-consumer-groups \
  --bootstrap-server localhost:9092 \
  --describe \
  --group connect-gcs-sink-parquet

# Jumlah pesan di topic
docker exec -it kafka kafka-run-class kafka.tools.GetOffsetShell \
  --bootstrap-server localhost:9092 \
  --topic cdc.public.rides \
  --time -1

# Log Kafka Connect secara live
docker compose logs -f kafka-connect

# Cek error di dead letter queue
docker exec -it kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic gcs-sink-dlq \
  --from-beginning \
  --max-messages 5
```

---

## 15. Troubleshooting

### ❌ Connector status FAILED

```bash
# Lihat error detail
curl -s http://localhost:8083/connectors/gcs-sink-parquet/status | jq .tasks

# Restart connector
curl -s -X POST http://localhost:8083/connectors/gcs-sink-parquet/restart

# Restart task spesifik
curl -s -X POST http://localhost:8083/connectors/gcs-sink-parquet/tasks/0/restart
```

### ❌ GCS credentials error

```bash
# Pastikan file key ter-mount dengan benar
docker exec -it kafka-connect ls -la /opt/kafka/secrets/

# Cek isi file
docker exec -it kafka-connect cat /opt/kafka/secrets/gcs-keyfile.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['client_email'])"

# Test akses GCS dari dalam container
docker exec -it kafka-connect \
  curl -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://storage.googleapis.com/storage/v1/b/cdc-raw-pipeline"
```

### ❌ MongoDB Debezium FAILED — replica set not initialized

```bash
# Cek status replica set
docker exec -it mongodb mongosh --eval "rs.status().ok"

# Jika return error, init ulang
docker exec -it mongodb mongosh --eval "
rs.initiate({ _id: 'rs0', members: [{ _id: 0, host: 'mongodb:27017' }] })
"

# Lalu restart connector
curl -s -X POST http://localhost:8083/connectors/debezium-mongodb-source/restart
```

### ❌ PostgreSQL — slot debezium sudah ada (error duplicate slot)

```bash
# Lihat semua replication slots
docker exec -it postgres-ops psql -U postgres -d ride_ops_pg -c "
SELECT slot_name, plugin, active FROM pg_replication_slots;
"

# Hapus slot lama jika ada
docker exec -it postgres-ops psql -U postgres -d ride_ops_pg -c "
SELECT pg_drop_replication_slot('debezium_slot');
"

# Lalu restart connector
curl -s -X POST http://localhost:8083/connectors/debezium-postgres-source/restart
```

### ❌ Kafka Connect tidak mau start (port 8083 tidak respond)

```bash
# Cek logs lengkap
docker compose logs kafka-connect | tail -100

# Pastikan GCS keyfile ada sebelum start
ls -la credentials/gcs-keyfile.json

# Rebuild image jika plugin tidak terinstall
docker compose build --no-cache kafka-connect
docker compose up -d kafka-connect
```

### ❌ Plugin GCS tidak ditemukan di list

```bash
# Cek apakah plugin path benar
docker exec -it kafka-connect ls /usr/share/confluent-hub-components/ | grep gcs

# Jika kosong, rebuild image
docker compose build --no-cache kafka-connect
```

### Hapus semua dan mulai ulang dari awal

```bash
# Stop dan hapus semua container + volumes
docker compose down -v

# Hapus image Kafka Connect untuk rebuild bersih
docker rmi data-pipeline-kafka-gcs-kafka-connect

# Mulai lagi dari langkah 8
docker compose build kafka-connect
docker compose up -d
```

---

## Ringkasan Urutan Setup

```
1. gcloud auth login
2. gcloud storage buckets create gs://cdc-raw-pipeline ...
3. gcloud iam service-accounts create kafka-connect-gcs ...
4. gcloud storage buckets add-iam-policy-binding ...
5. gcloud iam service-accounts keys create ./credentials/gcs-keyfile.json ...
6. Edit kafka-connect/Dockerfile  → install kafka-connect-gcs
7. Buat connectors/gcs-sink.json
8. Buat docker-compose.yaml       → mount gcs-keyfile, hapus minio
9. docker compose build kafka-connect
10. docker compose up -d
11. docker exec mongodb mongosh → rs.initiate(...)
12. Tunggu semua service healthy
13. curl POST /connectors (postgres + mongodb + gcs)
14. Verifikasi: gcloud storage ls gs://cdc-raw-pipeline/raw/cdc/
```