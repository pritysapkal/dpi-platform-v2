"""Tool tests against the real dev.duckdb (read-only). No Gemini API calls.

Skipped automatically if dev.duckdb has not been built (e.g. a fresh clone).
Expected numbers below were computed directly with SQL against the mart.
"""

import duckdb
import pytest

from agent import db, tools
from agent.config import DB_PATH

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="dev.duckdb not built")


# ---------- database-level defence (layer 3) ----------

@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM main.mart_dora_metrics_daily",
        "CREATE TABLE x (a INT)",
        "SELECT * FROM read_csv('anything.csv')",
        "INSTALL httpfs",
        "ATTACH 'other.db'",
        "SET enable_external_access = true",
    ],
)
def test_connection_itself_refuses_writes_and_file_access(statement):
    # Bypasses guardrails.py on purpose: proves the DB blocks these on its own.
    con = db.get_connection()
    try:
        with pytest.raises(duckdb.Error):
            con.execute(statement)
    finally:
        con.close()


def test_query_timeout():
    # 523^3 = 143M row cross join, cut off by a tiny timeout.
    heavy = (
        "SELECT count(*) FROM main.int_pr_lifecycle a, main.int_pr_lifecycle b, "
        "main.int_pr_lifecycle c WHERE a.lead_time_hours + b.lead_time_hours > c.review_count"
    )
    with pytest.raises(db.QueryTimeout):
        db.run_query(heavy, timeout_seconds=0.05)


# ---------- run_readonly_sql ----------

def test_run_readonly_sql_returns_error_dict_instead_of_raising():
    result = tools.run_readonly_sql("DROP TABLE main.mart_dora_metrics_daily")
    assert result["ok"] is False
    assert "guardrail" in result["error"]


def test_run_readonly_sql_blocks_non_whitelisted_table():
    result = tools.run_readonly_sql("SELECT * FROM raw.github_pull_requests")
    assert result["ok"] is False and "not allowed" in result["error"]


def test_run_readonly_sql_caps_rows_at_200():
    result = tools.run_readonly_sql("SELECT pr_number FROM main.int_pr_lifecycle")  # 523 rows
    assert result["ok"] is True
    assert result["row_count"] == 200
    assert result["truncated"] is True


def test_run_readonly_sql_bad_column_returns_error_dict():
    result = tools.run_readonly_sql("SELECT no_such_column FROM main.int_pr_lifecycle")
    assert result["ok"] is False and "Database error" in result["error"]


# ---------- structured tools ----------

def test_list_tables_only_whitelisted():
    names = {t["name"] for t in tools.list_tables()["tables"]}
    assert names == {"main.mart_dora_metrics_daily", "main.int_pr_lifecycle", "main.fct_pull_requests"}


def test_every_listed_table_has_a_description_and_the_mart_warns_about_average_lead_time():
    tables = {t["name"]: t["description"] for t in tools.list_tables()["tables"]}
    assert all(tables.values())
    assert "Do NOT use avg_lead_time_hours" in tables["main.mart_dora_metrics_daily"]
    assert "PREFERRED" in tables["main.fct_pull_requests"]


def test_get_schema_fact_table():
    result = tools.get_schema("main.fct_pull_requests")
    cols = {c["name"] for c in result["columns"]}
    assert {"pr_number", "is_bot", "lead_time_hours", "was_reviewed_before_merge", "data_as_of"} <= cols


def test_run_readonly_sql_on_fact_table_works_and_dimensions_are_blocked():
    ok = tools.run_readonly_sql("select count(*), count(*) filter (where is_merged) from main.fct_pull_requests")
    assert ok["ok"] is True and ok["rows"] == [[523, 288]]
    blocked = tools.run_readonly_sql("select * from main.dim_contributors")
    assert blocked["ok"] is False and "not allowed" in blocked["error"]


def test_get_schema_rejects_raw_table():
    result = tools.get_schema("raw.github_pull_requests")
    assert result["ok"] is False


def test_get_schema_mart():
    cols = [c["name"] for c in tools.get_schema("mart_dora_metrics_daily")["columns"]]
    assert cols == ["metric_date", "deployment_frequency", "avg_lead_time_hours"]


def test_data_coverage():
    result = tools.get_data_coverage()
    assert result["metrics_first_date"] == "2023-01-02"  # first merge in the 2023-01-01+ window
    assert result["metrics_last_date"] == "2026-02-23"
    assert result["pull_requests_total"] == 523
    assert result["pull_requests_merged"] == 288
    assert result["repos_available"] == ["encode/httpx"]


def test_full_range_matches_mart_totals():
    result = tools.get_dora_metrics("2023-01-01", "2026-12-31")
    assert result["deployment_frequency"]["merged_prs_total"] == 288
    lead = result["lead_time_for_changes"]
    assert lead["merged_prs_measured"] == 288  # mart and PR-grain table agree
    assert lead["median_hours"] == 20.62
    assert lead["p90_hours"] == 483.04
    assert lead["mean_hours_pr_weighted"] == 235.64
    # the average-of-daily-averages figure was removed and must not come back
    assert not any("day_weighted" in key or "dashboard" in key for key in lead)
    assert result["change_failure_rate"]["available"] is False
    assert "coverage_note" in result  # requested range is wider than the data


def test_period_with_no_data():
    result = tools.get_dora_metrics("2026-07-01", "2026-09-30")
    assert result["ok"] is True and result["has_data"] is False
    assert "2026-02-23" in result["message"]


def test_bad_dates_return_error_dict():
    assert tools.get_dora_metrics("2024-13-01", "2024-12-31")["ok"] is False
    assert tools.get_dora_metrics("2024-12-31", "2024-01-01")["ok"] is False


def test_unknown_repo_returns_error_dict():
    result = tools.get_dora_metrics("2024-01-01", "2024-12-31", repo="facebook/react")
    assert result["ok"] is False and "encode/httpx" in result["error"]


def test_compare_periods_2023_vs_2024():
    result = tools.compare_periods("2023-01-01", "2023-12-31", "2024-01-01", "2024-12-31")
    assert result["period_a"]["deployment_frequency"]["merged_prs_total"] == 149
    assert result["period_b"]["deployment_frequency"]["merged_prs_total"] == 109
    assert result["percent_change_a_to_b"]["merged_prs_total"] == round((109 - 149) / 149 * 100, 1)


# ---------- period phrases (resolved in code) ----------

def test_get_dora_metrics_with_period_phrase():
    result = tools.get_dora_metrics(period="2024")
    assert result["resolved_period"]["start_date"] == "2024-01-01"
    assert result["deployment_frequency"]["merged_prs_total"] == 109


def test_get_dora_metrics_rejects_period_and_dates_together():
    result = tools.get_dora_metrics(start_date="2024-01-01", end_date="2024-12-31", period="2024")
    assert result["ok"] is False and "not both" in result["error"]


def test_get_dora_metrics_needs_dates_or_period():
    result = tools.get_dora_metrics()
    assert result["ok"] is False and "period" in result["error"]


def test_get_dora_metrics_unrecognised_period():
    result = tools.get_dora_metrics(period="next sprint")
    assert result["ok"] is False and "Unrecognised period" in result["error"]


def test_compare_periods_with_phrases():
    result = tools.compare_periods(period_a="2023", period_b="2024")
    assert result["period_a"]["deployment_frequency"]["merged_prs_total"] == 149
    assert result["period_b"]["resolved_period"]["label"] == "2024"


def test_compare_periods_rejects_both_for_one_side():
    result = tools.compare_periods(period_a="2023", period_a_start="2023-01-01", period_a_end="2023-12-31",
                                   period_b="2024")
    assert result["ok"] is False and "period_a_" in result["error"]


# ---------- numbers come from fct_pull_requests and match an independent calculation ----------

def raw_merged_lead_times(where: str = "true", params: list | None = None) -> list[float]:
    """Lead times in hours for merged PRs, straight from the RAW tables (not dbt models)."""
    _, rows, _ = db.run_query(
        f"""
        select (epoch(cast(merged_at as timestamp)) - epoch(cast(created_at as timestamp))) / 3600.0
        from raw.github_pull_requests
        where merged_at is not null and {where}
        """,
        params,
    )
    return [r[0] for r in rows]


def raw_stats(hours: list[float]) -> dict:
    import statistics
    ordered = sorted(hours)
    position = 0.9 * (len(ordered) - 1)  # linear interpolation, same as PERCENTILE_CONT(0.9)
    low = int(position)
    p90 = ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (position - low)
    return {"n": len(ordered), "median": statistics.median(ordered), "p90": p90, "mean": statistics.fmean(ordered)}


@pytest.mark.parametrize("include_bots", [True, False])
def test_2024_metrics_match_independent_calculation_from_raw_tables(include_bots):
    where = "merged_at >= '2024-01-01' and merged_at < '2025-01-01'"
    if not include_bots:
        where += " and author not like '%[bot]'"
    expected = raw_stats(raw_merged_lead_times(where))

    result = tools.get_dora_metrics("2024-01-01", "2024-12-31", include_bots=include_bots)
    lead = result["lead_time_for_changes"]
    assert result["includes_bot_prs"] is include_bots
    assert result["deployment_frequency"]["merged_prs_total"] == expected["n"]
    assert lead["merged_prs_measured"] == expected["n"]
    # the dbt model rounds each PR's lead time to 2 decimals, hence the small tolerance
    assert lead["median_hours"] == pytest.approx(expected["median"], abs=0.01)
    assert lead["p90_hours"] == pytest.approx(expected["p90"], abs=0.01)
    assert lead["mean_hours_pr_weighted"] == pytest.approx(expected["mean"], abs=0.01)


def test_2024_values_equal_the_metricflow_query_results():
    # The same numbers `mf query --metrics deployment_frequency,lead_time_median_hours,
    # lead_time_p90_hours,lead_time_mean_hours --group-by metric_time__year` printed for 2024.
    result = tools.get_dora_metrics(period="2024")
    lead = result["lead_time_for_changes"]
    assert result["deployment_frequency"]["merged_prs_total"] == 109
    assert (lead["median_hours"], lead["p90_hours"], lead["mean_hours_pr_weighted"]) == (23.91, 495.83, 284.87)

    humans = tools.get_dora_metrics(period="2024", include_bots=False)
    lead = humans["lead_time_for_changes"]
    assert humans["deployment_frequency"]["merged_prs_total"] == 92
    assert (lead["median_hours"], lead["p90_hours"], lead["mean_hours_pr_weighted"]) == (23.18, 697.91, 331.35)


def test_bot_filter_splits_the_total_and_reports_how_many_bot_prs_there_are():
    everyone = tools.get_dora_metrics("2024-01-01", "2024-12-31")["deployment_frequency"]
    humans = tools.get_dora_metrics("2024-01-01", "2024-12-31", include_bots=False)["deployment_frequency"]
    assert everyone["bot_merged_prs_in_period"] == humans["bot_merged_prs_in_period"] == 17
    assert everyone["merged_prs_total"] - humans["merged_prs_total"] == 17


def test_humans_only_headline_differs_from_all_prs():
    # Why the filter exists: fast bot merges pull the all-PR median down in 2023.
    all_prs = tools.get_dora_metrics(period="2023")["lead_time_for_changes"]["median_hours"]
    humans = tools.get_dora_metrics(period="2023", include_bots=False)["lead_time_for_changes"]["median_hours"]
    assert (all_prs, humans) == (20.47, 29.4)


def test_period_after_the_last_merge_has_no_data_and_does_not_crash():
    quiet = tools.get_dora_metrics("2026-03-01", "2026-03-31")  # data ends 2026-02-23
    assert quiet["ok"] is True and quiet["has_data"] is False


def test_period_inside_the_data_with_zero_merges_gives_zero_and_no_lead_time():
    result = tools.get_dora_metrics("2025-12-01", "2025-12-03")  # three days, no merges
    assert result["ok"] is True and result["has_data"] is True
    assert result["deployment_frequency"]["merged_prs_total"] == 0
    assert result["deployment_frequency"]["merged_prs_per_week"] == 0
    lead = result["lead_time_for_changes"]
    assert lead["merged_prs_measured"] == 0
    assert lead["median_hours"] is None and lead["p90_hours"] is None and lead["mean_hours_pr_weighted"] is None


def test_compare_periods_includes_p90_change_and_respects_bot_filter():
    result = tools.compare_periods(period_a="2023", period_b="2024", include_bots=False)
    assert result["period_a"]["includes_bot_prs"] is False
    assert result["period_a"]["deployment_frequency"]["merged_prs_total"] == 88
    assert result["period_b"]["deployment_frequency"]["merged_prs_total"] == 92
    a_p90 = result["period_a"]["lead_time_for_changes"]["p90_hours"]
    b_p90 = result["period_b"]["lead_time_for_changes"]["p90_hours"]
    assert result["percent_change_a_to_b"]["lead_time_p90_hours"] == round((b_p90 - a_p90) / a_p90 * 100, 1)


def test_include_bots_must_be_a_boolean():
    from agent.agent import execute_tool
    result = execute_tool("get_dora_metrics", {"period": "2024", "include_bots": "yes"}, tools.TOOLS)
    assert result["ok"] is False and "include_bots" in result["error"]


def test_data_coverage_reports_data_as_of_and_bot_count():
    result = tools.get_data_coverage()
    assert result["data_as_of"].startswith("2026-11-01T18:34")
    assert result["bot_pull_requests"] == 104
    assert "not today" in result["data_as_of_note"]
