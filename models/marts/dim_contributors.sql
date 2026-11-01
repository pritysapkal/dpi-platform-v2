-- Dimension table: one row per GitHub login that appears in the data, whether as a
-- PR author, a reviewer, or both. Lets any chart ask "who?" and split bots from people.

with authored as (

    select
        author_login as contributor_login,
        count(*) as prs_authored,
        min(opened_at) as first_authored_at,
        max(opened_at) as last_authored_at

    from {{ ref('fct_pull_requests') }}
    group by author_login

),

reviewed as (

    select
        reviewer_login as contributor_login,
        count(*) as reviews_given,
        count(distinct pr_number) as prs_reviewed,
        min(submitted_at) as first_reviewed_at,
        max(submitted_at) as last_reviewed_at

    from {{ ref('stg_github__pr_reviews') }}
    -- reviews by deleted GitHub accounts arrive with no login; there is no one to attribute them to
    where reviewer_login is not null
    group by reviewer_login

)

select
    coalesce(a.contributor_login, r.contributor_login) as contributor_login,

    -- GitHub names automated accounts like "dependabot[bot]"
    coalesce(ends_with(coalesce(a.contributor_login, r.contributor_login), '[bot]'), false) as is_bot,

    -- earliest and latest sign of life, as an author or as a reviewer
    least(a.first_authored_at, r.first_reviewed_at) as first_activity_at,
    greatest(a.last_authored_at, r.last_reviewed_at) as last_activity_at,

    coalesce(a.prs_authored, 0) as prs_authored,
    coalesce(r.reviews_given, 0) as reviews_given,
    coalesce(r.prs_reviewed, 0) as prs_reviewed

from authored a
full outer join reviewed r on a.contributor_login = r.contributor_login
