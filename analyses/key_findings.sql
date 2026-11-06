-- Key findings: the exact SQL behind every number quoted in the README's "Key findings".
--
-- Each block starts with a "-- @query <name>" marker. To run one, paste it into DuckDB
-- (replace the ref() with main.<table>), or run `dbt compile --select key_findings` and use
-- the compiled SQL. dashboard/tests/test_findings.py runs every block and checks the
-- results, so the README cannot drift away from the data.
--
-- Scope for all of them: pull requests on encode/httpx created on or after 2023-01-01, pulled
-- from GitHub on the date in `data_as_of`. Lead time = PR opened to PR merged. A merge is
-- used as a proxy for a deployment. "Human" = author login does not end in "[bot]".


-- @query data_window
-- first and last PR opened, last merge, and when the data was pulled
select
    cast(min(opened_at) as date) as first_opened,
    cast(max(opened_at) as date) as last_opened,
    cast(max(merged_at) as date) as last_merge,
    cast(max(data_as_of) as date) as data_pulled
from {{ ref('fct_pull_requests') }}


-- @query f1_throughput_by_year
-- Finding 1: human PRs opened per year, human PRs merged per year, and the median lead time of those merges
with opened as (
    select year(opened_at) as yr, count(*) as prs_opened
    from {{ ref('fct_pull_requests') }}
    where not is_bot
    group by 1
),
merged as (
    select year(merged_at) as yr, count(*) as prs_merged, round(median(lead_time_hours), 1) as median_lead_time_hours
    from {{ ref('fct_pull_requests') }}
    where is_merged and not is_bot
    group by 1
)
select o.yr, o.prs_opened, coalesce(m.prs_merged, 0) as prs_merged, m.median_lead_time_hours
from opened o
left join merged m on o.yr = m.yr
order by o.yr


-- @query f1_outcome_of_prs_by_year_opened
-- Finding 1: what became of the human PRs opened in each year (merged / still open / closed without merging)
select
    year(opened_at) as opened_year,
    count(*) as prs_opened,
    count(*) filter (where is_merged) as merged,
    count(*) filter (where pr_lifecycle_stage = 'open') as still_open,
    count(*) filter (where pr_lifecycle_stage = 'closed_without_merge') as closed_without_merge
from {{ ref('fct_pull_requests') }}
where not is_bot
group by 1
order by 1


-- @query f1_since_the_last_merge
-- Finding 1: PRs opened after the last merge on record (all authors, then humans only)
select
    cast(max(merged_at) as date) as last_merge,
    (select count(*) from {{ ref('fct_pull_requests') }} where opened_at > (select max(merged_at) from {{ ref('fct_pull_requests') }})) as prs_opened_since,
    (select count(*) from {{ ref('fct_pull_requests') }} where not is_bot and opened_at > (select max(merged_at) from {{ ref('fct_pull_requests') }})) as human_prs_opened_since
from {{ ref('fct_pull_requests') }}


-- @query f1_backlog
-- Finding 1: human PRs still open when the data was pulled (age measured to data_as_of)
select
    count(*) as open_prs,
    count(*) filter (where not was_reviewed) as never_reviewed,
    count(*) filter (where date_diff('day', opened_at, data_as_of) > 365) as older_than_a_year,
    round(median(date_diff('second', opened_at, data_as_of) / 86400.0), 0) as median_age_days
from {{ ref('fct_pull_requests') }}
where pr_lifecycle_stage = 'open' and not is_bot


-- @query f2_review_coverage_by_year
-- Finding 2: human PRs merged each year, and how many had a review before they were merged
select
    year(merged_at) as merged_year,
    count(*) as merged_prs,
    count(*) filter (where was_reviewed_before_merge) as reviewed_before_merge,
    round(100.0 * count(*) filter (where was_reviewed_before_merge) / count(*), 1) as coverage_pct
from {{ ref('fct_pull_requests') }}
where is_merged and not is_bot
group by 1
order by 1


-- @query f2_unreviewed_merges_by_author
-- Finding 2: human merges with no review before merge, and how many came from the single most frequent author
with per_author as (
    select author_login, count(*) as unreviewed
    from {{ ref('fct_pull_requests') }}
    where is_merged and not is_bot and not was_reviewed_before_merge
    group by 1
)
select sum(unreviewed) as unreviewed_merges, max(unreviewed) as from_the_most_frequent_author, count(*) as distinct_authors
from per_author


-- @query f3_wait_vs_review
-- Finding 3: human PRs merged after a review. Median wait for the first review, median time in review
-- (first review to merge), the wait's share of all hours, and how quickly things happen
select
    count(*) as prs,
    round(median(review_wait_hours), 2) as median_wait_hours,
    round(median(time_in_review_hours), 2) as median_in_review_hours,
    round(100.0 * sum(review_wait_hours) / (sum(review_wait_hours) + sum(time_in_review_hours)), 1) as wait_share_pct,
    round(100.0 * count(*) filter (where review_wait_hours <= 24) / count(*), 1) as first_review_within_a_day_pct,
    round(100.0 * count(*) filter (where review_wait_hours <= 168) / count(*), 1) as first_review_within_a_week_pct,
    round(100.0 * count(*) filter (where time_in_review_hours <= 1) / count(*), 1) as merged_within_an_hour_of_first_review_pct
from {{ ref('fct_pull_requests') }}
where is_merged and not is_bot and was_reviewed_before_merge


-- @query f3_wait_by_year
-- Finding 3: the same, by merge year (2025 and 2026 are small samples)
select
    year(merged_at) as merged_year,
    count(*) as prs,
    round(median(review_wait_hours), 1) as median_wait_hours,
    round(100.0 * sum(review_wait_hours) / (sum(review_wait_hours) + sum(time_in_review_hours)), 1) as wait_share_pct
from {{ ref('fct_pull_requests') }}
where is_merged and not is_bot and was_reviewed_before_merge
group by 1
order by 1


-- @query f4_lead_time_shape
-- Finding 4: lead time of all merged PRs. Median vs mean vs p90, how fast most are, and how much the slowest tenth dominates
with merged as (
    select lead_time_hours, ntile(10) over (order by lead_time_hours) as tenth
    from {{ ref('fct_pull_requests') }}
    where is_merged
)
select
    count(*) as merged_prs,
    round(median(lead_time_hours), 2) as median_hours,
    round(avg(lead_time_hours), 2) as mean_hours,
    round(quantile_cont(lead_time_hours, 0.9), 2) as p90_hours,
    round(100.0 * count(*) filter (where lead_time_hours <= 24) / count(*), 1) as within_a_day_pct,
    round(100.0 * count(*) filter (where lead_time_hours <= 168) / count(*), 1) as within_a_week_pct,
    count(*) filter (where lead_time_hours > 720) as over_30_days,
    round(100.0 * sum(lead_time_hours) filter (where tenth = 10) / sum(lead_time_hours), 1) as slowest_tenth_share_of_hours_pct
from merged


-- @query f4_old_dashboard_figure
-- Finding 4: the figure the dashboard used to headline, the average of the mart's daily averages
select round(avg(avg_lead_time_hours), 2) as average_of_daily_averages
from {{ ref('mart_dora_metrics_daily') }}


-- @query f4_bots_vs_humans
-- Finding 4: merged PRs and lead time for bot-authored vs human-authored PRs
select
    is_bot,
    count(*) as merged_prs,
    round(median(lead_time_hours), 2) as median_hours,
    round(quantile_cont(lead_time_hours, 0.9), 1) as p90_hours
from {{ ref('fct_pull_requests') }}
where is_merged
group by 1
order by 1


-- @query f4_bot_share_by_year
-- Finding 4: how much of each year's merged PRs came from bots
select
    year(merged_at) as merged_year,
    count(*) as merged_prs,
    count(*) filter (where is_bot) as bot_merged_prs,
    round(100.0 * count(*) filter (where is_bot) / count(*), 1) as bot_share_pct
from {{ ref('fct_pull_requests') }}
where is_merged
group by 1
order by 1


-- @query f5_review_concentration
-- Finding 5: of the PRs that got a review from someone other than their author (people only), how many were
-- reviewed by the top 1, top 3 and top 5 reviewers (ranked by distinct PRs reviewed), and the approvals split
with others as (
    select r.pr_number, r.reviewer_login, r.review_state
    from {{ ref('stg_github__pr_reviews') }} r
    join {{ ref('fct_pull_requests') }} p on p.pr_number = r.pr_number
    join {{ ref('dim_contributors') }} d on d.contributor_login = r.reviewer_login
    where not d.is_bot and r.reviewer_login <> p.author_login
),
ranked as (
    select
        reviewer_login,
        row_number() over (order by count(distinct pr_number) desc, count(*) desc, reviewer_login) as rnk
    from others
    group by reviewer_login
)
select
    (select count(distinct pr_number) from others) as prs_reviewed_by_someone_else,
    (select count(distinct reviewer_login) from others) as distinct_reviewers,
    (select count(distinct pr_number) from others where reviewer_login in (select reviewer_login from ranked where rnk <= 1)) as top_1_prs,
    (select count(distinct pr_number) from others where reviewer_login in (select reviewer_login from ranked where rnk <= 3)) as top_3_prs,
    (select count(distinct pr_number) from others where reviewer_login in (select reviewer_login from ranked where rnk <= 5)) as top_5_prs,
    (select count(*) from others where review_state = 'APPROVED') as approvals,
    (select count(*) from others where review_state = 'APPROVED' and reviewer_login in (select reviewer_login from ranked where rnk = 1)) as top_1_approvals
