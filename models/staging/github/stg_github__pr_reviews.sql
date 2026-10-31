with source as (

    select * from {{ source('github', 'github_pr_reviews') }}

),

cleaned as (

    select
        pr_number,
        review_id,
        reviewer_login,
        review_state,
        cast(submitted_at as timestamp) as submitted_at,
        cast(_loaded_at as timestamp) as _loaded_at

    from source

)

select * from cleaned
