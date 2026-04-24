-- ══════════════════════════════════════════════════════════════
--  analyses/ride_funnel.sql
--
--  Exploratory: ride lifecycle funnel — how many requests
--  convert to completions, and where do we lose rides?
--  Run with: dbt compile --select analyses/ride_funnel.sql
-- ══════════════════════════════════════════════════════════════

with base as (

    select
        date_trunc(requested_date, week)    as week_start,
        vehicle_type_code,
        count(*)                            as requested,
        countif(accepted_at is not null)    as accepted,
        countif(picked_up_at is not null)   as picked_up,
        countif(ride_status = 'COMPLETED')  as completed,
        countif(ride_status = 'CANCELLED')  as cancelled,
        countif(ride_status = 'NO_DRIVER')  as no_driver

    from {{ ref('fct_rides') }}
    where requested_date >= date_sub(current_date(), interval 90 day)
    group by 1, 2

)

select
    week_start,
    vehicle_type_code,
    requested,
    accepted,
    picked_up,
    completed,
    cancelled,
    no_driver,

    -- ── Funnel rates ──────────────────────────────────────────
    round(safe_divide(accepted, requested) * 100, 1)    as acceptance_rate_pct,
    round(safe_divide(picked_up, accepted) * 100, 1)    as pickup_rate_pct,
    round(safe_divide(completed, picked_up) * 100, 1)   as completion_rate_pct,
    round(safe_divide(completed, requested) * 100, 1)   as e2e_conversion_pct

from base
order by week_start desc, vehicle_type_code
