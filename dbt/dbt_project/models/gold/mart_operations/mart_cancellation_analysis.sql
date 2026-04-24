{{
  config(
    materialized = 'table',
    partition_by = {
      'field': 'ride_date',
      'data_type': 'date',
      'granularity': 'day'
    },
    tags         = ['gold', 'operations', 'cancellation']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  mart_cancellation_analysis
--  Purpose : Cancellation funnel by reason, vehicle type, zone.
--  Used by : Quality & ops team to reduce cancellation rate.
-- ══════════════════════════════════════════════════════════════

with rides as (

    select * from {{ ref('fct_rides') }}

),

cancellations as (

    select
        requested_date                                              as ride_date,
        coalesce(cancellation_reason, 'Unknown')                    as cancellation_reason,
        vehicle_type_code,
        vehicle_type_name,
        pickup_zone_code,
        pickup_zone_name,
        pickup_city,
        time_bucket,

        count(*)                                                    as cancelled_count,

        -- ── Context: were drivers close? ──────────────────────────
        -- acceptance_duration_secs null = driver never accepted → NO_DRIVER or immediate cancel
        countif(acceptance_duration_secs is null)                   as no_acceptance_count,
        countif(acceptance_duration_secs is not null)               as post_acceptance_count,

        round(avg(
            case when acceptance_duration_secs is not null
                 then acceptance_duration_secs / 60.0 end
        ), 2)                                                       as avg_time_to_accept_min,

        current_timestamp()                                         as refreshed_at

    from rides
    where ride_status in ('CANCELLED', 'NO_DRIVER')
    group by 1, 2, 3, 4, 5, 6, 7, 8

)

select * from cancellations
