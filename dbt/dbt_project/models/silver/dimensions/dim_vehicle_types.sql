{{
  config(
    materialized = 'table',
    tags         = ['silver', 'dimension', 'vehicle_types']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  dim_vehicle_types
--  Purpose : Vehicle type dimension with fare tier and category.
-- ══════════════════════════════════════════════════════════════

with source as (

    select * from {{ source('bronze_pg', 'vehicle_types') }}

)

select
    {{ surrogate_key(['vehicle_type_id']) }}    as vehicle_type_key,
    vehicle_type_id,
    type_code,
    type_name,
    base_fare,
    per_km_rate,
    per_minute_rate,
    capacity,

    -- ── Tier grouping ─────────────────────────────────────────
    case
        when type_code = 'MOTOR'                      then '2-Wheeler'
        when type_code in ('ECONOMI', 'COMFORT')      then 'Standard Car'
        when type_code = 'SUV'                        then 'Large Car'
        when type_code = 'PREMIUM'                    then 'Premium Car'
    end                             as vehicle_category,

    case
        when base_fare < 10000  then 'Economy'
        when base_fare < 20000  then 'Mid-Range'
        else 'Premium'
    end                             as fare_tier,

    created_at

from source
