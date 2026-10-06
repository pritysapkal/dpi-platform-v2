"""Tools the agent can call. Each docstring is sent to Gemini as the tool's description,
so it is written for the model: what the tool does, when to use it, what the args mean.

Every tool returns a dict and never raises: errors come back as {"ok": False, "error": ...}
so the model can read the problem and try again instead of the agent crashing.
"""

import functools

import duckdb

from agent import db, periods
from agent.guardrails import (
    ALLOWED_REPO,
    ALLOWED_TABLES,
    MAX_RESULT_ROWS,
    GuardrailError,
    validate_date_range,
    validate_repo,
    validate_sql,
    validate_table_name,
)

# Descriptions taken from the dbt schema.yml files, so the model knows what each table is.
TABLE_DESCRIPTIONS = {
    "mart_dora_metrics_daily": (
        "One row per calendar day (no gaps) with deployment_frequency (PRs merged that day, "
        "0 on no-merge days) and avg_lead_time_hours (average open-to-merge hours for that "
        "day's merges, null on no-merge days)."
    ),
    "int_pr_lifecycle": (
        "One row per pull request (open, merged, or closed) with its opened -> reviewed -> "
        "merged timeline, review counts, author, and lead_time_hours (open to merge)."
    ),
}

CFR_NOT_AVAILABLE = {
    "available": False,
    "reason": (
        "Change Failure Rate is not computed by DPIP: the pipeline ingests only pull "
        "requests and reviews, with no deployment, incident, or rollback data."
    ),
}

DEPLOYMENT_DEFINITION = "A merged pull request is used as a proxy for a deployment."
LEAD_TIME_DEFINITION = "Hours from a pull request being opened to being merged."
DASHBOARD_FIGURE_NOTE = (
    "The dashboard figure averages the daily averages, so every day counts equally "
    "regardless of how many PRs merged that day; median and PR-weighted mean count every PR once."
)


def safe_tool(func):
    """Turn any exception into an {"ok": False, "error": ...} result the model can read.

    functools.wraps keeps the original name, docstring and type hints, which the
    agent uses to build the tool declarations sent to Gemini.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except GuardrailError as exc:
            return {"ok": False, "error": f"Rejected by guardrail: {exc}"}
        except db.QueryTimeout as exc:
            return {"ok": False, "error": f"{exc} Simplify the query."}
        except duckdb.Error as exc:
            return {"ok": False, "error": f"Database error: {exc}"}
        except FileNotFoundError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # last resort: never crash the agent loop
            return {"ok": False, "error": f"Unexpected error: {type(exc).__name__}: {exc}"}
    return wrapper


def _round(value, digits: int = 2):
    return None if value is None else round(value, digits)


def _resolve_dates(start: str | None, end: str | None, period: str | None, prefix: str = "") -> tuple[str, str, dict | None]:
    """Accept EITHER a period phrase OR explicit dates, never both, and return validated dates.

    A phrase is turned into dates by periods.resolve_period (code), never by the model.
    """
    if period is not None and (start is not None or end is not None):
        raise GuardrailError(
            f"Pass either {prefix}period or {prefix}start_date/{prefix}end_date, not both."
        )
    if period is not None:
        resolved = periods.resolve_period(period)
        if not resolved["ok"]:
            raise GuardrailError(resolved["error"])
        info = {key: resolved[key] for key in ("phrase", "label", "start_date", "end_date")}
        return resolved["start_date"], resolved["end_date"], info
    if start is None or end is None:
        raise GuardrailError(
            f"Provide {prefix}start_date and {prefix}end_date (YYYY-MM-DD), or a {prefix}period phrase "
            "such as 'last quarter'."
        )
    validate_date_range(start, end, prefix=prefix)
    return start, end, None


def _metrics_for_period(start: str, end: str) -> dict:
    """Compute the DORA metrics for one validated date range (inclusive)."""
    coverage = periods.data_coverage()
    # ISO date strings compare correctly as text, so no date parsing is needed here.
    overlap_start = max(start, coverage["min"])
    overlap_end = min(end, coverage["max"])

    result = {
        "period": {"start_date": start, "end_date": end},
        "data_coverage": coverage,
    }
    if overlap_start > overlap_end:
        result["has_data"] = False
        result["message"] = (
            f"No data for {start} to {end}. DPIP data covers {coverage['min']} to "
            f"{coverage['max']}; the last date with data is {coverage['max']}."
        )
        return result

    # Deployment frequency comes straight from the mart, the same source as the dashboard.
    _, rows, _ = db.run_query(
        """
        select
            sum(deployment_frequency),
            count(*),
            avg(avg_lead_time_hours)
        from main.mart_dora_metrics_daily
        where cast(metric_date as date) between ? and ?
        """,
        [start, end],
    )
    merged_total, days_covered, dashboard_lead_time = rows[0]

    # Median and PR-weighted mean need one row per PR, which only int_pr_lifecycle has.
    _, rows, _ = db.run_query(
        """
        select
            count(*),
            median(lead_time_hours),
            avg(lead_time_hours)
        from main.int_pr_lifecycle
        where is_merged and cast(merged_at as date) between ? and ?
        """,
        [start, end],
    )
    prs_measured, median_lead_time, mean_lead_time = rows[0]

    result["has_data"] = True
    result["days_with_data_in_period"] = days_covered
    if days_covered < (_days_between(start, end)):
        result["coverage_note"] = (
            f"Only {overlap_start} to {overlap_end} of the requested period has data; "
            "per-week figures use those days only."
        )
    result["deployment_frequency"] = {
        "merged_prs_total": merged_total,
        "merged_prs_per_week": _round(merged_total / days_covered * 7),
        "definition": DEPLOYMENT_DEFINITION,
    }
    result["lead_time_for_changes"] = {
        "median_hours": _round(median_lead_time),
        "mean_hours_pr_weighted": _round(mean_lead_time),
        "dashboard_figure_hours_day_weighted": _round(dashboard_lead_time),
        "merged_prs_measured": prs_measured,
        "definition": LEAD_TIME_DEFINITION,
        "note": DASHBOARD_FIGURE_NOTE,
    }
    result["change_failure_rate"] = CFR_NOT_AVAILABLE
    return result


def _days_between(start: str, end: str) -> int:
    start_date, end_date = validate_date_range(start, end)
    return (end_date - start_date).days + 1


def _pct_change(a, b):
    """Percent change from a to b; None when a is missing or zero (undefined)."""
    if a is None or b is None or a == 0:
        return None
    return round((b - a) / a * 100, 1)


# ---------- tools exposed to the model ----------

@safe_tool
def list_tables() -> dict:
    """List the database tables you are allowed to query, with a short description of each.

    Use this first if you are unsure which table holds the data you need.
    """
    return {
        "ok": True,
        "tables": [
            {"name": f"main.{name}", "description": TABLE_DESCRIPTIONS[name]}
            for name in sorted(ALLOWED_TABLES)
        ],
    }


@safe_tool
def get_schema(table_name: str) -> dict:
    """Get the column names and data types of one allowed table.

    Args:
        table_name: An allowed table, e.g. "mart_dora_metrics_daily" or "main.int_pr_lifecycle".
    """
    name = validate_table_name(table_name)
    _, rows, _ = db.run_query(
        """
        select column_name, data_type
        from information_schema.columns
        where table_schema = 'main' and table_name = ?
        order by ordinal_position
        """,
        [name],
    )
    return {
        "ok": True,
        "table": f"main.{name}",
        "description": TABLE_DESCRIPTIONS[name],
        "columns": [{"name": col, "type": dtype} for col, dtype in rows],
    }


@safe_tool
def get_data_coverage() -> dict:
    """Get which repository and which date range DPIP has data for.

    Call this before answering questions about relative periods like "last quarter" or
    "this year", to check whether the period actually has data.
    """
    coverage = periods.data_coverage()
    _, rows, _ = db.run_query(
        """
        select
            count(*),
            count(*) filter (where is_merged),
            cast(min(opened_at) as date),
            cast(max(opened_at) as date)
        from main.int_pr_lifecycle
        """
    )
    total_prs, merged_prs, first_opened, last_opened = rows[0]
    return {
        "ok": True,
        "repo": ALLOWED_REPO,
        "repos_available": [ALLOWED_REPO],
        "metrics_first_date": coverage["min"],
        "metrics_last_date": coverage["max"],
        "metrics_date_note": "DORA metric dates are PR merge dates; there are no merges after metrics_last_date.",
        "pull_requests_total": total_prs,
        "pull_requests_merged": merged_prs,
        "pull_requests_first_opened": first_opened,
        "pull_requests_last_opened": last_opened,
        "metrics_available": ["deployment_frequency", "lead_time_for_changes"],
        "metrics_not_available": {"change_failure_rate": CFR_NOT_AVAILABLE["reason"]},
    }


@safe_tool
def resolve_period(phrase: str) -> dict:
    """Turn a time phrase into exact start/end dates, computed in code, and say whether
    DPIP has data for it. Use this instead of working out dates yourself.

    Supports: today, yesterday, this/last week, this/last month, this/last quarter,
    this/last year, last N days/weeks/months, "Q1 2025", "H1 2024", "October 2024", "2024".

    Args:
        phrase: The time phrase exactly as the user said it, e.g. "last quarter".
    """
    return periods.resolve_period(phrase)


@safe_tool
def get_dora_metrics(
    start_date: str | None = None,
    end_date: str | None = None,
    repo: str | None = None,
    period: str | None = None,
) -> dict:
    """Get the DORA metrics for one period: EITHER a time phrase in `period`
    (e.g. "last quarter", "Q1 2025", "this year"), resolved to dates in code,
    OR explicit start_date and end_date (both inclusive). Never both.

    For relative phrases like "last quarter" or "this year", always pass the phrase
    as `period`; do not compute the dates yourself.

    Returns deployment frequency (merged PRs total and per week) and lead time for
    changes (median hours as the headline figure, PR-weighted mean, and the dashboard's
    day-weighted average). Change failure rate is reported as not available.

    Args:
        start_date: First day of the period, format YYYY-MM-DD. Omit when using `period`.
        end_date: Last day of the period, format YYYY-MM-DD. Omit when using `period`.
        repo: Optional repository "owner/name". Only "encode/httpx" has data.
        period: A time phrase as the user said it, e.g. "last quarter". Omit when using dates.
    """
    repo_name = validate_repo(repo)
    start, end, resolved = _resolve_dates(start_date, end_date, period)
    result = {"ok": True, "repo": repo_name}
    if resolved:
        result["resolved_period"] = resolved
    return {**result, **_metrics_for_period(start, end)}


@safe_tool
def compare_periods(
    period_a_start: str | None = None,
    period_a_end: str | None = None,
    period_b_start: str | None = None,
    period_b_end: str | None = None,
    repo: str | None = None,
    period_a: str | None = None,
    period_b: str | None = None,
) -> dict:
    """Compare DORA metrics between two periods, with the percent change from A to B.

    Each period is EITHER a time phrase (period_a / period_b, e.g. "last year",
    "Q4 2024"), resolved to dates in code, OR explicit start/end dates. Never both
    for the same period. For relative phrases, always pass the phrase; do not
    compute the dates yourself.

    Use period A for the earlier/baseline period and period B for the later one.
    A positive lead-time change means changes got SLOWER; a positive deployment change
    means MORE merges.

    Args:
        period_a_start: First day of period A (baseline), YYYY-MM-DD. Omit when using period_a.
        period_a_end: Last day of period A, YYYY-MM-DD. Omit when using period_a.
        period_b_start: First day of period B, YYYY-MM-DD. Omit when using period_b.
        period_b_end: Last day of period B, YYYY-MM-DD. Omit when using period_b.
        repo: Optional repository "owner/name". Only "encode/httpx" has data.
        period_a: Time phrase for period A, e.g. "last year". Omit when using dates.
        period_b: Time phrase for period B, e.g. "this year". Omit when using dates.
    """
    repo_name = validate_repo(repo)
    a_start, a_end, a_resolved = _resolve_dates(period_a_start, period_a_end, period_a, prefix="period_a_")
    b_start, b_end, b_resolved = _resolve_dates(period_b_start, period_b_end, period_b, prefix="period_b_")

    a = _metrics_for_period(a_start, a_end)
    b = _metrics_for_period(b_start, b_end)
    if a_resolved:
        a["resolved_period"] = a_resolved
    if b_resolved:
        b["resolved_period"] = b_resolved
    result = {"ok": True, "repo": repo_name, "period_a": a, "period_b": b}

    if not (a["has_data"] and b["has_data"]):
        result["percent_change_a_to_b"] = None
        result["message"] = "At least one period has no data, so no change can be computed."
        return result

    a_dep, b_dep = a["deployment_frequency"], b["deployment_frequency"]
    a_lt, b_lt = a["lead_time_for_changes"], b["lead_time_for_changes"]
    result["percent_change_a_to_b"] = {
        "merged_prs_total": _pct_change(a_dep["merged_prs_total"], b_dep["merged_prs_total"]),
        "merged_prs_per_week": _pct_change(a_dep["merged_prs_per_week"], b_dep["merged_prs_per_week"]),
        "lead_time_median_hours": _pct_change(a_lt["median_hours"], b_lt["median_hours"]),
        "lead_time_mean_hours_pr_weighted": _pct_change(
            a_lt["mean_hours_pr_weighted"], b_lt["mean_hours_pr_weighted"]
        ),
    }
    return result


@safe_tool
def run_readonly_sql(query: str) -> dict:
    """Run a read-only SQL query for questions the other tools cannot answer
    (e.g. per-author counts, review statistics, monthly breakdowns).

    Rules, enforced in code: one SELECT or WITH statement only; only the tables
    main.mart_dora_metrics_daily and main.int_pr_lifecycle; no table functions; CTE
    names must not reuse real table names; at most 200 rows are returned; 10 second
    time limit. Uses DuckDB SQL syntax.

    Args:
        query: A single DuckDB SELECT statement.
    """
    validate_sql(query, db.existing_object_names())
    columns, rows, truncated = db.run_query(query, max_rows=MAX_RESULT_ROWS)
    result = {"ok": True, "columns": columns, "rows": rows, "row_count": len(rows)}
    if truncated:
        result["truncated"] = True
        result["note"] = f"Only the first {MAX_RESULT_ROWS} rows are shown. Aggregate or filter to see everything."
    return result


# The agent looks tools up by name in this registry when the model asks to call one.
TOOLS = {
    func.__name__: func
    for func in (
        list_tables,
        get_schema,
        get_data_coverage,
        resolve_period,
        get_dora_metrics,
        compare_periods,
        run_readonly_sql,
    )
}
