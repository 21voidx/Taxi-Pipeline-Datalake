-- ══════════════════════════════════════════════════════════════
--  tests/assert_fare_consistency.sql
--
--  Singular data test: completed rides must have total_fare >= 0
--  and fare components must be non-negative.
--  Cancelled rides should have total_fare = 0.
-- ══════════════════════════════════════════════════════════════

select
    ride_id,
    ride_status,
    base_fare,
    distance_fare,
    time_fare,
    total_fare
from {{ ref('fct_rides') }}
where
    -- COMPLETED rides must have a positive total fare
    (ride_status = 'COMPLETED' and total_fare <= 0)
    -- No fare component should ever be negative
    or base_fare < 0
    or distance_fare < 0
    or time_fare < 0
    or total_fare < 0
    -- Cancelled rides should have zero total fare
    or (ride_status = 'CANCELLED' and total_fare > 0)
