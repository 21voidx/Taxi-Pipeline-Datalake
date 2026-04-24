-- ══════════════════════════════════════════════════════════════
--  scd2_drivers
--
--  Tracks historical changes to driver attributes over time,
--  most importantly: status, rating, vehicle assignments, and zone.
--
--  dbt adds these columns automatically:
--    dbt_scd_id      → unique surrogate key per version row
--    dbt_updated_at  → when dbt processed this version
--    dbt_valid_from  → timestamp this version became active
--    dbt_valid_to    → timestamp this version was superseded (NULL = current)
--
--  Common analytical queries:
--    - How many drivers transitioned from ACTIVE → SUSPENDED in Q1?
--    - What was driver X's status on a specific date?
--    - Which drivers changed vehicle type more than once?
-- ══════════════════════════════════════════════════════════════
{% snapshot snapshot_dim_driver %}

{{
    config(
        unique_key    = 'driver_id',
        strategy      = 'timestamp',
        updated_at    = 'updated_at',
        invalidate_hard_deletes = True
    )
}}

select
    driver_id,
    driver_code,
    full_name,
    phone_number,
    email,
    nik,
    sim_number,
    license_plate,
    vehicle_type_id,
    vehicle_brand,
    vehicle_model,
    vehicle_year,
    vehicle_color,
    home_zone_id,
    rating,
    total_trips,
    status,
    joined_at,
    updated_at,
    _source_system

from {{ source('bronze_pg', 'drivers') }}

{% endsnapshot %}