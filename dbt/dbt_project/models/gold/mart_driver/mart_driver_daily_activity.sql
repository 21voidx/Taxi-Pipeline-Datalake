{{
  config(
    materialized = 'table',
    partition_by = {
      'field': 'ride_date',
      'data_type': 'date',
      'granularity': 'day'
    },
    cluster_by   = ['vehicle_type_code'],
    tags         = ['gold', 'driver', 'daily']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  mart_driver_daily_activity
--  Purpose : Per-driver daily activity rollup for payroll & ops.
--  Grain   : driver_id × ride_date
-- ══════════════════════════════════════════════════════════════

with rides as (

    select * from {{ ref('fct_rides') }}
    where driver_id is not null

)

select
    driver_id,
    driver_name,
    vehicle_type_code,
    vehicle_category,
    driver_tier,
    requested_date                                                  as ride_date,

    -- ── Daily volume ──────────────────────────────────────────
    count(*)                                                        as daily_requests,
    countif(ride_status = 'COMPLETED')                             as daily_completed,
    countif(ride_status = 'CANCELLED')                             as daily_cancelled,

    -- ── Daily earnings ────────────────────────────────────────
    round(sum(case when ride_status = 'COMPLETED' then total_fare * 0.80 else 0 end), 0)
                                                                    as daily_earnings_idr,
    round(sum(case when ride_status = 'COMPLETED' then total_fare else 0 end), 0)
                                                                    as daily_gmv_idr,
    round(avg(case when ride_status = 'COMPLETED' then total_fare end), 0)
                                                                    as avg_fare_idr,

    -- ── Daily distance ────────────────────────────────────────
    round(sum(case when ride_status = 'COMPLETED' then distance_km else 0 end), 2)
                                                                    as daily_km,
    round(sum(case when ride_status = 'COMPLETED' then duration_minutes else 0 end))
                                                                    as daily_trip_minutes,

    -- ── Surge trips ───────────────────────────────────────────
    countif(is_surge = true and ride_status = 'COMPLETED')         as surge_trips,
    round(sum(case when is_surge and ride_status = 'COMPLETED'
                   then total_fare - (total_fare / surge_multiplier) else 0 end), 0)
                                                                    as surge_bonus_idr,

    -- ── Rating ────────────────────────────────────────────────
    round(avg(case when ride_status = 'COMPLETED' then passenger_rating end), 2)
                                                                    as daily_avg_rating,

    -- ── Zone of primary activity (pickup majority) ────────────
    approx_top_count(pickup_zone_code, 1)[offset(0)].value         as primary_zone_code,

    current_timestamp()                                             as refreshed_at

from rides
group by 1, 2, 3, 4, 5, 6
