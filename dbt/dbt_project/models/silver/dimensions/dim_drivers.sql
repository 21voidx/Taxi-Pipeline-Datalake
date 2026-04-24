{{
  config(
    materialized = 'incremental',
    unique_key = 'driver_key',
    tags         = ['silver', 'dimension', 'drivers']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  dim_drivers
--  Purpose : Clean & standardise driver data, then enrich with
--            vehicle type and home zone (staging inlined).
--  Cleaning :
--    - phone_number  → strip +62, dashes → canonical 08XXXXXXXXX
--    - nik           → remove spaces, pad/truncate to 16 digits
--    - license_plate → UPPER() + trim
--    - rating        → cap between 1.0 – 5.0
-- ══════════════════════════════════════════════════════════════


WITH snapshot_source AS (
    SELECT
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
        dbt_valid_from                          AS valid_from,
        dbt_valid_to                            AS valid_to,
        CASE WHEN dbt_valid_to IS NULL THEN TRUE ELSE FALSE END AS is_current
    FROM {{ ref('snapshot_dim_driver') }}
    where dbt_valid_to is null
),

cleaned as (

    select
        driver_id,
        driver_code,
        full_name,
        {{ clean_phone_id('phone_number') }}                        as phone_number_clean,
        phone_number                                                as phone_number_raw,
        email,
        {{ clean_nik('nik') }}                                      as nik_clean,
        nik                                                         as nik_raw,
        sim_number,
        upper(trim(regexp_replace(license_plate, r'\s+', ' ')))    as license_plate_clean,
        license_plate                                               as license_plate_raw,
        vehicle_type_id,
        vehicle_brand,
        vehicle_model,
        vehicle_year,
        vehicle_color,
        home_zone_id,
        greatest(1.0, least(5.0, coalesce(rating, 5.0)))           as rating,
        coalesce(total_trips, 0)                                    as total_trips,
        upper(status)                                               as driver_status,
        joined_at,
        updated_at,
        case
            when regexp_contains(phone_number, r'^\+62')      then true
            when regexp_contains(phone_number, r'-')           then true
            else false
        end                                                         as phone_was_dirty,
        case
            when length(regexp_replace(nik, r'\s', '')) != 16  then true
            else false
        end                                                         as nik_was_dirty,
        case
            when lower(license_plate) = license_plate             then true
            else false
        end                                                         as plate_was_dirty,
        valid_from,
        valid_to,
        is_current

    from snapshot_source

),

vehicle_types as (

    select * from {{ ref('dim_vehicle_types') }}

),

zones as (

    select * from {{ ref('dim_zones') }}

),

joined as (

    select
        -- ── Surrogate key ──────────────────────────────────────────
        {{ surrogate_key(['d.driver_id', 'd.valid_from']) }}                        as driver_key,

        -- ── Driver core ───────────────────────────────────────────
        d.driver_id,
        d.driver_code,
        d.full_name                                     as driver_name,
        d.phone_number_clean                            as phone_number,
        d.email,
        d.nik_clean                                     as nik,
        d.sim_number,
        d.license_plate_clean                           as license_plate,
        d.driver_status,
        d.rating,
        d.total_trips,
        d.joined_at,
        d.updated_at,
        d.phone_was_dirty,
        d.nik_was_dirty,
        d.plate_was_dirty,

        -- ── Vehicle info (denormalised) ───────────────────────────
        vt.vehicle_type_id,
        vt.type_code                                    as vehicle_type_code,
        vt.type_name                                    as vehicle_type_name,
        vt.capacity                                     as vehicle_capacity,
        vt.vehicle_category,
        d.vehicle_brand,
        d.vehicle_model,
        d.vehicle_year,
        d.vehicle_color,

        -- ── Driver experience tier ────────────────────────────────
        case
            when d.total_trips >= 1000 then 'Platinum'
            when d.total_trips >= 500  then 'Gold'
            when d.total_trips >= 100  then 'Silver'
            else 'Bronze'
        end                                             as experience_tier,

        -- ── Home zone ─────────────────────────────────────────────
        z.zone_code                                     as home_zone_code,
        z.zone_name                                     as home_zone_name,
        z.city                                          as home_city,
        z.province                                      as home_province,
        z.area_type                                     as home_area_type,
        d.valid_from,
        d.valid_to,
        d.is_current

    from cleaned d
    left join vehicle_types vt
           on d.vehicle_type_id = vt.vehicle_type_id
    left join zones z
           on d.home_zone_id = z.zone_id

)

select * from joined

