-- Every date a PR was opened or merged on must exist in dim_date. If the calendar
-- stopped short of the data, later joins would silently lose those PRs. Returns the
-- offending PRs (= test fails) if any date is missing.

select
    f.pr_number,
    f.opened_at,
    f.merged_at
from {{ ref('fct_pull_requests') }} f
left join {{ ref('dim_date') }} d_open on cast(f.opened_at as date) = d_open.date_day
left join {{ ref('dim_date') }} d_merge on cast(f.merged_at as date) = d_merge.date_day
where d_open.date_day is null
   or (f.merged_at is not null and d_merge.date_day is null)
