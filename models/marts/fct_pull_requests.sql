-- Fact table: one row per pull request. Every PR-level metric (lead time, review wait,
-- review coverage, open-PR age) should be calculated from this table, so a number
-- means the same thing wherever it is shown.

with lifecycle as (

    select * from {{ ref('int_pr_lifecycle') }}

),

-- One value for the whole table: when the data was last pulled from GitHub.
-- Anything "as of now" (like how long a PR has been open) must be measured against
-- this, not today's date, because the data is only as fresh as the last fetch.
load_info as (

    select max(_loaded_at) as data_as_of
    from {{ ref('stg_github__pull_requests') }}

)

select
    lc.pr_number,
    lc.pr_title,
    lc.pr_state,
    lc.pr_lifecycle_stage,
    lc.is_merged,
    lc.author_login,

    -- GitHub names automated accounts like "dependabot[bot]"
    coalesce(ends_with(lc.author_login, '[bot]'), false) as is_bot,

    lc.opened_at,
    lc.first_reviewed_at,
    lc.last_reviewed_at,
    lc.merged_at,
    lc.closed_at,

    lc.review_count,
    lc.distinct_reviewer_count,
    lc.was_approved,
    lc.had_changes_requested,
    lc.was_reviewed,

    -- Review coverage should count a review that happened BEFORE the merge. A comment
    -- left after the PR was already merged is not a review of the change.
    coalesce(lc.is_merged and lc.first_reviewed_at <= lc.merged_at, false) as was_reviewed_before_merge,

    lc.review_wait_hours,
    lc.time_in_review_hours,
    lc.lead_time_hours,

    li.data_as_of

from lifecycle lc
cross join load_info li
