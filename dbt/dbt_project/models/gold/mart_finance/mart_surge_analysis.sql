{{
  config(
    materialized = 'table',
    partition_by = {
      'field': 'ride_date',
      'data_type': 'date',
      'granularity': 'day'
    },
    tags         = ['gold', 'finance', 'surge']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  mart_surge_analysis
--  Purpose : Surge pricing effectiveness by zone and time bucket.
--  Used by : Pricing team to calibrate surge thresholds.
-- ══════════════════════════════════════════════════════════════

with rides as (

    select * from {{ ref('fct_rides') }}
    where ride_status = 'COMPLETED'

),

surge as (

    select
        requested_date                                              as ride_date,
        time_bucket,
        pickup_zone_code,
        pickup_zone_name,
        pickup_city,
        vehicle_type_code,

        -- ── Surge multiplier distribution ─────────────────────────
        countif(surge_multiplier = 1.0)                             as flat_trips,
        countif(surge_multiplier = 1.2)                             as surge_12x_trips,
        countif(surge_multiplier = 1.5)                             as surge_15x_trips,
        countif(surge_multiplier = 2.0)                             as surge_20x_trips,
        count(*)                                                    as total_trips,

        round(avg(surge_multiplier), 3)                             as avg_surge_multiplier,
        round(max(surge_multiplier), 2)                             as peak_surge_multiplier,

        -- ── Revenue impact ────────────────────────────────────────
        round(sum(
            total_fare - (total_fare / nullif(surge_multiplier, 0))
        ), 0)                                                       as surge_incremental_idr,

        round(sum(total_fare), 0)                                   as total_fare_idr,

        round(
            safe_divide(
                sum(total_fare - (total_fare / nullif(surge_multiplier, 0))),
                nullif(sum(total_fare), 0)
            ) * 100, 2
        )                                                           as surge_pct_of_revenue,

        current_timestamp()                                         as refreshed_at

    from rides
    group by 1, 2, 3, 4, 5, 6

)

select * from surge
