{{
  config(
    materialized = 'table',
    tags         = ['silver', 'dimension', 'date']
  )
}}

-- ══════════════════════════════════════════════════════════════
--  dim_date
--  Purpose : Date spine from data_start_date to today + 90 days.
--  Includes Indonesian public holiday flags and business calendar.
-- ══════════════════════════════════════════════════════════════

with date_spine as (

    {{ dbt_utils.date_spine(
        datepart   = "day",
        start_date = "cast('" ~ var('data_start_date') ~ "' as date)",
        end_date   = "date_add(current_date(), interval 90 day)"
    ) }}

),

enriched as (

    select
        {{ surrogate_key(['cast(date_day as string)']) }}           as date_key,
        cast(date_day as date)                                      as date_id,
        date_day                                                    as full_date,

        -- ── Calendar attributes ───────────────────────────────────
        extract(year  from date_day)                                as year_num,
        extract(month from date_day)                                as month_num,
        extract(day   from date_day)                                as day_of_month,
        extract(dayofweek from date_day)                            as day_of_week_num,   -- 1=Sun
        extract(dayofyear from date_day)                            as day_of_year,
        extract(week  from date_day)                                as week_of_year,
        extract(quarter from date_day)                              as quarter_num,

        -- ── Formatted labels ──────────────────────────────────────
        format_date('%A',   date_day)                               as day_name,
        format_date('%B',   date_day)                               as month_name,
        format_date('%Y%m', date_day)                               as year_month,
        format_date('%Y-Q%Q', date_day)                             as year_quarter,
        format_date('%Y-W%V', date_day)                             as iso_year_week,

        -- ── Boolean flags ─────────────────────────────────────────
        case when extract(dayofweek from date_day) in (1, 7)
             then true else false end                               as is_weekend,
        case when extract(dayofweek from date_day) not in (1, 7)
             then true else false end                               as is_weekday,

        -- TODO: replace with seed table for accurate public holiday flags
        false                                                       as is_public_holiday,

        -- ── Relative helpers ──────────────────────────────────────
        date_diff(current_date(), date_day, day)                    as days_ago,
        case
            when date_diff(current_date(), date_day, day) <= 7   then 'last_7d'
            when date_diff(current_date(), date_day, day) <= 30  then 'last_30d'
            when date_diff(current_date(), date_day, day) <= 90  then 'last_90d'
            when date_day > current_date()                        then 'future'
            else 'older'
        end                                                         as recency_bucket

    from date_spine

)

select * from enriched
