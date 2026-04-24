{{
  config(
    materialized = 'incremental',
    unique_key = 'passenger_key',
    tags         = ['silver', 'dimension', 'passengers']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  dim_passengers
--  Purpose : Clean & standardise passenger data, then enrich
--            with home zone (staging inlined).
--  Cleaning : phone_number → canonical 08XXXXXXXXX
-- ══════════════════════════════════════════════════════════════

WITH snapshot_source AS (
    -- Baca dari hasil snapshot dbt (snapshot_dim_customer)
    SELECT
        passenger_id,
        passenger_code,
        full_name,
        phone_number,
        email,
        birth_date,
        gender,
        home_zone_id,
        referral_code,
        is_verified,
        total_trips,
        status,
        registered_at,
        updated_at,
        dbt_valid_from                          AS valid_from,
        dbt_valid_to                            AS valid_to,
        CASE WHEN dbt_valid_to IS NULL THEN TRUE ELSE FALSE END AS is_current
    FROM {{ ref('snapshot_dim_passengers') }}
    where dbt_valid_to is null
),

cleaned as (

    select
        passenger_id,
        passenger_code,
        full_name,
        {{ clean_phone_id('phone_number') }}    as phone_number_clean,
        phone_number                            as phone_number_raw,
        email,
        birth_date,
        date_diff(current_date(), birth_date, year) as age_years,
        case
            when date_diff(current_date(), birth_date, year) between 18 and 25 then '18-25'
            when date_diff(current_date(), birth_date, year) between 26 and 35 then '26-35'
            when date_diff(current_date(), birth_date, year) between 36 and 45 then '36-45'
            when date_diff(current_date(), birth_date, year) between 46 and 55 then '46-55'
            else '56+'
        end                                     as age_bucket,
        upper(coalesce(gender, 'UNKNOWN'))       as gender,
        home_zone_id,
        referral_code,
        is_verified,
        coalesce(total_trips, 0)                as total_trips,
        upper(status)                           as passenger_status,
        registered_at,
        updated_at,
        case
            when regexp_contains(phone_number, r'^\+62') then true
            when regexp_contains(phone_number, r'-')      then true
            else false
        end                                     as phone_was_dirty,
        valid_from,
        valid_to,
        is_current

    from snapshot_source

),

zones as (

    select * from {{ ref('dim_zones') }}

),

joined as (

    select
        -- ── Surrogate key ──────────────────────────────────────────
        {{ surrogate_key(['p.passenger_id', 'p.valid_from']) }}     as passenger_key,

        -- ── Passenger core ────────────────────────────────────────
        p.passenger_id,
        p.passenger_code,
        p.full_name                     as passenger_name,
        p.phone_number_clean            as phone_number,
        p.email,
        p.birth_date,
        p.age_years,
        p.age_bucket,
        p.gender,
        p.referral_code,
        p.is_verified,
        p.total_trips,
        p.passenger_status,
        p.registered_at,
        p.updated_at,
        p.phone_was_dirty,

        -- ── Loyalty tier ──────────────────────────────────────────
        case
            when p.total_trips >= 200  then 'Platinum'
            when p.total_trips >= 50   then 'Gold'
            when p.total_trips >= 10   then 'Silver'
            else 'New'
        end                             as loyalty_tier,

        -- ── Home zone ─────────────────────────────────────────────
        z.zone_code                     as home_zone_code,
        z.zone_name                     as home_zone_name,
        z.city                          as home_city,
        z.province                      as home_province,
        p.valid_from,
        p.valid_to,
        p.is_current

    from cleaned p
    left join zones z
           on p.home_zone_id = z.zone_id

)

select * from joined
