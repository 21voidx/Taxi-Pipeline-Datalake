{{
  config(
    materialized = 'table',
    tags         = ['silver', 'dimension', 'zones']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  dim_zones
--  Purpose : Zone lookup enriched with region classifications.
-- ══════════════════════════════════════════════════════════════

with source as (

    select * from {{ source('bronze_pg', 'zones') }}

)

select
    {{ surrogate_key(['zone_id']) }}    as zone_key,
    zone_id,
    zone_code,
    zone_name,
    city,
    latitude,
    longitude,
    is_active,

    -- ── Region classification ─────────────────────────────────
    case
        when city = 'Jakarta'    then 'DKI Jakarta'
        when city = 'Banten'     then 'Banten'
        when city = 'Jawa Barat' then 'Jawa Barat'
        else 'Other'
    end                          as province,

    case
        when zone_code like 'JKT-%' then 'Jakarta Core'
        else 'Greater Jakarta'
    end                          as area_type,

    created_at

from source
where is_active = true
