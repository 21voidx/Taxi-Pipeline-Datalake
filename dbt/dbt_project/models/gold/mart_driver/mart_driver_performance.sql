{{
  config(
    materialized = 'table',
    cluster_by   = ['experience_tier', 'vehicle_type_code'],
    tags         = ['gold', 'driver', 'performance']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  mart_driver_performance
--  Purpose : All-time driver scorecards for leaderboard & payouts.
--  Grain   : driver_id (one row per driver, cumulative metrics)
--  Used by : Driver management dashboard, incentive calculation.
-- ══════════════════════════════════════════════════════════════

with rides as (

    select * from {{ ref('fct_rides') }}

),

driver_dim as (

    select driver_id, driver_name, vehicle_type_code, vehicle_type_name,
           vehicle_category, experience_tier, driver_status,
           home_zone_code, home_zone_name, home_city, home_province,
           rating, joined_at
    from {{ ref('dim_drivers') }}
    where is_current = true
),

driver_stats as (

    select
        driver_id,

        count(*)                                                    as total_requests,
        countif(ride_status = 'COMPLETED')                         as completed_trips,
        countif(ride_status = 'CANCELLED')                         as cancelled_trips,
        countif(ride_status = 'NO_DRIVER')                         as no_driver_trips,

        -- ── Completion rate ───────────────────────────────────────
        round(
            safe_divide(
                countif(ride_status = 'COMPLETED'),
                nullif(count(*), 0)
            ) * 100, 2
        )                                                           as completion_rate_pct,

        -- ── Revenue metrics ───────────────────────────────────────
        round(sum(case when ride_status = 'COMPLETED' then total_fare else 0 end), 0)
                                                                    as lifetime_gmv_idr,
        round(sum(case when ride_status = 'COMPLETED' then total_fare * 0.80 else 0 end), 0)
                                                                    as lifetime_earnings_idr,
        round(avg(case when ride_status = 'COMPLETED' then total_fare end), 0)
                                                                    as avg_fare_per_trip,

        -- ── Distance & time ───────────────────────────────────────
        round(sum(case when ride_status = 'COMPLETED' then distance_km else 0 end), 2)
                                                                    as total_distance_km,
        round(avg(case when ride_status = 'COMPLETED' then distance_km end), 2)
                                                                    as avg_trip_distance_km,
        round(sum(case when ride_status = 'COMPLETED' then duration_minutes else 0 end), 0)
                                                                    as total_online_minutes,

        -- ── Acceptance speed ──────────────────────────────────────
        round(avg(
            case when acceptance_duration_secs is not null
                 then acceptance_duration_secs / 60.0 end
        ), 2)                                                       as avg_acceptance_min,

        -- ── Rating ────────────────────────────────────────────────
        round(avg(case when ride_status = 'COMPLETED' then passenger_rating end), 2)
                                                                    as avg_passenger_rating,

        -- ── Surge & promo ─────────────────────────────────────────
        countif(is_surge = true and ride_status = 'COMPLETED')     as surge_trips_completed,
        round(avg(case when is_surge = true and ride_status = 'COMPLETED'
                       then surge_multiplier end), 2)               as avg_surge_multiplier,

        -- ── Activity window ───────────────────────────────────────
        min(requested_date)                                         as first_trip_date,
        max(requested_date)                                         as last_trip_date,
        date_diff(
            max(requested_date), min(requested_date), day
        )                                                           as active_days_span

    from rides
    where driver_id is not null
    group by 1

)

select
    -- ── Driver identity (from dim) ────────────────────────────
    d.driver_id,
    d.driver_name,
    d.vehicle_type_code,
    d.vehicle_type_name,
    d.vehicle_category,
    d.experience_tier,
    d.driver_status,
    d.home_zone_code,
    d.home_zone_name,
    d.home_city,
    d.rating                                                        as current_rating,
    d.joined_at,

    -- ── Computed stats ────────────────────────────────────────
    s.total_requests,
    s.completed_trips,
    s.cancelled_trips,
    s.completion_rate_pct,
    s.lifetime_gmv_idr,
    s.lifetime_earnings_idr,
    s.avg_fare_per_trip,
    s.total_distance_km,
    s.avg_trip_distance_km,
    s.total_online_minutes,
    s.avg_acceptance_min,
    s.avg_passenger_rating,
    s.surge_trips_completed,
    s.avg_surge_multiplier,
    s.first_trip_date,
    s.last_trip_date,
    s.active_days_span,

    -- ── Performance score (composite) ─────────────────────────
    round(
        (coalesce(s.completion_rate_pct, 0) * 0.4)
      + (coalesce(s.avg_passenger_rating, 0) * 10 * 0.4)
      + (least(coalesce(s.completed_trips, 0) / 10.0, 20.0) * 0.2)
    , 2)                                                            as performance_score,

    current_timestamp()                                             as refreshed_at

from driver_dim d
left join driver_stats s on d.driver_id = s.driver_id
