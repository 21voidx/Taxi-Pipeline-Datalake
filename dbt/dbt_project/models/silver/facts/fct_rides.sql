{{
  config(
    materialized        = 'incremental',
    incremental_strategy= 'merge',
    unique_key          = 'ride_id',
    partition_by        = {
      'field': 'requested_date',
      'data_type': 'date',
      'granularity': 'day'
    },
    cluster_by          = ['ride_status', 'vehicle_type_id', 'pickup_zone_id'],
    tags                = ['silver', 'fact', 'rides']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  fct_rides
--  Purpose : Central fact table — one row per ride.
--            Bronze source → cleaning/enrichment (staging inlined)
--            → dimension joins.
--  Grain   : ride_id
--  Incremental: merge strategy, lookback {{ var('incremental_lookback_days') }} days
-- ══════════════════════════════════════════════════════════════

with source as (

    select * from {{ source('bronze_pg', 'rides') }}

    {% if is_incremental() %}
    where updated_at >= timestamp_sub(
        current_timestamp(),
        interval {{ var('incremental_lookback_days') }} day
    )
    {% endif %}

),

-- ── Inline staging: clean + enrich raw ride records ───────────
enriched as (

    select
        -- ── Identity ──────────────────────────────────────────────
        ride_id,
        ride_code,
        driver_id,
        passenger_id,
        vehicle_type_id,

        -- ── Zones ─────────────────────────────────────────────────
        pickup_zone_id,
        dropoff_zone_id,
        pickup_address,
        dropoff_address,
        pickup_lat,
        pickup_lon,
        dropoff_lat,
        dropoff_lon,

        -- ── Timestamps ────────────────────────────────────────────
        requested_at,
        date(requested_at)                                          as requested_date,
        accepted_at,
        picked_up_at,
        completed_at,
        cancelled_at,

        -- ── Derived timing durations ──────────────────────────────
        case
            when accepted_at is not null
            then timestamp_diff(accepted_at, requested_at, second)
        end                                                         as acceptance_duration_secs,

        case
            when picked_up_at is not null and accepted_at is not null
            then timestamp_diff(picked_up_at, accepted_at, second)
        end                                                         as pickup_wait_secs,

        case
            when completed_at is not null and picked_up_at is not null
            then timestamp_diff(completed_at, picked_up_at, second)
        end                                                         as trip_duration_secs,

        -- ── Metrics ───────────────────────────────────────────────
        coalesce(distance_km, 0)                                    as distance_km,
        coalesce(duration_minutes, 0)                               as duration_minutes,

        -- ── Fare components ───────────────────────────────────────
        coalesce(base_fare, 0)                                      as base_fare,
        coalesce(distance_fare, 0)                                  as distance_fare,
        coalesce(time_fare, 0)                                      as time_fare,
        coalesce(surge_multiplier, 1.0)                             as surge_multiplier,
        coalesce(promo_discount, 0)                                 as promo_discount,
        coalesce(total_fare, 0)                                     as total_fare,

        -- ── Derived fare flags ────────────────────────────────────
        case when coalesce(surge_multiplier, 1.0) > 1.0 then true else false end
                                                                    as is_surge,
        case when coalesce(promo_discount, 0) > 0        then true else false end
                                                                    as has_promo,
        case
            when extract(hour from requested_at) between 7  and 9  then 'morning_peak'
            when extract(hour from requested_at) between 17 and 19 then 'evening_peak'
            when extract(hour from requested_at) between 22 and 23
              or extract(hour from requested_at) between 0  and 4  then 'late_night'
            else 'off_peak'
        end                                                         as time_bucket,

        -- ── Status ────────────────────────────────────────────────
        upper(ride_status)                                          as ride_status,
        cancellation_reason,

        -- ── Ratings ───────────────────────────────────────────────
        passenger_rating,
        driver_rating,
        notes,
        created_at,
        updated_at

    from source

),

-- ── Dimension lookups ─────────────────────────────────────────
drivers as (
    select driver_key,driver_id, driver_name, vehicle_type_code, vehicle_type_name,
           experience_tier, home_zone_code, driver_status
    from {{ ref('dim_drivers') }}
),

passengers as (
    select passenger_key, passenger_id, passenger_name, loyalty_tier, age_bucket,
           gender, home_zone_code as pax_home_zone_code
    from {{ ref('dim_passengers') }}
),

pickup_zones as (
    select 
        zone_key,
        zone_id,
        zone_code  as pickup_zone_code,
        zone_name  as pickup_zone_name,
        city       as pickup_city,
        province   as pickup_province,
        area_type  as pickup_area_type
    from {{ ref('dim_zones') }}
),

dropoff_zones as (
    select 
        zone_key,
        zone_id,
        zone_code  as dropoff_zone_code,
        zone_name  as dropoff_zone_name,
        city       as dropoff_city,
        province   as dropoff_province,
        area_type  as dropoff_area_type
    from {{ ref('dim_zones') }}
),

vehicle_types as (
    select vehicle_type_key, vehicle_type_id, type_code, type_name, vehicle_category, fare_tier, capacity
    from {{ ref('dim_vehicle_types') }}
),

final as (

    select
        -- ── Surrogate key ──────────────────────────────────────────
        {{ surrogate_key(['r.ride_id']) }}                          as ride_key,
        d.driver_key,
        p.passenger_key,
        pz.zone_key,
        vt.vehicle_type_key,


        -- ── Natural key & code ────────────────────────────────────
        r.ride_id,
        r.ride_code,

        -- ── FK to dimensions ──────────────────────────────────────
        r.driver_id,
        r.passenger_id,
        r.vehicle_type_id,
        r.pickup_zone_id,
        r.dropoff_zone_id,
        r.requested_date,                         

        -- ── Status & classification ───────────────────────────────
        r.ride_status,
        r.time_bucket,
        r.is_surge,
        r.has_promo,

        -- ── Driver attributes (denormalised) ──────────────────────
        d.driver_name,
        d.vehicle_type_code,
        d.vehicle_type_name,
        d.experience_tier                                   as driver_tier,

        -- ── Passenger attributes ──────────────────────────────────
        p.passenger_name,
        p.loyalty_tier                                      as passenger_tier,
        p.age_bucket                                        as passenger_age_bucket,
        p.gender                                            as passenger_gender,

        -- ── Pickup zone attributes ────────────────────────────────
        pz.pickup_zone_code,
        pz.pickup_zone_name,
        pz.pickup_city,
        pz.pickup_province,
        pz.pickup_area_type,

        -- ── Dropoff zone attributes ───────────────────────────────
        dz.dropoff_zone_code,
        dz.dropoff_zone_name,
        dz.dropoff_city,
        dz.dropoff_province,
        dz.dropoff_area_type,

        -- ── Same-zone flag ────────────────────────────────────────
        case when r.pickup_zone_id = r.dropoff_zone_id then true else false end
                                                            as is_same_zone_trip,

        -- ── Vehicle attributes ────────────────────────────────────
        vt.vehicle_category,
        vt.fare_tier,
        vt.capacity                                         as vehicle_capacity,

        -- ── Measures: timing ──────────────────────────────────────
        r.requested_at,
        r.accepted_at,
        r.picked_up_at,
        r.completed_at,
        r.cancelled_at,
        r.acceptance_duration_secs,
        r.pickup_wait_secs,
        r.trip_duration_secs,
        r.distance_km,
        r.duration_minutes,

        -- ── Measures: fare ────────────────────────────────────────
        r.base_fare,
        r.distance_fare,
        r.time_fare,
        r.surge_multiplier,
        r.promo_discount,
        r.total_fare,

        -- ── Measures: ratings ─────────────────────────────────────
        r.passenger_rating,
        r.driver_rating,

        -- ── Cancellation ──────────────────────────────────────────
        r.cancellation_reason,

        -- ── Metadata ──────────────────────────────────────────────
        r.updated_at

    from enriched r
    left join drivers d         on r.driver_id      = d.driver_id
    left join passengers p      on r.passenger_id   = p.passenger_id
    left join pickup_zones pz   on r.pickup_zone_id = pz.zone_id
    left join dropoff_zones dz  on r.dropoff_zone_id = dz.zone_id
    left join vehicle_types vt  on r.vehicle_type_id = vt.vehicle_type_id

)

select * from final
