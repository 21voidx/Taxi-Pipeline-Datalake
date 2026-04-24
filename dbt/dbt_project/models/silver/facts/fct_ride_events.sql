{{
  config(
    materialized        = 'incremental',
    incremental_strategy= 'merge',
    unique_key          = 'event_id',
    partition_by        = {
      'field': 'occurred_date',
      'data_type': 'date',
      'granularity': 'day'
    },
    cluster_by          = ['event_type'],
    tags                = ['silver', 'fact', 'events']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  fct_ride_events
--  Purpose : Event-level fact — one row per ride lifecycle event.
--            Bronze source → JSON parsing (staging inlined)
--            → ride context joined from fct_rides.
--  Grain   : event_id
--  Useful  : funnel analysis, acceptance rate, cancellation root cause
-- ══════════════════════════════════════════════════════════════

with source as (

    select * from {{ source('bronze_pg', 'ride_events') }}

    {% if is_incremental() %}
    where occurred_at >= timestamp_sub(
        current_timestamp(),
        interval {{ var('incremental_lookback_days') }} day
    )
    {% endif %}

),

-- ── Inline staging: flatten JSON payload per event_type ───────
parsed as (

    select
        event_id,
        ride_id,
        event_type,
        occurred_at,
        date(occurred_at)  as occurred_date,

        -- ── Typed JSON extractions per event_type ─────────────────
        -- RIDE_REQUESTED  → surge (float), zone (string)
        safe_cast({{ extract_json_bq('event_payload', 'surge') }}    as float64)
                           as req_surge,
        {{ extract_json_bq('event_payload', 'zone') }}
                           as req_zone,

        -- RIDE_ACCEPTED   → driver_id (string)
        {{ extract_json_bq('event_payload', 'driver_id') }}
                           as acc_driver_id,

        -- RIDE_COMPLETED  → fare (float), distance (float)
        safe_cast({{ extract_json_bq('event_payload', 'fare') }}     as float64)
                           as cmp_fare,
        safe_cast({{ extract_json_bq('event_payload', 'distance') }} as float64)
                           as cmp_distance_km,

        -- RIDE_CANCELLED  → reason (string)
        {{ extract_json_bq('event_payload', 'reason') }}
                           as can_reason,

        event_payload

    from source

),

-- ── Ride context (lightweight columns only) ───────────────────
rides as (
    select ride_key, ride_id, ride_status, vehicle_type_id, pickup_zone_id,
           driver_id, passenger_id, total_fare, requested_date
    from {{ ref('fct_rides') }}
),

final as (

    select
        -- ── Surrogate key ──────────────────────────────────────────
        {{ surrogate_key(['e.event_id']) }}                         as event_key,
        r.ride_key,
        -- ── Natural key ───────────────────────────────────────────
        e.event_id,
        e.ride_id,
        e.event_type,
        e.occurred_at,
        e.occurred_date,

        -- ── Parsed payload fields ─────────────────────────────────
        e.req_surge,
        e.req_zone,
        e.acc_driver_id,
        e.cmp_fare,
        e.cmp_distance_km,
        e.can_reason,

        -- ── Ride context (denormalised) ───────────────────────────
        r.ride_status,
        r.vehicle_type_id,
        r.pickup_zone_id,
        r.driver_id,
        r.passenger_id,
        r.total_fare,
        r.requested_date


    from parsed e
    left join rides r on e.ride_id = r.ride_id

)

select * from final
