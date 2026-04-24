-- ══════════════════════════════════════════════════════════════
--  snapshot_dim_passengers
--
--  Tracks historical changes to passenger attributes over time,
--  most importantly: status, loyalty tier, phone number,
--  is_verified, total_trips, and home zone.
--
--  Source  : bronze_pg.passengers (raw OLTP replica)
--  Strategy: timestamp on updated_at (SCD Type 2)
--
--  dbt adds these columns automatically:
--    dbt_scd_id      → unique surrogate key per version row
--    dbt_updated_at  → when dbt processed this version
--    dbt_valid_from  → timestamp this version became active
--    dbt_valid_to    → timestamp this version was superseded (NULL = current)
--
--  Common analytical queries:
--    - Which passengers were BANNED and later reinstated to ACTIVE?
--    - What was passenger X's loyalty tier on a specific date?
--    - How has total_trips evolved per passenger over time?
--    - When did a passenger first become is_verified = true?
-- ══════════════════════════════════════════════════════════════
{% snapshot snapshot_dim_passengers %}

{{
    config(
        unique_key               = 'passenger_id',
        strategy                 = 'timestamp',
        updated_at               = 'updated_at',
        invalidate_hard_deletes  = True
    )
}}

select
    -- ── Identity ──────────────────────────────────────────────
    passenger_id,
    passenger_code,
    full_name,
    phone_number,
    email,

    -- ── Demographics ──────────────────────────────────────────
    birth_date,
    gender,

    -- ── Tracked mutable attributes ────────────────────────────
    home_zone_id,
    referral_code,
    is_verified,
    total_trips,
    status,

    -- ── Timestamps ────────────────────────────────────────────
    registered_at,
    updated_at,
    _source_system

from {{ source('bronze_pg', 'passengers') }}

{% endsnapshot %}
