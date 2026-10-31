with source as (

    select * from {{ source('github', 'github_pull_requests') }}

),

cleaned as (

    select
        number as pr_number,
        title as pr_title,
        state as pr_state,
        merged as is_merged,
        author as author_login,
        cast(created_at as timestamp) as created_at,
        cast(updated_at as timestamp) as updated_at,
        cast(merged_at as timestamp) as merged_at,
        cast(closed_at as timestamp) as closed_at,
        cast(_loaded_at as timestamp) as _loaded_at

    from source

)

select * from cleaned
