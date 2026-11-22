-- One row per run of fetch_prs.py, including incremental runs that found nothing new. max(loaded_at) is "when the data
-- was last pulled from GitHub": with incremental loading, the newest _loaded_at of the PR rows only says when a PR last
-- changed, which on a quiet day can be much older than the last pull.

select
    cast(loaded_at as timestamp) as loaded_at,
    mode as fetch_mode,
    prs_written,
    reviews_written

from {{ source('github', 'github_fetch_runs') }}
