-- ══════════════════════════════════════════════════════════════
--  tests/assert_ride_timestamp_order.sql
--
--  Singular data test: ride lifecycle timestamps must be
--  chronologically ordered:
--    requested_at <= accepted_at <= picked_up_at <= completed_at
-- ══════════════════════════════════════════════════════════════

select
    ride_id,
    ride_code,
    ride_status,
    requested_at,
    accepted_at,
    picked_up_at,
    completed_at
from {{ ref('fct_rides') }}
where
    -- accepted must be after requested
    (accepted_at is not null and accepted_at < requested_at)
    -- picked_up must be after accepted
    or (picked_up_at is not null and accepted_at is not null and picked_up_at < accepted_at)
    -- completed must be after picked_up
    or (completed_at is not null and picked_up_at is not null and completed_at < picked_up_at)
