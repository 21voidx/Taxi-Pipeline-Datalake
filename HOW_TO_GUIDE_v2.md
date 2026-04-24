# HOW_TO_GUIDE.md

# End-to-End CDC Taxi Pipeline

Panduan ini menjelaskan setup end-to-end pipeline berikut:

- **PostgreSQL** sebagai sumber data transaksional utama
- **MongoDB** sebagai sumber event dan location stream
- **Debezium + Kafka Connect** untuk CDC
- **Kafka + Schema Registry** untuk transport event Avro
- **MinIO** sebagai raw landing zone Parquet
- **Trino** untuk query lintas PostgreSQL dan MongoDB
- **Python generator** untuk simulasi data ride-hailing real time

---

## 1. Arsitektur yang dipakai

### PostgreSQL
Digunakan untuk data relasional dan current state:
- `zones`
- `vehicle_types`
- `drivers`
- `passengers`
- `rides`

### MongoDB
Digunakan untuk event dan semi-structured stream:
- `ride_events`
- `driver_location_stream`

### Kafka / Connect
- Debezium PostgreSQL Source → Kafka topic CDC Postgres
- Debezium MongoDB Source → Kafka topic CDC MongoDB
- S3/MinIO Sink → tulis raw CDC ke MinIO dalam format **Parquet**

### Trino
Dipakai untuk:
- query lintas PostgreSQL dan MongoDB
- validasi join cepat
- eksplorasi source sebelum dibawa ke warehouse/datamart

---

## 2. Folder tree yang disarankan

```text
project-root/
├── docker-compose.yml
├── .env
├── HOW_TO_GUIDE.md
│
├── kafka-connect/
│   └── Dockerfile
│
├── connectors/
│   ├── debezium-postgres.json
│   ├── debezium-mongodb-source.json
│   └── minio-sink.json
│
├── data-generator/
│   └── generator/
│       ├── Dockerfile
│       ├── requirements-generator.txt
│       └── generator_v3.py
│
├── postgres/
│   └── init/
│       └── init_postgres_revised.sql
│
├── trino/
│   └── etc/
│       ├── config.properties
│       ├── jvm.config
│       ├── node.properties
│       └── catalog/
│           ├── postgresql.properties
│           └── mongodb.properties
│
└── minio/
    └── minio.properties
```

---

## 3. File yang dipakai

### A. Docker Compose
Gunakan file compose yang sudah dimodifikasi agar memuat:
- Kafka
- Schema Registry
- Kafka Connect
- MinIO
- Kafka UI
- PostgreSQL
- MongoDB
- Generator
- Trino

Jika Anda sudah punya compose utama, gunakan versi modifikasi yang menambahkan:
- service `mongodb`
- env MongoDB pada `generator`
- dependency MongoDB pada `kafka-connect`
- service `trino`

---

## 4. Dockerfile Kafka Connect

Pastikan image Kafka Connect memuat plugin berikut:
- Debezium PostgreSQL Connector
- Debezium MongoDB Connector
- S3 Sink Connector
- Avro Converter

Contoh `kafka-connect/Dockerfile`:

```dockerfile
FROM confluentinc/cp-kafka-connect:7.6.0

RUN confluent-hub install --no-prompt debezium/debezium-connector-postgresql:2.5.4
RUN confluent-hub install --no-prompt debezium/debezium-connector-mongodb:2.5.4
RUN confluent-hub install --no-prompt confluentinc/kafka-connect-s3:10.5.13
RUN confluent-hub install --no-prompt confluentinc/kafka-connect-avro-converter:7.6.0

USER appuser
```

---

## 5. Dockerfile Generator

Contoh `data-generator/generator/Dockerfile`:

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Jakarta

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-generator.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY generator_v3.py /app/generator.py

CMD ["python", "-u", "/app/generator.py"]
```

Contoh `requirements-generator.txt`:

```txt
psycopg2-binary
faker
python-dateutil
pymongo
```

---

## 6. Konfigurasi Trino

### `trino/etc/config.properties`

```properties
coordinator=true
node-scheduler.include-coordinator=true
http-server.http.port=8085
query.max-memory=1GB
query.max-memory-per-node=512MB
query.max-total-memory-per-node=768MB
discovery-server.enabled=true
discovery.uri=http://trino:8085
```

### `trino/etc/jvm.config`

```properties
-server
-Xmx2G
-XX:+UseG1GC
-XX:G1HeapRegionSize=32M
-XX:+ExplicitGCInvokesConcurrent
-XX:+ExitOnOutOfMemoryError
```

### `trino/etc/node.properties`

```properties
node.environment=development
node.id=trino-coordinator
node.data-dir=/var/trino/data
```

### `trino/etc/catalog/postgresql.properties`

```properties
connector.name=postgresql
connection-url=jdbc:postgresql://postgres-ops:5432/ride_ops_pg
connection-user=postgres
connection-password=postgres
```

### `trino/etc/catalog/mongodb.properties`

```properties
connector.name=mongodb
mongodb.connection-url=mongodb://mongodb:27017/?replicaSet=rs0
```

---

## 7. Contoh `docker-compose.yml`

> Fokus panduan ini adalah setup MongoDB manual. Jadi **service MongoDB hanya satu**. Inisialisasi replica set dilakukan manual setelah container hidup.

### Service MongoDB

```yaml
mongodb:
  image: mongo:7
  container_name: mongodb
  command: ["mongod", "--replSet", "rs0", "--bind_ip_all"]
  ports:
    - "27017:27017"
  volumes:
    - mongodb-data:/data/db
  networks:
    - cdc-net
```

### Service Trino

```yaml
trino:
  image: trinodb/trino:477
  container_name: trino
  ports:
    - "8085:8085"
  volumes:
    - ./trino/etc:/etc/trino
  depends_on:
    postgres-ops:
      condition: service_healthy
    mongodb:
      condition: service_started
  networks:
    - cdc-net
```

### Update service generator

```yaml
generator:
  build:
    context: ./data-generator/generator
    dockerfile: Dockerfile
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
```

### Update service Kafka Connect

```yaml
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
  ports:
    - "8083:8083"
  networks:
    - cdc-net
```

### Tambahkan volume

```yaml
volumes:
  kafka-data:
  postgres-data:
  minio-data:
  mongodb-data:
```

---

## 8. Step-by-step setup

## Step 1 — Simpan semua file
Pastikan file sudah berada pada folder yang benar sesuai tree di atas.

## Step 2 — Build dan jalankan semua service

```bash
docker compose up -d --build
```

## Step 3 — Cek container hidup

```bash
docker ps
```

Pastikan minimal service ini hidup:
- `kafka`
- `schema-registry`
- `kafka-connect`
- `postgres-ops`
- `mongodb`
- `minio`
- `kafka-ui`
- `generator`
- `trino`

---

## 9. Setup MongoDB manual

Karena MongoDB source connector Debezium membutuhkan **replica set**, Anda harus menginisialisasi replica set secara manual.

### Masuk ke shell MongoDB

```bash
docker exec -it mongodb mongosh
```

### Jalankan inisialisasi replica set

```javascript
rs.initiate({
  _id: "rs0",
  members: [
    { _id: 0, host: "mongodb:27017" }
  ]
})
```

### Cek status

```javascript
rs.status()
```

Jika berhasil, akan terlihat member `PRIMARY`.

> `rs.initiate()` hanya perlu dijalankan sekali selama volume MongoDB belum dihapus.

Keluar dari shell:

```javascript
exit
```

---

## 10. Verifikasi plugin Kafka Connect

Cek plugin yang tersedia:

```bash
curl http://localhost:8083/connector-plugins
```

Pastikan output memuat connector class berikut:
- `io.debezium.connector.postgresql.PostgresConnector`
- `io.debezium.connector.mongodb.MongoDbConnector`
- `io.confluent.connect.s3.S3SinkConnector`

Kalau plugin MongoDB tidak ada, berarti Dockerfile Kafka Connect masih belum memuat plugin MongoDB dan Anda harus rebuild image.

---

## 11. Register connector PostgreSQL

```bash
curl -X POST http://localhost:8083/connectors \
  -H "Content-Type: application/json" \
  -d @connectors/debezium-postgres.json
```

Contoh isi penting:
- `table.include.list` hanya untuk:
  - `public.drivers`
  - `public.passengers`
  - `public.rides`
  - `public.vehicle_types`
  - `public.zones`

`ride_events` **tidak** lagi berasal dari Postgres.

---

## 12. Register connector MongoDB

```bash
curl -X POST http://localhost:8083/connectors \
  -H "Content-Type: application/json" \
  -d @connectors/debezium-mongodb-source.json
```

Contoh config penting:

```json
{
  "name": "debezium-mongodb-source",
  "config": {
    "connector.class": "io.debezium.connector.mongodb.MongoDbConnector",
    "mongodb.connection.string": "mongodb://mongodb:27017/?replicaSet=rs0",
    "mongodb.name": "ride_ops_mg",
    "topic.prefix": "cdc",
    "collection.include.list": "ride_ops_mg.ride_events,ride_ops_mg.driver_location_stream",
    "capture.mode": "change_streams_update_full",
    "transforms": "unwrap",
    "transforms.unwrap.type": "io.debezium.connector.mongodb.transforms.ExtractNewDocumentState",
    "tasks.max": "1"
  }
}
```

---

## 13. Register MinIO sink

```bash
curl -X POST http://localhost:8083/connectors \
  -H "Content-Type: application/json" \
  -d @connectors/minio-sink.json
```

Gunakan regex topic yang mencakup Postgres dan MongoDB:

```json
"topics.regex": "cdc\\.(public\\.(drivers|passengers|rides|vehicle_types|zones)|ride_ops_mg\\.(ride_events|driver_location_stream))"
```

---

## 14. Verifikasi connector status

```bash
curl http://localhost:8083/connectors/debezium-postgres-source/status
curl http://localhost:8083/connectors/debezium-mongodb-source/status
curl http://localhost:8083/connectors/minio-sink-parquet/status
```

Semua task seharusnya `RUNNING`.

---

## 15. Verifikasi topic di Kafka UI

Buka:

- Kafka UI: `http://localhost:8080`
- Schema Registry: `http://localhost:8081`
- Kafka Connect: `http://localhost:8083`
- MinIO Console: `http://localhost:9001`
- Trino: `http://localhost:8085`

Topic yang diharapkan muncul:

### PostgreSQL
- `cdc.public.drivers`
- `cdc.public.passengers`
- `cdc.public.rides`
- `cdc.public.vehicle_types`
- `cdc.public.zones`

### MongoDB
- `cdc.ride_ops_mg.ride_events`
- `cdc.ride_ops_mg.driver_location_stream`

---

## 16. Verifikasi generator

Lihat log generator:

```bash
docker logs -f taxi-generator
```

Yang diharapkan:
- row baru terus masuk ke `rides` di Postgres
- event ride lifecycle masuk ke MongoDB `ride_events`
- GPS ping masuk ke MongoDB `driver_location_stream`

---

## 17. Verifikasi data di PostgreSQL

Masuk ke Postgres:

```bash
docker exec -it postgres-ops psql -U postgres -d ride_ops_pg
```

Contoh query:

```sql
SELECT ride_status, COUNT(*)
FROM rides
GROUP BY 1
ORDER BY 1;
```

```sql
SELECT DATE_TRUNC('hour', requested_at) AS hour_bucket,
       COUNT(*) AS total_rides,
       AVG(total_fare) AS avg_fare
FROM rides
GROUP BY 1
ORDER BY 1 DESC
LIMIT 24;
```

---

## 18. Verifikasi data di MongoDB

Masuk ke MongoDB:

```bash
docker exec -it mongodb mongosh
```

```javascript
use ride_ops_mg

db.ride_events.countDocuments()
db.driver_location_stream.countDocuments()

db.ride_events.find().limit(3).pretty()
db.driver_location_stream.find().limit(3).pretty()
```

---

## 19. Verifikasi file Parquet di MinIO

Masuk ke MinIO Console lalu cek bucket raw CDC.

Path yang diharapkan kira-kira seperti:

```text
raw/cdc/table=cdc.public.rides/year=2026/month=01/day=01/hour=08/
raw/cdc/table=cdc.ride_ops_mg.ride_events/year=2026/month=01/day=01/hour=08/
raw/cdc/table=cdc.ride_ops_mg.driver_location_stream/year=2026/month=01/day=01/hour=08/
```

---

## 20. Query dengan Trino

Masuk ke CLI Trino bila perlu:

```bash
docker exec -it trino trino
```

### Lihat schema PostgreSQL

```sql
SHOW SCHEMAS FROM postgresql;
SHOW TABLES FROM postgresql.public;
```

### Lihat schema MongoDB

```sql
SHOW SCHEMAS FROM mongodb;
SHOW TABLES FROM mongodb.ride_ops_mg;
```

### Query PostgreSQL

```sql
SELECT ride_status, COUNT(*)
FROM postgresql.public.rides
GROUP BY 1
ORDER BY 2 DESC;
```

### Query MongoDB

```sql
SELECT event_type, COUNT(*)
FROM mongodb.ride_ops_mg.ride_events
GROUP BY 1
ORDER BY 2 DESC;
```

### Join lintas source

```sql
SELECT
  r.ride_code,
  r.ride_status,
  e.event_type,
  e.event_time
FROM postgresql.public.rides r
JOIN mongodb.ride_ops_mg.ride_events e
  ON CAST(r.ride_id AS VARCHAR) = e.ride_id
LIMIT 20;
```

### Agregasi gabungan

```sql
SELECT
  r.ride_status,
  COUNT(DISTINCT CAST(r.ride_id AS VARCHAR)) AS total_rides,
  COUNT(*) AS total_events
FROM postgresql.public.rides r
JOIN mongodb.ride_ops_mg.ride_events e
  ON CAST(r.ride_id AS VARCHAR) = e.ride_id
GROUP BY 1
ORDER BY 2 DESC;
```

---

## 21. Troubleshooting

### Connector MongoDB gagal start
Cek:
- replica set belum diinisialisasi
- plugin MongoDB belum ada di Kafka Connect
- connection string belum memakai `?replicaSet=rs0`

### Topic MongoDB tidak muncul
Cek:
- generator benar-benar menulis ke MongoDB
- collection name sesuai dengan `collection.include.list`
- connector MongoDB status `RUNNING`

### MinIO sink tidak menulis Parquet
Cek:
- topic regex sink sudah mencakup topic MongoDB
- connector sink status `RUNNING`
- bucket MinIO sudah ada
- credentials MinIO benar

### Trino tidak bisa query MongoDB
Cek:
- file `mongodb.properties` benar
- Trino bisa menjangkau host `mongodb:27017`
- replica set sudah aktif

---

## 22. Hasil akhir yang diharapkan

Setelah semua langkah selesai, Anda akan punya pipeline seperti ini:

```text
PostgreSQL (rides, drivers, passengers, zones, vehicle_types)
    -> Debezium PostgreSQL
    -> Kafka (Avro)
    -> MinIO Parquet

MongoDB (ride_events, driver_location_stream)
    -> Debezium MongoDB
    -> Kafka (Avro)
    -> MinIO Parquet

PostgreSQL + MongoDB
    -> Trino
    -> Query join / validation / ad hoc analytics
```

Dengan pola ini Anda bisa membangun:
- raw CDC zone di MinIO
- bronze/silver di warehouse
- datamart ride performance
- event analytics
- driver mobility analytics

