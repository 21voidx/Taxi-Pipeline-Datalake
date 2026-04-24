{{
  config(
    materialized = 'table',
    partition_by = {
      'field': 'ride_date',
      'data_type': 'date',
      'granularity': 'day'
    },
    cluster_by   = ['pickup_zone_code'],
    tags         = ['gold', 'operations', 'heatmap']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  mart_zone_heatmap
--  Purpose : Origin-Destination (OD) matrix + zone demand heatmap.
--  Used by : Geo dashboard — supply/demand imbalance, hotspot zones.
-- ══════════════════════════════════════════════════════════════

with rides as (

    select * from {{ ref('fct_rides') }}
    where ride_status = 'COMPLETED'

),

od_matrix as (

    select
        requested_date                                              as ride_date,
        pickup_zone_code,
        pickup_zone_name,
        pickup_city,
        pickup_province,
        pickup_area_type,
        dropoff_zone_code,
        dropoff_zone_name,
        is_same_zone_trip,
        time_bucket,

        count(*)                                                    as trip_count,
        round(sum(total_fare), 0)                                   as total_fare_idr,
        round(avg(total_fare), 0)                                   as avg_fare_idr,
        round(avg(distance_km), 2)                                  as avg_distance_km,
        round(avg(duration_minutes), 1)                             as avg_duration_min,
        countif(is_surge = true)                                    as surge_trip_count,
        round(avg(surge_multiplier), 2)                             as avg_surge_multiplier,

        current_timestamp()                                         as refreshed_at

    from rides
    group by 1, 2, 3, 4, 5, 6, 7, 8, 9, 10

)

select * from od_matrix
