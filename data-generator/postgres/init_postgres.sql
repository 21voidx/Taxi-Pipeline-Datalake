-- ══════════════════════════════════════════════════════════════
--  PostgreSQL Schema — Taxi Ride-Hailing CDC Source (Revised)
--  Tables : zones, vehicle_types, drivers, passengers, rides
--  CDC    : debezium user + debezium_pub publication
-- ══════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE USER debezium REPLICATION LOGIN PASSWORD 'debezium';
GRANT CONNECT ON DATABASE ride_ops_pg TO debezium;
GRANT USAGE   ON SCHEMA public TO debezium;
GRANT SELECT  ON ALL TABLES IN SCHEMA public TO debezium;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO debezium;

CREATE TABLE IF NOT EXISTS zones (
    zone_id         SERIAL PRIMARY KEY,
    zone_code       VARCHAR(10)   NOT NULL UNIQUE,
    zone_name       VARCHAR(100)  NOT NULL,
    city            VARCHAR(50)   NOT NULL DEFAULT 'Jakarta',
    latitude        DECIMAL(10,7),
    longitude       DECIMAL(10,7),
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    _source_system  VARCHAR(20)   DEFAULT 'postgres'
);

INSERT INTO zones (zone_code, zone_name, city, latitude, longitude, _source_system) VALUES
  ('JKT-SEL', 'Jakarta Selatan',  'Jakarta',    -6.2615, 106.8106, 'postgres'),
  ('JKT-PUS', 'Jakarta Pusat',    'Jakarta',    -6.1745, 106.8227, 'postgres'),
  ('JKT-BAR', 'Jakarta Barat',    'Jakarta',    -6.1682, 106.7632, 'postgres'),
  ('JKT-TIM', 'Jakarta Timur',    'Jakarta',    -6.2250, 106.9004, 'postgres'),
  ('JKT-UTA', 'Jakarta Utara',    'Jakarta',    -6.1382, 106.8833, 'postgres'),
  ('TGR',     'Tangerang',        'Banten',     -6.1783, 106.6319, 'postgres'),
  ('BKS',     'Bekasi',           'Jawa Barat', -6.2383, 106.9756, 'postgres'),
  ('DPK',     'Depok',            'Jawa Barat', -6.4025, 106.7942, 'postgres'),
  ('BSD',     'BSD City',         'Banten',     -6.2994, 106.6535, 'postgres'),
  ('KWN',     'Kawasan Niaga',    'Jakarta',    -6.2088, 106.8456, 'postgres')
ON CONFLICT (zone_code) DO NOTHING;

CREATE TABLE IF NOT EXISTS vehicle_types (
    vehicle_type_id   SERIAL PRIMARY KEY,
    type_code         VARCHAR(20)   NOT NULL UNIQUE,
    type_name         VARCHAR(50)   NOT NULL,
    base_fare         DECIMAL(10,2) NOT NULL,
    per_km_rate       DECIMAL(10,2) NOT NULL,
    per_minute_rate   DECIMAL(10,2) NOT NULL,
    capacity          INT           NOT NULL DEFAULT 4,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    _source_system    VARCHAR(20)   DEFAULT 'postgres'
);

INSERT INTO vehicle_types (type_code, type_name, base_fare, per_km_rate, per_minute_rate, capacity, _source_system) VALUES
  ('MOTOR',   'Motor',           4000,  1500, 200, 1, 'postgres'),
  ('ECONOMI', 'Mobil Ekonomi',   8000,  2500, 400, 4, 'postgres'),
  ('COMFORT', 'Mobil Comfort',   12000, 3500, 500, 4, 'postgres'),
  ('SUV',     'SUV / XL',        15000, 4500, 600, 6, 'postgres'),
  ('PREMIUM', 'Mobil Premium',   25000, 6000, 800, 4, 'postgres')
ON CONFLICT (type_code) DO NOTHING;

CREATE TABLE IF NOT EXISTS drivers (
    driver_id         UUID          PRIMARY KEY DEFAULT uuid_generate_v4(),
    driver_code       VARCHAR(20)   NOT NULL UNIQUE,
    full_name         VARCHAR(100)  NOT NULL,
    phone_number      VARCHAR(20)   NOT NULL,
    email             VARCHAR(100),
    nik               VARCHAR(20),
    sim_number        VARCHAR(20),
    license_plate     VARCHAR(15)   NOT NULL,
    vehicle_type_id   INT           REFERENCES vehicle_types(vehicle_type_id),
    vehicle_brand     VARCHAR(50),
    vehicle_model     VARCHAR(50),
    vehicle_year      INT,
    vehicle_color     VARCHAR(30),
    home_zone_id      INT           REFERENCES zones(zone_id),
    rating            DECIMAL(3,2)  DEFAULT 5.00,
    total_trips       INT           DEFAULT 0,
    status            VARCHAR(20)   DEFAULT 'ACTIVE'
                          CHECK (status IN ('ACTIVE','INACTIVE','SUSPENDED','PENDING')),
    joined_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    _source_system    VARCHAR(20)   DEFAULT 'postgres'
);

CREATE INDEX idx_drivers_status  ON drivers(status);
CREATE INDEX idx_drivers_zone    ON drivers(home_zone_id);
CREATE INDEX idx_drivers_updated ON drivers(updated_at);

CREATE TABLE IF NOT EXISTS passengers (
    passenger_id      UUID          PRIMARY KEY DEFAULT uuid_generate_v4(),
    passenger_code    VARCHAR(20)   NOT NULL UNIQUE,
    full_name         VARCHAR(100)  NOT NULL,
    phone_number      VARCHAR(20)   NOT NULL,
    email             VARCHAR(100),
    birth_date        DATE,
    gender            VARCHAR(10)
                          CHECK (gender IN ('M','F','OTHER','UNKNOWN')),
    home_zone_id      INT           REFERENCES zones(zone_id),
    referral_code     VARCHAR(20),
    is_verified       BOOLEAN       DEFAULT FALSE,
    total_trips       INT           DEFAULT 0,
    status            VARCHAR(20)   DEFAULT 'ACTIVE'
                          CHECK (status IN ('ACTIVE','INACTIVE','BANNED')),
    registered_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    _source_system    VARCHAR(20)   DEFAULT 'postgres'
);

CREATE INDEX idx_passengers_status  ON passengers(status);
CREATE INDEX idx_passengers_updated ON passengers(updated_at);

CREATE TABLE IF NOT EXISTS rides (
    ride_id             UUID          PRIMARY KEY DEFAULT uuid_generate_v4(),
    ride_code           VARCHAR(30)   NOT NULL UNIQUE,
    driver_id           UUID          REFERENCES drivers(driver_id),
    passenger_id        UUID          REFERENCES passengers(passenger_id),
    vehicle_type_id     INT           REFERENCES vehicle_types(vehicle_type_id),
    pickup_zone_id      INT           REFERENCES zones(zone_id),
    dropoff_zone_id     INT           REFERENCES zones(zone_id),
    pickup_address      TEXT,
    dropoff_address     TEXT,
    pickup_lat          DECIMAL(10,7),
    pickup_lon          DECIMAL(10,7),
    dropoff_lat         DECIMAL(10,7),
    dropoff_lon         DECIMAL(10,7),
    requested_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    accepted_at         TIMESTAMPTZ,
    picked_up_at        TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    distance_km         DECIMAL(8,2),
    duration_minutes    INT,
    base_fare           DECIMAL(10,2),
    distance_fare       DECIMAL(10,2),
    time_fare           DECIMAL(10,2),
    surge_multiplier    DECIMAL(4,2)  DEFAULT 1.00,
    promo_discount      DECIMAL(10,2) DEFAULT 0,
    total_fare          DECIMAL(10,2),
    ride_status         VARCHAR(20)   DEFAULT 'REQUESTED'
                            CHECK (ride_status IN
                              ('REQUESTED','ACCEPTED','PICKED_UP','COMPLETED','CANCELLED','NO_DRIVER')),
    cancellation_reason VARCHAR(100),
    passenger_rating    DECIMAL(3,1),
    driver_rating       DECIMAL(3,1),
    notes               TEXT,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    _source_system      VARCHAR(20)   DEFAULT 'postgres'
);

CREATE INDEX idx_rides_driver    ON rides(driver_id);
CREATE INDEX idx_rides_passenger ON rides(passenger_id);
CREATE INDEX idx_rides_status    ON rides(ride_status);
CREATE INDEX idx_rides_requested ON rides(requested_at);
CREATE INDEX idx_rides_updated   ON rides(updated_at);

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_drivers_updated ON drivers;
CREATE TRIGGER trg_drivers_updated
    BEFORE UPDATE ON drivers
    FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();

DROP TRIGGER IF EXISTS trg_passengers_updated ON passengers;
CREATE TRIGGER trg_passengers_updated
    BEFORE UPDATE ON passengers
    FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();

DROP TRIGGER IF EXISTS trg_rides_updated ON rides;
CREATE TRIGGER trg_rides_updated
    BEFORE UPDATE ON rides
    FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'debezium_pub') THEN
        EXECUTE 'DROP PUBLICATION debezium_pub';
    END IF;
END
$$;

CREATE PUBLICATION debezium_pub FOR TABLE
    zones, vehicle_types, drivers, passengers, rides;
