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
    # 500^3 = 125M row cross join, cut off by a tiny timeout.
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
    result = tools.run_readonly_sql("SELECT pr_number FROM main.int_pr_lifecycle")  # 500 rows
    assert result["ok"] is True
    assert result["row_count"] == 200
    assert result["truncated"] is True


def test_run_readonly_sql_bad_column_returns_error_dict():
    result = tools.run_readonly_sql("SELECT no_such_column FROM main.int_pr_lifecycle")
    assert result["ok"] is False and "Database error" in result["error"]


# ---------- structured tools ----------

def test_list_tables_only_whitelisted():
    names = {t["name"] for t in tools.list_tables()["tables"]}
    assert names == {"main.mart_dora_metrics_daily", "main.int_pr_lifecycle"}


def test_get_schema_rejects_raw_table():
    result = tools.get_schema("raw.github_pull_requests")
    assert result["ok"] is False


def test_get_schema_mart():
    cols = [c["name"] for c in tools.get_schema("mart_dora_metrics_daily")["columns"]]
    assert cols == ["metric_date", "deployment_frequency", "avg_lead_time_hours"]


def test_data_coverage():
    result = tools.get_data_coverage()
    assert result["metrics_last_date"] == "2026-02-23"
    assert result["repos_available"] == ["encode/httpx"]


def test_full_range_matches_mart_totals():
    result = tools.get_dora_metrics("2023-01-01", "2026-12-31")
    assert result["deployment_frequency"]["merged_prs_total"] == 269
    lead = result["lead_time_for_changes"]
    assert lead["merged_prs_measured"] == 269  # mart and PR-grain table agree
    assert lead["mean_hours_pr_weighted"] == 234.94
    assert lead["dashboard_figure_hours_day_weighted"] == 258.24
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
    assert result["period_a"]["deployment_frequency"]["merged_prs_total"] == 130
    assert result["period_b"]["deployment_frequency"]["merged_prs_total"] == 109
    assert result["percent_change_a_to_b"]["merged_prs_total"] == round((109 - 130) / 130 * 100, 1)


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
    assert result["period_a"]["deployment_frequency"]["merged_prs_total"] == 130
    assert result["period_b"]["resolved_period"]["label"] == "2024"


def test_compare_periods_rejects_both_for_one_side():
    result = tools.compare_periods(period_a="2023", period_a_start="2023-01-01", period_a_end="2023-12-31",
                                   period_b="2024")
    assert result["ok"] is False and "period_a_" in result["error"]
