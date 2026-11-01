-- Built on the PR-grain fact table, so the daily numbers are sums over the same rows
-- every other PR metric uses.
with merged_prs as (

    select *
    from {{ ref('fct_pull_requests') }}
    where is_merged = true

),

date_bounds as (

    select
        min(date_trunc('day', merged_at)) as min_date,
        max(date_trunc('day', merged_at)) as max_date
    from merged_prs

),

-- a full calendar spine so days with zero merges show up as 0, not a missing row
date_spine as (

    select unnest(generate_series(min_date, max_date, interval 1 day)) as metric_date
    from date_bounds

),

daily_merge_stats as (

    select
        date_trunc('day', merged_at) as metric_date,
        count(*) as deployment_frequency,
        round(avg(lead_time_hours), 2) as avg_lead_time_hours

    from merged_prs
    group by 1

)

select
    ds.metric_date,
    coalesce(dms.deployment_frequency, 0) as deployment_frequency,
    dms.avg_lead_time_hours

from date_spine ds
left join daily_merge_stats dms on ds.metric_date = dms.metric_date
order by ds.metric_date
