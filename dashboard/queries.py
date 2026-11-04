"""All the dashboard's SQL, in one place, so the numbers can be tested without the UI.

Every figure is computed over individual PRs in main.fct_pull_requests, with the same
definitions as the MetricFlow metrics and the agent. Nothing is averaged from daily averages.

Filters used everywhere:
  start, end     inclusive dates (datetime.date)
  include_bots   False drops PRs whose author is an automated account (is_bot)

Which date the range applies to depends on the question:
  merged-PR figures (lead time, review coverage, ...)  -> the day the PR was merged
  opened-PR figures (funnel, open-PR age)               -> the day the PR was opened
"""

import datetime
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parent.parent / "dev.duckdb"


def run(sql: str, params: list | None = None) -> list[tuple]:
    """Run one query on a read-only connection, so this never competes with dbt for the file lock."""
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def run_df(sql: str, params: list | None = None):
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


def data_window() -> tuple[datetime.date, datetime.date, datetime.datetime]:
    """(first day PRs were opened, last day with any PR activity, when the data was pulled)."""
    first_opened, data_as_of = run(
        "select cast(min(opened_at) as date), max(data_as_of) from main.fct_pull_requests"
    )[0]
    return first_opened, data_as_of.date(), data_as_of


# Which dim_date column labels each period. A fixed lookup, so the column name put into
# the SQL below never comes from user input.
GRAIN_COLUMNS = {"week": "week_start_date", "month": "month_start_date"}


def trend(start: datetime.date, end: datetime.date, include_bots: bool, grain: str):
    """Merged PRs and lead time per week or month, for merged PRs in [start, end].

    One row per period that has any day in the range, from dim_date, so a period with no
    merges shows up with 0 PRs and no lead time (None) instead of being missing. The first
    and last period can be partial when the range starts or ends mid-period.
    Weeks start on Monday; period_start is the first day of the week or month.
    """
    if grain not in GRAIN_COLUMNS:
        raise ValueError(f"grain must be one of {sorted(GRAIN_COLUMNS)}, got {grain!r}")
    col = GRAIN_COLUMNS[grain]
    return run_df(
        f"""
        select
            p.period_start,
            coalesce(m.merged_prs, 0) as merged_prs,
            m.median_lead_time_hours,
            m.p90_lead_time_hours
        from (
            select distinct {col} as period_start
            from main.dim_date
            where date_day between ? and ?
        ) p
        left join (
            select
                d.{col} as period_start,
                count(*) as merged_prs,
                median(f.lead_time_hours) as median_lead_time_hours,
                quantile_cont(f.lead_time_hours, 0.9) as p90_lead_time_hours
            from main.fct_pull_requests f
            join main.dim_date d on cast(f.merged_at as date) = d.date_day
            where f.is_merged
              and d.date_day between ? and ?
              and (? or not f.is_bot)
            group by 1
        ) m on p.period_start = m.period_start
        order by p.period_start
        """,
        [start, end, start, end, include_bots],
    )


def lead_time_breakdown(start: datetime.date, end: datetime.date) -> dict:
    """Where a human PR's time to merge goes: waiting for the first review, or in review.

    Human-authored merged PRs (merged in [start, end]) that were reviewed before they were
    merged. For those, lead time = wait for first review + time in review (first review to
    merge), so the two stages add up. Merged human PRs with no review before the merge can't
    be split, and are counted in `not_split` instead.

    `*_share_pct` is each stage's share of all hours these PRs spent open, so it is pulled
    toward the slowest PRs; the medians describe the typical PR.
    """
    (n, wait_median, wait_p90, review_median, review_p90, lead_median, lead_p90,
     wait_hours, review_hours), = run(
        """
        select
            count(*),
            median(review_wait_hours), quantile_cont(review_wait_hours, 0.9),
            median(time_in_review_hours), quantile_cont(time_in_review_hours, 0.9),
            median(lead_time_hours), quantile_cont(lead_time_hours, 0.9),
            sum(review_wait_hours), sum(time_in_review_hours)
        from main.fct_pull_requests
        where is_merged and not is_bot and was_reviewed_before_merge
          and cast(merged_at as date) between ? and ?
        """,
        [start, end],
    )
    (not_split,), = run(
        """
        select count(*)
        from main.fct_pull_requests
        where is_merged and not is_bot and not was_reviewed_before_merge
          and cast(merged_at as date) between ? and ?
        """,
        [start, end],
    )
    stage_hours = (wait_hours or 0) + (review_hours or 0)
    return {
        "split_prs": n,
        "not_split_prs": not_split,
        "median_wait_hours": wait_median,
        "p90_wait_hours": wait_p90,
        "median_in_review_hours": review_median,
        "p90_in_review_hours": review_p90,
        "median_lead_time_hours": lead_median,
        "p90_lead_time_hours": lead_p90,
        "wait_share_pct": (100.0 * wait_hours / stage_hours) if stage_hours else None,
        "in_review_share_pct": (100.0 * review_hours / stage_hours) if stage_hours else None,
    }


def review_funnel(start: datetime.date, end: datetime.date, include_bots: bool) -> dict:
    """What happened to the PRs opened in [start, end]: opened -> reviewed -> approved -> merged.

    "Reviewed" means it got a review while it was still open: any review for a PR that was not
    merged, a review before the merge for one that was. "Approved" means at least one APPROVED
    review (no PR in the data was approved only after merging). Merged PRs are not all
    approved, so the stages are counts, not a strict funnel: merged_without_approval says how
    many merged PRs skipped a stage, and merged_without_review how many of those had no review.
    PRs still open at the data's pull date are counted in opened but cannot have merged yet.
    """
    (opened, reviewed, approved, merged, merged_no_review, merged_no_approval,
     closed_unmerged, still_open), = run(
        """
        select
            count(*),
            count(*) filter (where was_reviewed and (not is_merged or was_reviewed_before_merge)),
            count(*) filter (where was_approved),
            count(*) filter (where is_merged),
            count(*) filter (where is_merged and not was_reviewed_before_merge),
            count(*) filter (where is_merged and not was_approved),
            count(*) filter (where pr_lifecycle_stage = 'closed_without_merge'),
            count(*) filter (where pr_lifecycle_stage = 'open')
        from main.fct_pull_requests
        where cast(opened_at as date) between ? and ?
          and (? or not is_bot)
        """,
        [start, end, include_bots],
    )
    return {
        "opened": opened,
        "reviewed": reviewed,
        "approved": approved,
        "merged": merged,
        "merged_without_review": merged_no_review,
        "merged_without_approval": merged_no_approval,
        "closed_without_merge": closed_unmerged,
        "still_open": still_open,
    }


# Age buckets for open PRs: (label, upper bound in days, exclusive). The last has no upper bound.
AGE_BUCKETS = [
    ("Under 1 week", 7),
    ("1 to 4 weeks", 30),
    ("1 to 3 months", 90),
    ("3 to 6 months", 180),
    ("6 to 12 months", 365),
    ("Over 1 year", None),
]
OLDEST_SHOWN = 10


def open_pr_age(start: datetime.date, end: datetime.date, include_bots: bool) -> dict:
    """How long the PRs still open have been waiting, among PRs opened in [start, end].

    Age is measured up to data_as_of (when the data was pulled), NOT today's date: the data is
    only as fresh as the last pull, and measuring from today would make every PR look a day
    older each day without any new information.
    Returns the count, median and oldest age in days, how many have had no review, an age
    histogram (every bucket present, empty ones as 0), and the oldest PRs.
    """
    df = run_df(
        """
        select
            pr_number,
            pr_title,
            author_login,
            cast(opened_at as date) as opened_on,
            date_diff('second', opened_at, data_as_of) / 86400.0 as age_days,
            was_reviewed,
            was_approved
        from main.fct_pull_requests
        where pr_lifecycle_stage = 'open'
          and cast(opened_at as date) between ? and ?
          and (? or not is_bot)
        order by age_days desc, pr_number
        """,
        [start, end, include_bots],
    )

    def bucket(age: float) -> str:
        for label, upper in AGE_BUCKETS:
            if upper is None or age < upper:
                return label

    counts = df["age_days"].map(bucket).value_counts() if len(df) else {}
    return {
        "open_prs": len(df),
        "median_age_days": float(df["age_days"].median()) if len(df) else None,
        "oldest_age_days": float(df["age_days"].max()) if len(df) else None,
        "never_reviewed": int((~df["was_reviewed"]).sum()) if len(df) else 0,
        "age_buckets": [(label, int(counts.get(label, 0))) for label, _ in AGE_BUCKETS],
        "oldest": df.head(OLDEST_SHOWN).reset_index(drop=True),
    }


TOP_N = 10


def top_reviewers(start: datetime.date, end: datetime.date, limit: int = TOP_N) -> dict:
    """Who reviews the most, among reviews submitted in [start, end]. People only (no bots).

    Ranked by distinct PRs reviewed, not by number of reviews: several comments on one PR
    are one piece of work. A review on your own PR (an author replying in their own thread,
    always a plain comment in this data) is not a review of someone else's work and is left
    out; `own_pr_comments_left_out` says how many. Reviews of bot-authored PRs (such as
    dependabot) do count, since a person still looked at them.
    """
    df = run_df(
        """
        select
            r.reviewer_login as reviewer,
            count(distinct r.pr_number) as prs_reviewed,
            count(*) as reviews,
            count(*) filter (where r.review_state = 'APPROVED') as approvals
        from main.stg_github__pr_reviews r
        join main.fct_pull_requests p on p.pr_number = r.pr_number
        join main.dim_contributors d on d.contributor_login = r.reviewer_login
        where not d.is_bot
          and r.reviewer_login <> p.author_login
          and cast(r.submitted_at as date) between ? and ?
        group by 1
        order by prs_reviewed desc, reviews desc, reviewer
        limit ?
        """,
        [start, end, limit],
    )
    (left_out,), = run(
        """
        select count(*)
        from main.stg_github__pr_reviews r
        join main.fct_pull_requests p on p.pr_number = r.pr_number
        join main.dim_contributors d on d.contributor_login = r.reviewer_login
        where not d.is_bot
          and r.reviewer_login = p.author_login
          and cast(r.submitted_at as date) between ? and ?
        """,
        [start, end],
    )
    return {"reviewers": df, "own_pr_comments_left_out": left_out}


def top_authors(start: datetime.date, end: datetime.date, limit: int = TOP_N):
    """Who opens the most PRs, among PRs opened in [start, end]. People only (no bots)."""
    return run_df(
        """
        select
            p.author_login as author,
            count(*) as prs_opened,
            count(*) filter (where p.is_merged) as prs_merged,
            count(*) filter (where p.pr_lifecycle_stage = 'open') as still_open
        from main.fct_pull_requests p
        join main.dim_contributors d on d.contributor_login = p.author_login
        where not d.is_bot
          and cast(p.opened_at as date) between ? and ?
        group by 1
        order by prs_opened desc, prs_merged desc, author
        limit ?
        """,
        [start, end, limit],
    )


def kpis(start: datetime.date, end: datetime.date, include_bots: bool) -> dict:
    """The headline figures for merged PRs in [start, end]. Missing values are None, never 0."""
    (merged, median_lead, p90_lead, reviewed, median_wait), = run(
        """
        select
            count(*),
            median(lead_time_hours),
            quantile_cont(lead_time_hours, 0.9),
            count(*) filter (where was_reviewed_before_merge),
            median(review_wait_hours) filter (where was_reviewed_before_merge)
        from main.fct_pull_requests
        where is_merged
          and cast(merged_at as date) between ? and ?
          and (? or not is_bot)
        """,
        [start, end, include_bots],
    )
    return {
        "merged_prs": merged,
        "median_lead_time_hours": median_lead,
        "p90_lead_time_hours": p90_lead,
        "reviewed_before_merge_prs": reviewed,
        "review_coverage_pct": (100.0 * reviewed / merged) if merged else None,
        "median_hours_to_first_review": median_wait,
    }
