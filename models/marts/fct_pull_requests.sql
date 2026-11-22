-- Fact table: one row per pull request. Every PR-level metric (lead time, review wait,
-- review coverage, open-PR age) should be calculated from this table, so a number
-- means the same thing wherever it is shown.
--
-- Incremental: after the first build, a run recomputes only the PRs whose PR row or any review row was written by a
-- fetch since the previous build (rows with _loaded_at after the previous build's data_as_of, minus a lookback), and
-- replaces those rows by pr_number. A late review on an old PR is written by the fetch together with its PR, so the PR
-- is recomputed from all its reviews. The lookback (var fct_lookback_days, default 3) also recomputes rows written
-- shortly before the previous build, for a fetch that overlapped it. `dbt build --full-refresh` rebuilds every row and
-- must give the same table (ingestion_tests/test_incremental.py checks it on the fixture and the frozen data).
-- data_as_of is one value for the whole table, so a post-hook sets it on every row after each run.

{{
    config(
        materialized='incremental',
        unique_key='pr_number',
        incremental_strategy='delete+insert',
        on_schema_change='fail',
        post_hook="update {{ this }} set data_as_of = (select max(loaded_at) from {{ ref('stg_github__fetch_runs') }}) where data_as_of is distinct from (select max(loaded_at) from {{ ref('stg_github__fetch_runs') }})"
    )
}}

{#- Referenced here, outside the incremental branch, so dbt always knows they come before this model. -#}
{%- set pull_requests = ref('stg_github__pull_requests') -%}
{%- set reviews = ref('stg_github__pr_reviews') -%}

with lifecycle as (

    select * from {{ ref('int_pr_lifecycle') }}

),

-- One value for the whole table: when the data was last pulled from GitHub (the last fetch run).
-- Anything "as of now" (like how long a PR has been open) must be measured against
-- this, not today's date, because the data is only as fresh as the last fetch.
load_info as (

    select max(loaded_at) as data_as_of
    from {{ ref('stg_github__fetch_runs') }}

){% if is_incremental() %},

-- The PRs to recompute: written (PR row or any review) since the previous build, minus the lookback.
since_previous_build as (

    select max(data_as_of) - interval '{{ var("fct_lookback_days", 3) }} days' as since
    from {{ this }}

),

changed_prs as (

    select pr_number from {{ pull_requests }}
    where _loaded_at > (select since from since_previous_build)
    union
    select pr_number from {{ reviews }}
    where _loaded_at > (select since from since_previous_build)

){% endif %}

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

    -- The same "at or before the merge" rule for approvals. was_approved (any time) is kept as it was; this is the
    -- version to use when a figure should mean "approved before it was merged".
    coalesce(lc.is_merged and lc.first_approved_at <= lc.merged_at, false) as was_approved_before_merge,

    lc.review_wait_hours,
    lc.time_in_review_hours,
    lc.lead_time_hours,

    li.data_as_of

from lifecycle lc
cross join load_info li
{% if is_incremental() %}
where lc.pr_number in (select pr_number from changed_prs)
{% endif %}
