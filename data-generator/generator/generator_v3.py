import os
import time
import math
import random
import uuid
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

import psycopg2
from faker import Faker
from psycopg2.extras import RealDictCursor, execute_batch
from dateutil import parser as dtparser
from pymongo import MongoClient, ASCENDING

faker = Faker("id_ID")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "postgres-ops"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "ride_ops_pg"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "postgres"),
}

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongodb:27017/?replicaSet=rs0")
MONGO_DB = os.getenv("MONGO_DB", "ride_ops_mg")
MONGO_RIDE_EVENTS_COLLECTION = os.getenv("MONGO_RIDE_EVENTS_COLLECTION", "ride_events")
MONGO_DRIVER_LOCATIONS_COLLECTION = os.getenv("MONGO_DRIVER_LOCATIONS_COLLECTION", "driver_location_stream")
DRIVER_LOCATIONS_PER_MINUTE = int(os.getenv("DRIVER_LOCATIONS_PER_MINUTE", "2"))

SIM_START_AT = dtparser.isoparse(os.getenv("SIM_START_AT", "2026-01-01T00:00:00+07:00"))
SIM_SPEED = float(os.getenv("SIM_SPEED", "60"))
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "1"))
INITIAL_DRIVERS = int(os.getenv("INITIAL_DRIVERS", "150"))
INITIAL_PASSENGERS = int(os.getenv("INITIAL_PASSENGERS", "600"))
RIDES_PER_TICK_WEEKDAY_BASE = int(os.getenv("RIDES_PER_TICK_WEEKDAY_BASE", "2"))
RIDES_PER_TICK_WEEKEND_BASE = int(os.getenv("RIDES_PER_TICK_WEEKEND_BASE", "3"))
MAX_OPEN_RIDES = int(os.getenv("MAX_OPEN_RIDES", "400"))
RANDOM_SEED = int(os.getenv("RANDOM_SEED", "42"))
RECOVERY_LOOKBACK_DAYS = int(os.getenv("RECOVERY_LOOKBACK_DAYS", "14"))
GPS_JITTER = float(os.getenv("GPS_JITTER", "0.003"))

random.seed(RANDOM_SEED)
Faker.seed(RANDOM_SEED)

RIDE_STATUS_REQUESTED = "REQUESTED"
RIDE_STATUS_ACCEPTED = "ACCEPTED"
RIDE_STATUS_PICKED_UP = "PICKED_UP"
RIDE_STATUS_COMPLETED = "COMPLETED"
RIDE_STATUS_CANCELLED = "CANCELLED"
RIDE_STATUS_NO_DRIVER = "NO_DRIVER"

TERMINAL_STATUSES = {RIDE_STATUS_COMPLETED, RIDE_STATUS_CANCELLED, RIDE_STATUS_NO_DRIVER}
RIDE_CODE_PATTERN = re.compile(r"^RIDE-(\d{8})-(\d{6})$")

ADDRESSES = {
    "JKT-SEL": [
        "Jl. TB Simatupang No. 18, Cilandak",
        "Jl. Kemang Raya No. 12, Kemang",
        "Jl. Panglima Polim No. 7, Kebayoran Baru",
        "Jl. Fatmawati No. 45, Cipete"
    ],
    "JKT-PUS": [
        "Jl. MH Thamrin No. 1, Menteng",
        "Jl. Sudirman Kav. 10, Tanah Abang",
        "Jl. Kramat Raya No. 88, Senen",
        "Jl. Cikini Raya No. 21, Menteng"
    ],
    "JKT-BAR": [
        "Jl. Daan Mogot KM 10, Cengkareng",
        "Jl. Panjang No. 23, Kebon Jeruk",
        "Jl. S. Parman No. 55, Palmerah",
        "Jl. Tanjung Duren Raya No. 6, Grogol"
    ],
    "JKT-TIM": [
        "Jl. Pemuda No. 101, Rawamangun",
        "Jl. Raya Bogor KM 21, Kramat Jati",
        "Jl. DI Panjaitan No. 9, Cawang",
        "Jl. Otista Raya No. 30, Jatinegara"
    ],
    "JKT-UTA": [
        "Jl. Yos Sudarso No. 15, Sunter",
        "Jl. Boulevard Barat No. 2, Kelapa Gading",
        "Jl. RE Martadinata No. 90, Ancol",
        "Jl. Pluit Karang Indah No. 11, Pluit"
    ],
    "TGR": [
        "Jl. Jenderal Sudirman No. 30, Tangerang",
        "Jl. MH Thamrin No. 8, Cikokol",
        "Jl. Imam Bonjol No. 14, Karawaci"
    ],
    "BKS": [
        "Jl. Ahmad Yani No. 77, Bekasi Selatan",
        "Jl. Harapan Indah Boulevard No. 5, Medan Satria",
        "Jl. Kalimalang No. 90, Bekasi Timur"
    ],
    "DPK": [
        "Jl. Margonda Raya No. 99, Beji",
        "Jl. Raya Sawangan No. 45, Pancoran Mas",
        "Jl. Juanda No. 18, Sukmajaya"
    ],
    "BSD": [
        "Jl. BSD Green Office Park No. 1, BSD",
        "Jl. Serpong Boulevard No. 10, Serpong",
        "Jl. Pahlawan Seribu No. 3, BSD"
    ],
    "KWN": [
        "Jl. Gatot Subroto No. 50, Kuningan",
        "Jl. HR Rasuna Said No. 33, Setiabudi",
        "Jl. Prof. Dr. Satrio No. 5, Kuningan"
    ]
}

EVENT_TYPES = {
    RIDE_STATUS_REQUESTED: "RIDE_REQUESTED",
    RIDE_STATUS_ACCEPTED: "RIDE_ACCEPTED",
    RIDE_STATUS_PICKED_UP: "RIDE_PICKED_UP",
    RIDE_STATUS_COMPLETED: "RIDE_COMPLETED",
    RIDE_STATUS_CANCELLED: "RIDE_CANCELLED",
    RIDE_STATUS_NO_DRIVER: "RIDE_NO_DRIVER",
}


@dataclass
class ScheduledTransition:
    ride_id: str
    ride_code: str
    next_status: str
    due_at: datetime
    driver_id: Optional[str] = None
    cancellation_reason: Optional[str] = None
    meta: Dict = field(default_factory=dict)


def to_money(value: float) -> Decimal:
    return Decimal(str(round(value, 2))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def now_iso(dt: datetime) -> str:
    return dt.isoformat()


def compute_demand_multiplier(sim_now: datetime) -> float:
    hour = sim_now.hour
    weekday = sim_now.weekday()
    weekend = weekday >= 5

    base = 0.5
    if 6 <= hour < 9:
        base = 1.8
    elif 9 <= hour < 11:
        base = 1.2
    elif 11 <= hour < 14:
        base = 1.4
    elif 14 <= hour < 16:
        base = 1.0
    elif 16 <= hour < 20:
        base = 2.1
    elif 20 <= hour < 23:
        base = 1.6
    else:
        base = 0.6

    if weekend:
        if 10 <= hour < 22:
            base += 0.3
        else:
            base += 0.1

    return max(0.3, base + random.uniform(-0.15, 0.15))


def surge_multiplier(demand_multiplier: float, pickup_zone_code: str, sim_now: datetime) -> Decimal:
    rush = 1.0
    if pickup_zone_code in {"KWN", "JKT-PUS", "JKT-SEL", "BSD"}:
        rush += 0.08
    if sim_now.hour in {7, 8, 17, 18, 19}:
        rush += 0.12
    mult = min(2.20, max(1.0, demand_multiplier * rush / 1.4))
    return to_money(mult)


def choose_zone_pair(zones: List[Dict], sim_now: datetime):
    hour = sim_now.hour
    weekday = sim_now.weekday()

    pickup_weights = {}
    for z in zones:
        code = z["zone_code"]
        w = 1.0
        if hour in {6, 7, 8, 9}:
            if code in {"BKS", "DPK", "TGR", "BSD", "JKT-SEL"}:
                w = 2.2
            if code == "KWN":
                w = 0.7
        elif hour in {17, 18, 19, 20}:
            if code in {"KWN", "JKT-PUS", "JKT-SEL", "BSD"}:
                w = 2.3
            if code in {"BKS", "DPK", "TGR"}:
                w = 0.9
        elif 11 <= hour <= 13:
            if code in {"KWN", "JKT-PUS", "JKT-BAR"}:
                w = 1.6
        elif 22 <= hour or hour <= 1:
            if code in {"JKT-SEL", "JKT-PUS", "KWN"}:
                w = 1.5
        if weekday >= 5 and code in {"BSD", "JKT-SEL", "JKT-UTA"}:
            w += 0.2
        pickup_weights[code] = w

    pickup = random.choices(zones, weights=[pickup_weights[z["zone_code"]] for z in zones], k=1)[0]

    dropoff_candidates = [z for z in zones if z["zone_id"] != pickup["zone_id"]]
    dropoff_weights = []
    for z in dropoff_candidates:
        code = z["zone_code"]
        w = 1.0
        if pickup["zone_code"] in {"BKS", "DPK", "TGR", "BSD"} and code in {"KWN", "JKT-PUS", "JKT-SEL", "JKT-BAR"} and sim_now.hour < 11:
            w = 2.4
        if pickup["zone_code"] in {"KWN", "JKT-PUS", "JKT-SEL", "JKT-BAR"} and code in {"BKS", "DPK", "TGR", "BSD"} and sim_now.hour >= 16:
            w = 2.3
        if pickup["zone_code"] in {"JKT-PUS", "KWN"} and code in {"JKT-SEL", "JKT-BAR", "JKT-TIM"}:
            w += 0.4
        dropoff_weights.append(w)

    dropoff = random.choices(dropoff_candidates, weights=dropoff_weights, k=1)[0]
    return pickup, dropoff


def random_lat_lon(zone: Dict):
    lat = float(zone["latitude"]) + random.uniform(-0.02, 0.02)
    lon = float(zone["longitude"]) + random.uniform(-0.02, 0.02)
    return round(lat, 7), round(lon, 7)


def estimate_distance_km(pickup: Dict, dropoff: Dict) -> float:
    lat1, lon1 = float(pickup["latitude"]), float(pickup["longitude"])
    lat2, lon2 = float(dropoff["latitude"]), float(dropoff["longitude"])
    approx = math.sqrt((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2) * 111
    traffic_bias = random.uniform(0.85, 1.35)
    minimum = random.uniform(1.2, 3.5)
    return round(max(minimum, approx * traffic_bias), 2)


def estimate_duration_minutes(distance_km: float, sim_now: datetime) -> int:
    hour = sim_now.hour
    base_speed = 28.0
    if hour in {7, 8, 17, 18, 19}:
        base_speed = 18.0
    elif hour in {10, 11, 12, 13, 14}:
        base_speed = 24.0
    elif hour >= 22 or hour <= 4:
        base_speed = 35.0
    duration = (distance_km / base_speed) * 60 * random.uniform(0.9, 1.25)
    return max(6, int(round(duration)))


def generate_ride_code(seq_num: int, sim_now: datetime) -> str:
    return f"RIDE-{sim_now.strftime('%Y%m%d')}-{seq_num:06d}"


class TaxiGenerator:
    def __init__(self):
        self.conn = None
        self.mongo_client = None
        self.mongo_db = None
        self.mongo_ride_events = None
        self.mongo_driver_locations = None
        self.zones: List[Dict] = []
        self.vehicle_types: List[Dict] = []
        self.vehicle_type_map: Dict[int, Dict] = {}
        self.zone_map: Dict[int, Dict] = {}
        self.drivers: List[Dict] = []
        self.passengers: List[Dict] = []
        self.available_driver_ids = set()
        self.pending: List[ScheduledTransition] = []
        self.seq_num = 0
        self.open_rides = 0
        self.real_start = time.time()
        self.sim_anchor = SIM_START_AT
        self.recovered_open_ride_ids = set()
        self.last_location_emit_at: Dict[str, datetime] = {}

    def connect(self):
        while True:
            try:
                self.conn = psycopg2.connect(**DB_CONFIG)
                self.conn.autocommit = False
                print("Connected to PostgreSQL")
                return
            except Exception as exc:
                print(f"Waiting for PostgreSQL: {exc}")
                time.sleep(3)

    def connect_mongo(self):
        while True:
            try:
                self.mongo_client = MongoClient(MONGO_URI)
                self.mongo_db = self.mongo_client[MONGO_DB]
                self.mongo_ride_events = self.mongo_db[MONGO_RIDE_EVENTS_COLLECTION]
                self.mongo_driver_locations = self.mongo_db[MONGO_DRIVER_LOCATIONS_COLLECTION]

                self.mongo_ride_events.create_index([("ride_id", ASCENDING)])
                self.mongo_ride_events.create_index([("event_time", ASCENDING)])
                self.mongo_ride_events.create_index([("event_type", ASCENDING)])
                self.mongo_ride_events.create_index([("driver_id", ASCENDING)])

                self.mongo_driver_locations.create_index([("driver_id", ASCENDING)])
                self.mongo_driver_locations.create_index([("ride_id", ASCENDING)])
                self.mongo_driver_locations.create_index([("event_time", ASCENDING)])
                self.mongo_driver_locations.create_index([("status_context", ASCENDING)])

                print("Connected to MongoDB")
                return
            except Exception as exc:
                print(f"Waiting for MongoDB: {exc}")
                time.sleep(3)

    def fetchall(self, query, params=None):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())
            return cur.fetchall()

    def execute(self, query, params=None):
        with self.conn.cursor() as cur:
            cur.execute(query, params or ())

    def seed_master_cache(self):
        self.zones = self.fetchall("SELECT zone_id, zone_code, zone_name, latitude, longitude FROM zones ORDER BY zone_id")
        self.vehicle_types = self.fetchall(
            "SELECT vehicle_type_id, type_code, type_name, base_fare, per_km_rate, per_minute_rate FROM vehicle_types ORDER BY vehicle_type_id"
        )
        self.zone_map = {int(z["zone_id"]): z for z in self.zones}
        self.vehicle_type_map = {int(v["vehicle_type_id"]): v for v in self.vehicle_types}

    def ensure_entities(self):
        self.seed_master_cache()
        existing_drivers = self.fetchall("SELECT driver_id, driver_code, vehicle_type_id, home_zone_id, status FROM drivers")
        existing_passengers = self.fetchall("SELECT passenger_id, passenger_code, home_zone_id, status FROM passengers")

        if len(existing_drivers) < INITIAL_DRIVERS:
            self.insert_drivers(INITIAL_DRIVERS - len(existing_drivers))
        if len(existing_passengers) < INITIAL_PASSENGERS:
            self.insert_passengers(INITIAL_PASSENGERS - len(existing_passengers))

        self.drivers = self.fetchall(
            "SELECT driver_id, driver_code, vehicle_type_id, home_zone_id, status, total_trips FROM drivers WHERE status='ACTIVE' ORDER BY driver_code"
        )
        self.passengers = self.fetchall(
            "SELECT passenger_id, passenger_code, home_zone_id, status, total_trips FROM passengers WHERE status='ACTIVE' ORDER BY passenger_code"
        )
        self.available_driver_ids = {str(d["driver_id"]) for d in self.drivers}

    def insert_drivers(self, count: int):
        print(f"Seeding {count} drivers...")
        rows = []
        start_idx = self.fetchall("SELECT COUNT(*) AS cnt FROM drivers")[0]["cnt"]
        for i in range(1, count + 1):
            idx = start_idx + i
            zone = random.choice(self.zones)
            vehicle = random.choice(self.vehicle_types)
            rows.append((
                f"DRV-{idx:05d}",
                faker.name(),
                faker.phone_number()[:18],
                f"driver{idx}@demo.local",
                str(random.randint(1000000000000000, 9999999999999999)),
                f"SIM{random.randint(1000000, 9999999)}",
                f"B {random.randint(1000, 9999)} {random.choice(['ABC','DEF','GHI','JKL'])}",
                vehicle["vehicle_type_id"],
                random.choice(["Toyota", "Honda", "Daihatsu", "Suzuki", "Yamaha", "Honda Motor"]),
                random.choice(["Avanza", "Brio", "Xenia", "Beat", "NMax", "Xpander", "Ertiga"]),
                random.randint(2017, 2025),
                random.choice(["Hitam", "Putih", "Silver", "Merah", "Biru"]),
                zone["zone_id"],
                Decimal(str(round(random.uniform(4.6, 5.0), 2))),
                random.randint(0, 2500),
                "ACTIVE",
                SIM_START_AT - timedelta(days=random.randint(30, 900)),
                SIM_START_AT - timedelta(days=random.randint(0, 30)),
                "postgres",
            ))
        query = """
            INSERT INTO drivers (
                driver_code, full_name, phone_number, email, nik, sim_number, license_plate,
                vehicle_type_id, vehicle_brand, vehicle_model, vehicle_year, vehicle_color,
                home_zone_id, rating, total_trips, status, joined_at, updated_at, _source_system
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        with self.conn.cursor() as cur:
            execute_batch(cur, query, rows, page_size=200)
        self.conn.commit()

    def insert_passengers(self, count: int):
        print(f"Seeding {count} passengers...")
        rows = []
        start_idx = self.fetchall("SELECT COUNT(*) AS cnt FROM passengers")[0]["cnt"]
        for i in range(1, count + 1):
            idx = start_idx + i
            zone = random.choice(self.zones)
            rows.append((
                f"PSG-{idx:05d}",
                faker.name(),
                faker.phone_number()[:18],
                f"passenger{idx}@demo.local",
                faker.date_of_birth(minimum_age=18, maximum_age=60),
                random.choice(["M", "F", "OTHER", "UNKNOWN"]),
                zone["zone_id"],
                None if random.random() < 0.75 else f"REF{random.randint(1000,9999)}",
                random.random() < 0.82,
                random.randint(0, 800),
                "ACTIVE",
                SIM_START_AT - timedelta(days=random.randint(10, 800)),
                SIM_START_AT - timedelta(days=random.randint(0, 20)),
                "postgres",
            ))
        query = """
            INSERT INTO passengers (
                passenger_code, full_name, phone_number, email, birth_date, gender,
                home_zone_id, referral_code, is_verified, total_trips, status, registered_at, updated_at, _source_system
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        with self.conn.cursor() as cur:
            execute_batch(cur, query, rows, page_size=200)
        self.conn.commit()

    def get_max_existing_sequence(self) -> int:
        rows = self.fetchall("SELECT ride_code FROM rides WHERE ride_code LIKE 'RIDE-%%'")
        max_seq = 0
        for row in rows:
            ride_code = row["ride_code"]
            if not ride_code:
                continue
            m = RIDE_CODE_PATTERN.match(ride_code)
            if m:
                max_seq = max(max_seq, int(m.group(2)))
        return max_seq

    def get_last_event_time(self) -> Optional[datetime]:
        row = self.fetchall(
            """
            SELECT GREATEST(
                COALESCE(MAX(updated_at), TIMESTAMPTZ '2026-01-01 00:00:00+07'),
                COALESCE(MAX(created_at), TIMESTAMPTZ '2026-01-01 00:00:00+07'),
                COALESCE(MAX(requested_at), TIMESTAMPTZ '2026-01-01 00:00:00+07')
            ) AS last_ts
            FROM rides
            """
        )[0]
        return row["last_ts"]

    def restore_runtime_state(self):
        self.seq_num = self.get_max_existing_sequence()
        last_ts = self.get_last_event_time()
        if last_ts and last_ts > SIM_START_AT:
            self.sim_anchor = last_ts + timedelta(seconds=max(1, int(TICK_SECONDS * SIM_SPEED)))
        else:
            self.sim_anchor = SIM_START_AT
        self.real_start = time.time()

        open_rows = self.fetchall(
            """
            SELECT ride_id, ride_code, ride_status, requested_at, accepted_at, picked_up_at,
                   driver_id, passenger_id, pickup_zone_id, dropoff_zone_id, vehicle_type_id,
                   distance_km, duration_minutes, total_fare, surge_multiplier,
                   cancellation_reason, updated_at
            FROM rides
            WHERE ride_status NOT IN ('COMPLETED', 'CANCELLED', 'NO_DRIVER')
              AND requested_at >= %s
            ORDER BY requested_at
            """,
            (self.sim_anchor - timedelta(days=RECOVERY_LOOKBACK_DAYS),)
        )

        self.pending = []
        self.recovered_open_ride_ids = set()
        busy_driver_ids = set()
        for row in open_rows:
            ride_id = str(row["ride_id"])
            ride_code = row["ride_code"]
            ride_status = row["ride_status"]
            driver_id = str(row["driver_id"]) if row["driver_id"] else None
            requested_at = row["requested_at"]
            accepted_at = row["accepted_at"]
            picked_up_at = row["picked_up_at"]
            pickup_zone = self.zone_map.get(int(row["pickup_zone_id"]))
            dropoff_zone = self.zone_map.get(int(row["dropoff_zone_id"]))
            distance_km = float(row["distance_km"] or 0)
            duration_minutes = int(row["duration_minutes"] or 10)
            total_fare = float(row["total_fare"] or 0)
            surge = float(row["surge_multiplier"] or 1.0)
            self.recovered_open_ride_ids.add(ride_id)
            if driver_id:
                busy_driver_ids.add(driver_id)

            common_meta = {
                "pickup_zone": pickup_zone,
                "dropoff_zone": dropoff_zone,
                "requested_at": requested_at,
                "distance_km": distance_km,
                "duration_minutes": duration_minutes,
                "total_fare": total_fare,
                "surge_multiplier": surge,
                "passenger_id": str(row["passenger_id"]) if row.get("passenger_id") else None,
            }

            if ride_status == RIDE_STATUS_REQUESTED:
                if driver_id:
                    accepted_due = max(self.sim_anchor, (accepted_at or requested_at) + timedelta(seconds=random.randint(15, 75)))
                    self.pending.append(ScheduledTransition(
                        ride_id=ride_id,
                        ride_code=ride_code,
                        next_status=RIDE_STATUS_ACCEPTED,
                        due_at=accepted_due,
                        driver_id=driver_id,
                        meta=common_meta,
                    ))
                else:
                    if random.random() < 0.12:
                        self.pending.append(ScheduledTransition(
                            ride_id=ride_id,
                            ride_code=ride_code,
                            next_status=RIDE_STATUS_NO_DRIVER,
                            due_at=max(self.sim_anchor, requested_at + timedelta(seconds=random.randint(30, 180))),
                            meta={"requested_at": requested_at, "passenger_id": common_meta["passenger_id"]},
                        ))
            elif ride_status == RIDE_STATUS_ACCEPTED and driver_id:
                pickup_due = max(self.sim_anchor, (accepted_at or requested_at) + timedelta(seconds=random.randint(90, 420)))
                self.pending.append(ScheduledTransition(
                    ride_id=ride_id,
                    ride_code=ride_code,
                    next_status=RIDE_STATUS_PICKED_UP,
                    due_at=pickup_due,
                    driver_id=driver_id,
                    meta={
                        **common_meta,
                        "accepted_at": accepted_at or requested_at,
                    }
                ))
            elif ride_status == RIDE_STATUS_PICKED_UP and driver_id:
                actual_duration = max(5, int(round(duration_minutes * random.uniform(0.85, 1.20))))
                complete_due = max(self.sim_anchor, (picked_up_at or accepted_at or requested_at) + timedelta(minutes=actual_duration))
                self.pending.append(ScheduledTransition(
                    ride_id=ride_id,
                    ride_code=ride_code,
                    next_status=RIDE_STATUS_COMPLETED,
                    due_at=complete_due,
                    driver_id=driver_id,
                    meta={
                        **common_meta,
                        "accepted_at": accepted_at or requested_at,
                        "picked_up_at": picked_up_at or accepted_at or requested_at,
                        "actual_duration_minutes": actual_duration,
                    }
                ))

        self.open_rides = len(open_rows)
        self.available_driver_ids -= busy_driver_ids
        print(
            f"Recovered state: sim_anchor={now_iso(self.sim_anchor)} seq_num={self.seq_num} "
            f"open_rides={self.open_rides} pending={len(self.pending)} busy_drivers={len(busy_driver_ids)}"
        )

    def simulated_now(self) -> datetime:
        elapsed_real = time.time() - self.real_start
        elapsed_sim = elapsed_real * SIM_SPEED
        return self.sim_anchor + timedelta(seconds=elapsed_sim)

    def demand_rides_this_tick(self, sim_now: datetime) -> int:
        base = RIDES_PER_TICK_WEEKEND_BASE if sim_now.weekday() >= 5 else RIDES_PER_TICK_WEEKDAY_BASE
        demand = compute_demand_multiplier(sim_now)
        count = max(0, int(round(base * demand + random.uniform(0, 1.2))))
        if sim_now.hour in {7, 8, 17, 18, 19}:
            count += random.randint(1, 3)
        return count

    def random_driver(self, pickup_zone: Dict) -> Optional[Dict]:
        if not self.available_driver_ids:
            return None
        candidates = [d for d in self.drivers if str(d["driver_id"]) in self.available_driver_ids]
        if not candidates:
            return None
        weighted = []
        weights = []
        for d in candidates:
            weighted.append(d)
            w = 1.0
            if d["home_zone_id"] == pickup_zone["zone_id"]:
                w = 2.8
            weights.append(w)
        return random.choices(weighted, weights=weights, k=1)[0]

    def insert_ride_event(self, ride_id: str, event_type: str, occurred_at: datetime, payload: Dict):
        doc = {
            "_id": str(uuid.uuid4()),
            "ride_id": ride_id,
            "ride_code": payload.get("ride_code"),
            "event_type": event_type,
            "event_time": occurred_at,
            "driver_id": payload.get("driver_id"),
            "passenger_id": payload.get("passenger_id"),
            "pickup_zone": payload.get("pickup_zone"),
            "dropoff_zone": payload.get("dropoff_zone"),
            "payload": payload,
            "_source_system": "mongodb",
        }
        self.mongo_ride_events.insert_one(doc)

    def interpolate_position(self, start_zone: Dict, end_zone: Dict, progress: float) -> Tuple[float, float]:
        lat1, lon1 = float(start_zone["latitude"]), float(start_zone["longitude"])
        lat2, lon2 = float(end_zone["latitude"]), float(end_zone["longitude"])
        lat = lat1 + (lat2 - lat1) * progress + random.uniform(-GPS_JITTER, GPS_JITTER)
        lon = lon1 + (lon2 - lon1) * progress + random.uniform(-GPS_JITTER, GPS_JITTER)
        return round(lat, 7), round(lon, 7)

    def emit_driver_location(
        self,
        driver_id: str,
        ride_id: str,
        ride_code: str,
        sim_now: datetime,
        start_zone: Dict,
        end_zone: Dict,
        status_context: str,
        progress: float,
    ):
        lat, lon = self.interpolate_position(start_zone, end_zone, progress)
        zone_id = end_zone["zone_id"] if progress >= 0.5 else start_zone["zone_id"]
        doc = {
            "_id": str(uuid.uuid4()),
            "driver_id": driver_id,
            "ride_id": ride_id,
            "ride_code": ride_code,
            "event_time": sim_now,
            "status_context": status_context,
            "zone_id": zone_id,
            "latitude": lat,
            "longitude": lon,
            "speed_kmh": round(random.uniform(12, 42), 1),
            "heading_deg": random.randint(0, 359),
            "gps_accuracy_m": round(random.uniform(3, 12), 1),
            "_source_system": "mongodb",
        }
        self.mongo_driver_locations.insert_one(doc)

    def emit_driver_locations_for_active_rides(self, sim_now: datetime):
        active_rows = self.fetchall(
            """
            SELECT ride_id, ride_code, driver_id, ride_status,
                   pickup_zone_id, dropoff_zone_id,
                   accepted_at, picked_up_at, requested_at, updated_at
            FROM rides
            WHERE ride_status IN ('ACCEPTED', 'PICKED_UP')
              AND driver_id IS NOT NULL
            """
        )

        for row in active_rows:
            driver_id = str(row["driver_id"])
            ride_id = str(row["ride_id"])
            ride_code = row["ride_code"]
            ride_status = row["ride_status"]

            last_emit = self.last_location_emit_at.get(driver_id)
            min_gap_seconds = max(10, int(60 / max(1, DRIVER_LOCATIONS_PER_MINUTE)))
            if last_emit and (sim_now - last_emit).total_seconds() < min_gap_seconds:
                continue

            pickup_zone = self.zone_map.get(int(row["pickup_zone_id"]))
            dropoff_zone = self.zone_map.get(int(row["dropoff_zone_id"]))
            if not pickup_zone or not dropoff_zone:
                continue

            if ride_status == RIDE_STATUS_ACCEPTED:
                start_at = row["accepted_at"] or row["requested_at"]
                elapsed = max(1, (sim_now - start_at).total_seconds())
                progress = min(1.0, elapsed / random.randint(240, 900))
                self.emit_driver_location(
                    driver_id=driver_id,
                    ride_id=ride_id,
                    ride_code=ride_code,
                    sim_now=sim_now,
                    start_zone=pickup_zone,
                    end_zone=pickup_zone,
                    status_context="PICKUP_EN_ROUTE",
                    progress=progress,
                )
            elif ride_status == RIDE_STATUS_PICKED_UP:
                start_at = row["picked_up_at"] or row["updated_at"]
                elapsed = max(1, (sim_now - start_at).total_seconds())
                progress = min(1.0, elapsed / random.randint(600, 2400))
                self.emit_driver_location(
                    driver_id=driver_id,
                    ride_id=ride_id,
                    ride_code=ride_code,
                    sim_now=sim_now,
                    start_zone=pickup_zone,
                    end_zone=dropoff_zone,
                    status_context="TRIP_IN_PROGRESS",
                    progress=progress,
                )

            self.last_location_emit_at[driver_id] = sim_now

    def create_ride(self, sim_now: datetime):
        if self.open_rides >= MAX_OPEN_RIDES:
            return

        pickup_zone, dropoff_zone = choose_zone_pair(self.zones, sim_now)
        passenger = random.choice(self.passengers)
        vehicle = random.choice(self.vehicle_types)

        ride_id = str(uuid.uuid4())
        while True:
            self.seq_num += 1
            ride_code = generate_ride_code(self.seq_num, sim_now)
            existing = self.fetchall("SELECT 1 AS ok FROM rides WHERE ride_code = %s LIMIT 1", (ride_code,))
            if not existing:
                break

        pickup_lat, pickup_lon = random_lat_lon(pickup_zone)
        dropoff_lat, dropoff_lon = random_lat_lon(dropoff_zone)
        distance_km = estimate_distance_km(pickup_zone, dropoff_zone)
        duration_minutes = estimate_duration_minutes(distance_km, sim_now)
        demand = compute_demand_multiplier(sim_now)
        surge = surge_multiplier(demand, pickup_zone["zone_code"], sim_now)
        base_fare = to_money(float(vehicle["base_fare"]))
        distance_fare = to_money(float(vehicle["per_km_rate"]) * distance_km)
        time_fare = to_money(float(vehicle["per_minute_rate"]) * duration_minutes)
        raw_total = (base_fare + distance_fare + time_fare) * surge
        promo_discount = to_money(random.choice([0, 0, 0, 2000, 3000, 5000]) if random.random() < 0.3 else 0)
        total_fare = max(Decimal("7000.00"), raw_total - promo_discount)

        driver = None
        no_driver_probability = 0.03 + (0.08 if demand > 1.8 else 0.0)
        if random.random() > no_driver_probability:
            driver = self.random_driver(pickup_zone)

        insert_query = """
            INSERT INTO rides (
                ride_id, ride_code, driver_id, passenger_id, vehicle_type_id,
                pickup_zone_id, dropoff_zone_id, pickup_address, dropoff_address,
                pickup_lat, pickup_lon, dropoff_lat, dropoff_lon,
                requested_at, distance_km, duration_minutes,
                base_fare, distance_fare, time_fare, surge_multiplier, promo_discount, total_fare,
                ride_status, created_at, updated_at, notes, _source_system
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
        """
        notes = random.choice([
            "pickup di lobby utama",
            "harap tunggu di gerbang",
            "bawa bagasi kecil",
            "penumpang prioritas kantor",
            ""
        ])
        self.execute(insert_query, (
            ride_id, ride_code, None, passenger["passenger_id"], vehicle["vehicle_type_id"],
            pickup_zone["zone_id"], dropoff_zone["zone_id"],
            random.choice(ADDRESSES[pickup_zone["zone_code"]]),
            random.choice(ADDRESSES[dropoff_zone["zone_code"]]),
            pickup_lat, pickup_lon, dropoff_lat, dropoff_lon,
            sim_now, distance_km, duration_minutes,
            base_fare, distance_fare, time_fare, surge, promo_discount, total_fare,
            RIDE_STATUS_REQUESTED, sim_now, sim_now, notes, "postgres"
        ))
        self.insert_ride_event(ride_id, EVENT_TYPES[RIDE_STATUS_REQUESTED], sim_now, {
            "ride_code": ride_code,
            "passenger_id": str(passenger["passenger_id"]),
            "pickup_zone": pickup_zone["zone_code"],
            "dropoff_zone": dropoff_zone["zone_code"],
            "estimated_distance_km": distance_km,
            "estimated_duration_minutes": duration_minutes,
            "surge_multiplier": float(surge),
            "total_fare": float(total_fare),
        })

        if not driver:
            no_driver_at = sim_now + timedelta(seconds=random.randint(30, 180))
            self.pending.append(ScheduledTransition(
                ride_id=ride_id,
                ride_code=ride_code,
                next_status=RIDE_STATUS_NO_DRIVER,
                due_at=no_driver_at,
                meta={
                    "requested_at": sim_now,
                    "passenger_id": str(passenger["passenger_id"]),
                    "pickup_zone": pickup_zone,
                    "dropoff_zone": dropoff_zone,
                }
            ))
        else:
            accept_delay_sec = random.randint(20, 180)
            accepted_at = sim_now + timedelta(seconds=accept_delay_sec)
            self.pending.append(ScheduledTransition(
                ride_id=ride_id,
                ride_code=ride_code,
                next_status=RIDE_STATUS_ACCEPTED,
                due_at=accepted_at,
                driver_id=str(driver["driver_id"]),
                meta={
                    "pickup_zone": pickup_zone,
                    "dropoff_zone": dropoff_zone,
                    "requested_at": sim_now,
                    "distance_km": distance_km,
                    "duration_minutes": duration_minutes,
                    "total_fare": float(total_fare),
                    "surge_multiplier": float(surge),
                    "passenger_id": str(passenger["passenger_id"]),
                }
            ))
            self.available_driver_ids.discard(str(driver["driver_id"]))

        self.open_rides += 1
        self.conn.commit()

    def process_transition(self, tr: ScheduledTransition):
        current = self.fetchall(
            "SELECT ride_status, accepted_at, picked_up_at, driver_id, passenger_id FROM rides WHERE ride_id = %s",
            (tr.ride_id,)
        )
        if not current:
            self.conn.rollback()
            return
        row = current[0]
        current_status = row["ride_status"]
        current_driver_id = str(row["driver_id"]) if row["driver_id"] else None
        passenger_id = str(row["passenger_id"]) if row["passenger_id"] else tr.meta.get("passenger_id")

        if current_status in TERMINAL_STATUSES:
            self.conn.rollback()
            if current_driver_id:
                self.available_driver_ids.add(current_driver_id)
            self.open_rides = max(0, self.open_rides - 1)
            return

        if tr.next_status == RIDE_STATUS_ACCEPTED:
            if current_status != RIDE_STATUS_REQUESTED:
                self.conn.rollback()
                return
            self.execute(
                """
                UPDATE rides
                SET driver_id = %s,
                    ride_status = 'ACCEPTED',
                    accepted_at = %s,
                    updated_at = %s
                WHERE ride_id = %s
                """,
                (tr.driver_id, tr.due_at, tr.due_at, tr.ride_id),
            )
            self.insert_ride_event(tr.ride_id, EVENT_TYPES[RIDE_STATUS_ACCEPTED], tr.due_at, {
                "ride_code": tr.ride_code,
                "driver_id": tr.driver_id,
                "passenger_id": passenger_id,
                "pickup_zone": tr.meta["pickup_zone"]["zone_code"],
                "dropoff_zone": tr.meta["dropoff_zone"]["zone_code"],
                "accepted_after_seconds": int((tr.due_at - tr.meta["requested_at"]).total_seconds()),
                "total_fare": tr.meta["total_fare"],
                "surge_multiplier": tr.meta["surge_multiplier"],
            })

            cancel_after_accept_probability = 0.05
            if random.random() < cancel_after_accept_probability:
                cancelled_at = tr.due_at + timedelta(seconds=random.randint(30, 240))
                self.pending.append(ScheduledTransition(
                    ride_id=tr.ride_id,
                    ride_code=tr.ride_code,
                    next_status=RIDE_STATUS_CANCELLED,
                    due_at=cancelled_at,
                    driver_id=tr.driver_id,
                    cancellation_reason=random.choice([
                        "PASSENGER_CANCELLED",
                        "DRIVER_DELAYED",
                        "WRONG_PICKUP_POINT",
                        "PASSENGER_NO_SHOW"
                    ]),
                    meta={
                        **tr.meta,
                        "accepted_at": tr.due_at,
                        "passenger_id": passenger_id,
                    }
                ))
            else:
                pickup_delay_sec = random.randint(120, 900)
                picked_up_at = tr.due_at + timedelta(seconds=pickup_delay_sec)
                self.pending.append(ScheduledTransition(
                    ride_id=tr.ride_id,
                    ride_code=tr.ride_code,
                    next_status=RIDE_STATUS_PICKED_UP,
                    due_at=picked_up_at,
                    driver_id=tr.driver_id,
                    meta={
                        **tr.meta,
                        "accepted_at": tr.due_at,
                        "passenger_id": passenger_id,
                    }
                ))

        elif tr.next_status == RIDE_STATUS_PICKED_UP:
            if current_status != RIDE_STATUS_ACCEPTED:
                self.conn.rollback()
                return
            self.execute(
                """
                UPDATE rides
                SET ride_status = 'PICKED_UP',
                    picked_up_at = %s,
                    updated_at = %s
                WHERE ride_id = %s
                """,
                (tr.due_at, tr.due_at, tr.ride_id),
            )
            self.insert_ride_event(tr.ride_id, EVENT_TYPES[RIDE_STATUS_PICKED_UP], tr.due_at, {
                "ride_code": tr.ride_code,
                "driver_id": tr.driver_id,
                "passenger_id": passenger_id,
                "pickup_zone": tr.meta["pickup_zone"]["zone_code"],
                "dropoff_zone": tr.meta["dropoff_zone"]["zone_code"],
                "pickup_after_seconds": int((tr.due_at - tr.meta["accepted_at"]).total_seconds()),
            })
            completion_minutes = max(5, int(round(tr.meta["duration_minutes"] * random.uniform(0.85, 1.25))))
            completed_at = tr.due_at + timedelta(minutes=completion_minutes)
            self.pending.append(ScheduledTransition(
                ride_id=tr.ride_id,
                ride_code=tr.ride_code,
                next_status=RIDE_STATUS_COMPLETED,
                due_at=completed_at,
                driver_id=tr.driver_id,
                meta={
                    **tr.meta,
                    "picked_up_at": tr.due_at,
                    "actual_duration_minutes": completion_minutes,
                    "passenger_id": passenger_id,
                }
            ))

        elif tr.next_status == RIDE_STATUS_COMPLETED:
            if current_status != RIDE_STATUS_PICKED_UP:
                self.conn.rollback()
                return
            passenger_rating = round(random.uniform(4.0, 5.0), 1)
            driver_rating = round(random.uniform(4.0, 5.0), 1)
            self.execute(
                """
                UPDATE rides
                SET ride_status = 'COMPLETED',
                    completed_at = %s,
                    passenger_rating = %s,
                    driver_rating = %s,
                    updated_at = %s
                WHERE ride_id = %s
                """,
                (tr.due_at, passenger_rating, driver_rating, tr.due_at, tr.ride_id),
            )
            self.execute("UPDATE drivers SET total_trips = total_trips + 1 WHERE driver_id = %s", (tr.driver_id,))
            self.execute("UPDATE passengers SET total_trips = total_trips + 1 WHERE passenger_id = (SELECT passenger_id FROM rides WHERE ride_id = %s)", (tr.ride_id,))
            self.insert_ride_event(tr.ride_id, EVENT_TYPES[RIDE_STATUS_COMPLETED], tr.due_at, {
                "ride_code": tr.ride_code,
                "driver_id": tr.driver_id,
                "passenger_id": passenger_id,
                "pickup_zone": tr.meta["pickup_zone"]["zone_code"],
                "dropoff_zone": tr.meta["dropoff_zone"]["zone_code"],
                "completed_after_seconds": int((tr.due_at - tr.meta["picked_up_at"]).total_seconds()),
                "actual_duration_minutes": tr.meta["actual_duration_minutes"],
                "passenger_rating": passenger_rating,
                "driver_rating": driver_rating,
                "total_fare": tr.meta["total_fare"],
            })
            self.available_driver_ids.add(tr.driver_id)
            self.open_rides = max(0, self.open_rides - 1)

        elif tr.next_status == RIDE_STATUS_CANCELLED:
            if current_status != RIDE_STATUS_ACCEPTED:
                self.conn.rollback()
                return
            self.execute(
                """
                UPDATE rides
                SET ride_status = 'CANCELLED',
                    cancelled_at = %s,
                    cancellation_reason = %s,
                    updated_at = %s
                WHERE ride_id = %s
                """,
                (tr.due_at, tr.cancellation_reason, tr.due_at, tr.ride_id),
            )
            self.insert_ride_event(tr.ride_id, EVENT_TYPES[RIDE_STATUS_CANCELLED], tr.due_at, {
                "ride_code": tr.ride_code,
                "driver_id": tr.driver_id,
                "passenger_id": passenger_id,
                "pickup_zone": tr.meta["pickup_zone"]["zone_code"],
                "dropoff_zone": tr.meta["dropoff_zone"]["zone_code"],
                "cancellation_reason": tr.cancellation_reason,
            })
            if tr.driver_id:
                self.available_driver_ids.add(tr.driver_id)
            self.open_rides = max(0, self.open_rides - 1)

        elif tr.next_status == RIDE_STATUS_NO_DRIVER:
            if current_status != RIDE_STATUS_REQUESTED:
                self.conn.rollback()
                return
            self.execute(
                """
                UPDATE rides
                SET ride_status = 'NO_DRIVER',
                    cancelled_at = %s,
                    cancellation_reason = 'NO_DRIVER_AVAILABLE',
                    updated_at = %s
                WHERE ride_id = %s
                """,
                (tr.due_at, tr.due_at, tr.ride_id),
            )
            self.insert_ride_event(tr.ride_id, EVENT_TYPES[RIDE_STATUS_NO_DRIVER], tr.due_at, {
                "ride_code": tr.ride_code,
                "passenger_id": passenger_id,
                "pickup_zone": tr.meta["pickup_zone"]["zone_code"] if tr.meta.get("pickup_zone") else None,
                "dropoff_zone": tr.meta["dropoff_zone"]["zone_code"] if tr.meta.get("dropoff_zone") else None,
                "reason": "NO_DRIVER_AVAILABLE",
            })
            self.open_rides = max(0, self.open_rides - 1)

        self.conn.commit()

    def process_due_transitions(self, sim_now: datetime):
        due = [p for p in self.pending if p.due_at <= sim_now]
        self.pending = [p for p in self.pending if p.due_at > sim_now]
        for tr in sorted(due, key=lambda x: x.due_at):
            self.process_transition(tr)

    def rebalance_driver_status(self, sim_now: datetime):
        if sim_now.minute % 30 != 0:
            return
        if random.random() < 0.08 and self.drivers:
            chosen = random.sample(self.drivers, k=min(3, len(self.drivers)))
            for driver in chosen:
                if str(driver["driver_id"]) in self.available_driver_ids and random.random() < 0.5:
                    self.execute(
                        "UPDATE drivers SET status = 'ACTIVE', updated_at = %s WHERE driver_id = %s",
                        (sim_now, driver["driver_id"]),
                    )
            self.conn.commit()

    def log_metrics(self, sim_now: datetime):
        print(
            f"[{now_iso(sim_now)}] open_rides={self.open_rides} pending={len(self.pending)} "
            f"available_drivers={len(self.available_driver_ids)} seq_num={self.seq_num}"
        )

    def run(self):
        self.connect()
        self.connect_mongo()
        self.ensure_entities()
        self.restore_runtime_state()
        last_metric_log = None
        while True:
            sim_now = self.simulated_now()
            self.process_due_transitions(sim_now)

            rides_to_create = self.demand_rides_this_tick(sim_now)
            for _ in range(rides_to_create):
                self.create_ride(sim_now)

            self.emit_driver_locations_for_active_rides(sim_now)
            self.rebalance_driver_status(sim_now)

            if last_metric_log is None or (sim_now - last_metric_log) >= timedelta(minutes=5):
                self.log_metrics(sim_now)
                last_metric_log = sim_now

            time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    TaxiGenerator().run()
