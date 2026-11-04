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
