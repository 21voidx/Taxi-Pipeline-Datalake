{{
  config(
    materialized = 'table',
    partition_by = {
      'field': 'ride_date',
      'data_type': 'date',
      'granularity': 'day'
    },
    cluster_by   = ['vehicle_type_code', 'pickup_city'],
    tags         = ['gold', 'operations', 'daily']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  mart_daily_ride_summary
--  Purpose : Daily operational KPIs per vehicle type and city.
--  Used by : Operations dashboard — ride volume, completion rate,
--            avg fare, surge rate, cancellation breakdown.
-- ══════════════════════════════════════════════════════════════

with rides as (

    select * from {{ ref('fct_rides') }}

),

daily as (

    select
        -- ── Dimensions ────────────────────────────────────────────
        requested_date                                              as ride_date,
        vehicle_type_code,
        vehicle_type_name,
        vehicle_category,
        pickup_city,
        pickup_province,
        pickup_area_type,
        time_bucket,

        -- ── Volume KPIs ───────────────────────────────────────────
        count(*)                                                    as total_requests,
        countif(ride_status = 'COMPLETED')                         as completed_rides,
        countif(ride_status = 'CANCELLED')                         as cancelled_rides,
        countif(ride_status = 'NO_DRIVER')                         as no_driver_rides,
        countif(ride_status in ('ACCEPTED','PICKED_UP'))           as in_progress_rides,

        -- ── Rate KPIs ─────────────────────────────────────────────
        round(
            safe_divide(
                countif(ride_status = 'COMPLETED'),
                nullif(count(*), 0)
            ) * 100, 2
        )                                                           as completion_rate_pct,

        round(
            safe_divide(
                countif(ride_status = 'CANCELLED'),
                nullif(count(*), 0)
            ) * 100, 2
        )                                                           as cancellation_rate_pct,

        round(
            safe_divide(
                countif(is_surge = true),
                nullif(count(*), 0)
            ) * 100, 2
        )                                                           as surge_rate_pct,

        -- ── Fare KPIs (COMPLETED only) ────────────────────────────
        round(sum(case when ride_status = 'COMPLETED' then total_fare end), 0)
                                                                    as gross_revenue_idr,
        round(avg(case when ride_status = 'COMPLETED' then total_fare end), 0)
                                                                    as avg_fare_idr,
        round(max(case when ride_status = 'COMPLETED' then total_fare end), 0)
                                                                    as max_fare_idr,
        round(sum(case when ride_status = 'COMPLETED' then promo_discount end), 0)
                                                                    as total_promo_idr,

        -- ── Distance KPIs ─────────────────────────────────────────
        round(avg(case when ride_status = 'COMPLETED' then distance_km end), 2)
                                                                    as avg_distance_km,
        round(sum(case when ride_status = 'COMPLETED' then distance_km end), 2)
                                                                    as total_distance_km,

        -- ── Duration KPIs ─────────────────────────────────────────
        round(avg(case when ride_status = 'COMPLETED' then duration_minutes end), 1)
                                                                    as avg_trip_minutes,
        round(avg(case when accepted_at is not null
                       then acceptance_duration_secs / 60.0 end), 1)
                                                                    as avg_acceptance_minutes,

        -- ── Rating KPIs ───────────────────────────────────────────
        round(avg(case when ride_status = 'COMPLETED' then driver_rating end), 2)
                                                                    as avg_driver_rating,
        round(avg(case when ride_status = 'COMPLETED' then passenger_rating end), 2)
                                                                    as avg_passenger_rating,

        -- ── Promo adoption ────────────────────────────────────────
        countif(has_promo = true and ride_status = 'COMPLETED')    as promo_rides_count,

        current_timestamp()                                         as refreshed_at

    from rides
    group by 1, 2, 3, 4, 5, 6, 7, 8

)

select * from daily
