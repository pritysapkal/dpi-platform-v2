-- Dimension table: one row per calendar day, with the labels charts group and filter by
-- (year, quarter, month, week, weekday). Built on metricflow_time_spine so the calendar
-- is defined once; the time spine itself is left untouched.

with spine as (

    select cast(date_day as date) as date_day
    from {{ ref('metricflow_time_spine') }}

),

-- Keep only the years the data touches: from 1 January of the first PR's year to
-- 31 December of the year the data was pulled. The window moves with the data.
bounds as (

    select
        cast(date_trunc('year', min(opened_at)) as date) as first_day,
        cast(date_trunc('year', max(data_as_of)) + interval 1 year - interval 1 day as date) as last_day
    from {{ ref('fct_pull_requests') }}

)

select
    s.date_day,

    year(s.date_day) as year,
    quarter(s.date_day) as quarter,
    month(s.date_day) as month_number,
    monthname(s.date_day) as month_name,

    -- first day of the month / of the week, handy as a chart axis value for monthly / weekly trends
    cast(date_trunc('month', s.date_day) as date) as month_start_date,
    -- weeks start on Monday, the same convention the agent's period resolver uses
    cast(date_trunc('week', s.date_day) as date) as week_start_date,

    isodow(s.date_day) as day_of_week_number,  -- 1 = Monday ... 7 = Sunday
    dayname(s.date_day) as day_name,
    isodow(s.date_day) in (6, 7) as is_weekend

from spine s
cross join bounds b
where s.date_day between b.first_day and b.last_day
