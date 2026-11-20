with pull_requests as (

    select * from {{ ref('stg_github__pull_requests') }}

),

review_rollup as (

    select
        pr_number,
        min(submitted_at) as first_reviewed_at,
        max(submitted_at) as last_reviewed_at,
        -- when the first approval came (null if none); lets the fact table tell "approved before the merge" from "approved at any time"
        min(submitted_at) filter (where review_state = 'APPROVED') as first_approved_at,
        count(*) as review_count,
        count(distinct reviewer_login) as distinct_reviewer_count,
        bool_or(review_state = 'APPROVED') as was_approved,
        bool_or(review_state = 'CHANGES_REQUESTED') as had_changes_requested

    from {{ ref('stg_github__pr_reviews') }}
    group by pr_number

),

lifecycle as (

    select
        pr.pr_number,
        pr.pr_title,
        pr.pr_state,
        pr.is_merged,
        pr.author_login,
        pr.created_at as opened_at,
        rr.first_reviewed_at,
        rr.last_reviewed_at,
        rr.first_approved_at,
        pr.merged_at,
        pr.closed_at,

        coalesce(rr.review_count, 0) as review_count,
        coalesce(rr.distinct_reviewer_count, 0) as distinct_reviewer_count,
        coalesce(rr.was_approved, false) as was_approved,
        coalesce(rr.had_changes_requested, false) as had_changes_requested,
        rr.first_reviewed_at is not null as was_reviewed,

        -- hours from PR opened to first human review (null if never reviewed)
        round(date_diff('second', pr.created_at, rr.first_reviewed_at) / 3600.0, 2)
            as review_wait_hours,

        -- hours from first review to merge (null if never reviewed, or if the only
        -- review(s) came in after merge — post-merge comments aren't "review time")
        case
            when rr.first_reviewed_at is not null
                and pr.merged_at is not null
                and rr.first_reviewed_at <= pr.merged_at
            then round(date_diff('second', rr.first_reviewed_at, pr.merged_at) / 3600.0, 2)
        end as time_in_review_hours,

        -- hours from opened to merged — feeds the Lead Time for Changes DORA metric later
        round(date_diff('second', pr.created_at, pr.merged_at) / 3600.0, 2)
            as lead_time_hours,

        case
            when pr.is_merged then 'merged'
            when pr.pr_state = 'closed' then 'closed_without_merge'
            else 'open'
        end as pr_lifecycle_stage

    from pull_requests pr
    left join review_rollup rr on pr.pr_number = rr.pr_number

)

select * from lifecycle
