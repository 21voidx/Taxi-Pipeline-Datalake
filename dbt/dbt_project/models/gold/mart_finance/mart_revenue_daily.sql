{{
  config(
    materialized = 'table',
    partition_by = {
      'field': 'ride_date',
      'data_type': 'date',
      'granularity': 'day'
    },
    cluster_by   = ['vehicle_category', 'fare_tier'],
    tags         = ['gold', 'finance', 'revenue']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  mart_revenue_daily
--  Purpose : Daily P&L view — GMV, platform fee (20%), driver
--            earnings (80%), promo burn, surge uplift.
--  Used by : Finance dashboard, CFO reporting.
-- ══════════════════════════════════════════════════════════════

with rides as (

    select * from {{ ref('fct_rides') }}
    where ride_status = 'COMPLETED'

),

revenue as (

    select
        requested_date                                              as ride_date,
        vehicle_category,
        fare_tier,
        vehicle_type_code,
        pickup_province,

        -- ── Volume ────────────────────────────────────────────────
        count(*)                                                    as completed_trips,
        countif(is_surge)                                           as surge_trips,
        countif(has_promo)                                          as promo_trips,

        -- ── GMV ───────────────────────────────────────────────────
        round(sum(total_fare), 0)                                   as gmv_idr,
        round(avg(total_fare), 0)                                   as avg_gmv_per_trip,

        -- ── Fare composition ──────────────────────────────────────
        round(sum(base_fare), 0)                                    as total_base_fare,
        round(sum(distance_fare), 0)                                as total_distance_fare,
        round(sum(time_fare), 0)                                    as total_time_fare,

        -- ── Surge uplift = fare - fare_without_surge ──────────────
        round(sum(
            total_fare - (total_fare / nullif(surge_multiplier, 0))
        ), 0)                                                       as surge_uplift_idr,

        -- ── Promo burn ────────────────────────────────────────────
        round(sum(promo_discount), 0)                               as promo_discount_idr,

        -- ── Platform / driver split (80/20) ───────────────────────
        round(sum(total_fare) * 0.20, 0)                            as platform_revenue_idr,
        round(sum(total_fare) * 0.80, 0)                            as driver_earnings_idr,

        -- ── Net revenue (platform – promo) ────────────────────────
        round(sum(total_fare) * 0.20 - sum(promo_discount), 0)     as net_platform_revenue_idr,

        -- ── Revenue per km ────────────────────────────────────────
        round(
            safe_divide(sum(total_fare), nullif(sum(distance_km), 0)),
            0
        )                                                           as revenue_per_km,

        current_timestamp()                                         as refreshed_at

    from rides
    group by 1, 2, 3, 4, 5

)

select * from revenue
