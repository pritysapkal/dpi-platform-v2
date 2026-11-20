"""Tool tests, read-only, no Gemini API calls. They read the database named by DPIP_DB_PATH (dev.duckdb by default).

Two kinds of test (see conftest.py):
  any_data         checks a rule that holds on any data (guardrails, error dicts, "the tool equals a fresh recount")
  fixture_numbers  checks exact figures of the 50-PR fixture, taken from fixtures/build_fixture.py: facts() and
                   period_facts() work them out from the case table in plain Python, never from this code's output
"""

import time

import duckdb
import pytest

import build_fixture as fx  # the answer key; put on the path by conftest.py
from agent import db, tools

ANY = pytest.mark.any_data
FIXTURE = pytest.mark.fixture_numbers


def year(y: int, humans_only: bool = False) -> dict:
    return fx.period_facts(f"{y}-01-01", f"{y}-12-31", humans_only)


def merge_dates() -> list[str]:
    return sorted(p.merged[:10] for p in fx.CASES if p.merged)


# ---------- database-level defence (layer 3) ----------

@ANY
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


# A query whose work does not depend on how much data there is: 100,000^3 combinations is far more than any machine
# finishes, so only the timeout can end it. (It reads no table, so a small test dataset cannot make it finish early.)
UNFINISHABLE = "SELECT count(*) FROM range(100000) a, range(100000) b, range(100000) c WHERE a.range + b.range > c.range"


@ANY
def test_query_timeout_fires_when_the_limit_is_reached_and_not_before():
    started = time.monotonic()
    with pytest.raises(db.QueryTimeout):
        db.run_query(UNFINISHABLE, timeout_seconds=0.05)
    quick = time.monotonic() - started
    assert quick < 3, f"a 0.05 s limit took {quick:.2f} s to fire"

    started = time.monotonic()
    with pytest.raises(db.QueryTimeout):
        db.run_query(UNFINISHABLE, timeout_seconds=1.0)
    slower = time.monotonic() - started
    # the query really was still running when the limit hit: a 1 s limit lets it run about 1 s, not 0.05 s
    assert 0.9 <= slower < 5, f"a 1 s limit took {slower:.2f} s to fire"


@ANY
def test_a_query_that_finishes_in_time_is_not_cut_off():
    columns, rows, _ = db.run_query("SELECT 1 AS one", timeout_seconds=5)
    assert rows == [(1,)] or rows == [[1]]


# ---------- run_readonly_sql ----------

@ANY
def test_run_readonly_sql_returns_error_dict_instead_of_raising():
    result = tools.run_readonly_sql("DROP TABLE main.mart_dora_metrics_daily")
    assert result["ok"] is False
    assert "guardrail" in result["error"]


@ANY
def test_run_readonly_sql_blocks_non_whitelisted_table():
    result = tools.run_readonly_sql("SELECT * FROM raw.github_pull_requests")
    assert result["ok"] is False and "not allowed" in result["error"]


# Every PR paired with every PR: n x n rows, more than 200 on any dataset of 15 or more PRs.
MANY_ROWS = "SELECT a.pr_number FROM main.fct_pull_requests a, main.fct_pull_requests b"


@ANY
def test_run_readonly_sql_caps_rows_at_200():
    # first prove the query really produces more than 200 rows, so the cap is what limits the answer
    _, counted, _ = db.run_query(f"SELECT count(*) FROM ({MANY_ROWS})")
    available = counted[0][0]
    assert available > 200, f"the query only produces {available} rows here, so it cannot show the cap"

    result = tools.run_readonly_sql(MANY_ROWS)
    assert result["ok"] is True
    assert result["row_count"] == 200
    assert result["truncated"] is True


@FIXTURE
def test_the_row_count_before_the_cap_is_the_square_of_the_pr_count():
    _, counted, _ = db.run_query(f"SELECT count(*) FROM ({MANY_ROWS})")
    assert counted[0][0] == fx.facts()["prs"] ** 2 == 2500


@ANY
def test_run_readonly_sql_bad_column_returns_error_dict():
    result = tools.run_readonly_sql("SELECT no_such_column FROM main.int_pr_lifecycle")
    assert result["ok"] is False and "Database error" in result["error"]


# ---------- structured tools ----------

@ANY
def test_list_tables_only_whitelisted():
    names = {t["name"] for t in tools.list_tables()["tables"]}
    assert names == {"main.mart_dora_metrics_daily", "main.int_pr_lifecycle", "main.fct_pull_requests"}


@ANY
def test_every_listed_table_has_a_description_and_the_mart_warns_about_average_lead_time():
    tables = {t["name"]: t["description"] for t in tools.list_tables()["tables"]}
    assert all(tables.values())
    assert "Do NOT use avg_lead_time_hours" in tables["main.mart_dora_metrics_daily"]
    assert "PREFERRED" in tables["main.fct_pull_requests"]


@ANY
def test_get_schema_fact_table():
    result = tools.get_schema("main.fct_pull_requests")
    cols = {c["name"] for c in result["columns"]}
    assert {"pr_number", "is_bot", "lead_time_hours", "was_reviewed_before_merge", "data_as_of"} <= cols


@FIXTURE
def test_run_readonly_sql_on_fact_table_works_and_dimensions_are_blocked():
    facts = fx.facts()
    ok = tools.run_readonly_sql("select count(*), count(*) filter (where is_merged) from main.fct_pull_requests")
    assert ok["ok"] is True and ok["rows"] == [[facts["prs"], facts["merged"]]]  # [[50, 39]]
    blocked = tools.run_readonly_sql("select * from main.dim_contributors")
    assert blocked["ok"] is False and "not allowed" in blocked["error"]


@ANY
def test_get_schema_rejects_raw_table():
    result = tools.get_schema("raw.github_pull_requests")
    assert result["ok"] is False


@ANY
def test_get_schema_mart():
    cols = [c["name"] for c in tools.get_schema("mart_dora_metrics_daily")["columns"]]
    assert cols == ["metric_date", "deployment_frequency", "avg_lead_time_hours"]


@FIXTURE
def test_data_coverage():
    facts = fx.facts()
    result = tools.get_data_coverage()
    assert result["metrics_first_date"] == merge_dates()[0] == "2023-01-02"  # the first merge in the case table
    assert result["metrics_last_date"] == merge_dates()[-1] == "2025-11-03"  # the last merge
    assert result["pull_requests_total"] == facts["prs"]
    assert result["pull_requests_merged"] == facts["merged"]
    assert result["repos_available"] == ["encode/httpx"]


@FIXTURE
def test_full_range_matches_mart_totals():
    expected = fx.period_facts("2023-01-01", "2026-12-31")  # every merge in the case table
    result = tools.get_dora_metrics("2023-01-01", "2026-12-31")
    assert result["deployment_frequency"]["merged_prs_total"] == expected["merged"] == 39
    lead = result["lead_time_for_changes"]
    assert lead["merged_prs_measured"] == expected["merged"]  # mart and PR-grain table agree
    assert lead["median_hours"] == pytest.approx(expected["median"], abs=0.01)  # 12.0
    assert lead["p90_hours"] == pytest.approx(expected["p90"], abs=0.01)  # 129.6
    assert lead["mean_hours_pr_weighted"] == pytest.approx(expected["mean"], abs=0.01)  # 34.79
    # the average-of-daily-averages figure was removed and must not come back
    assert not any("day_weighted" in key or "dashboard" in key for key in lead)
    assert result["change_failure_rate"]["available"] is False
    assert "coverage_note" in result  # requested range is wider than the data


@FIXTURE
def test_period_with_no_data():
    result = tools.get_dora_metrics("2026-07-01", "2026-09-30")  # after the last merge, 2025-11-03
    assert result["ok"] is True and result["has_data"] is False
    assert merge_dates()[-1] in result["message"]


@ANY
def test_bad_dates_return_error_dict():
    assert tools.get_dora_metrics("2024-13-01", "2024-12-31")["ok"] is False
    assert tools.get_dora_metrics("2024-12-31", "2024-01-01")["ok"] is False


@ANY
def test_unknown_repo_returns_error_dict():
    result = tools.get_dora_metrics("2024-01-01", "2024-12-31", repo="facebook/react")
    assert result["ok"] is False and "encode/httpx" in result["error"]


@FIXTURE
def test_compare_periods_2023_vs_2024():
    result = tools.compare_periods("2023-01-01", "2023-12-31", "2024-01-01", "2024-12-31")
    assert result["period_a"]["deployment_frequency"]["merged_prs_total"] == year(2023)["merged"] == 16
    assert result["period_b"]["deployment_frequency"]["merged_prs_total"] == year(2024)["merged"] == 16
    assert result["percent_change_a_to_b"]["merged_prs_total"] == 0.0  # 16 to 16


@FIXTURE
def test_compare_periods_percent_change_for_unequal_years():
    result = tools.compare_periods(period_a="2025", period_b="2024")
    assert result["period_a"]["deployment_frequency"]["merged_prs_total"] == year(2025)["merged"] == 7
    assert result["period_b"]["deployment_frequency"]["merged_prs_total"] == 16
    assert result["percent_change_a_to_b"]["merged_prs_total"] == 128.6  # (16 - 7) / 7 = 1.2857...


# ---------- period phrases (resolved in code) ----------

@FIXTURE
def test_get_dora_metrics_with_period_phrase():
    result = tools.get_dora_metrics(period="2024")
    assert result["resolved_period"]["start_date"] == "2024-01-01"
    assert result["deployment_frequency"]["merged_prs_total"] == year(2024)["merged"] == 16


@ANY
def test_get_dora_metrics_rejects_period_and_dates_together():
    result = tools.get_dora_metrics(start_date="2024-01-01", end_date="2024-12-31", period="2024")
    assert result["ok"] is False and "not both" in result["error"]


@ANY
def test_get_dora_metrics_needs_dates_or_period():
    result = tools.get_dora_metrics()
    assert result["ok"] is False and "period" in result["error"]


@ANY
def test_get_dora_metrics_unrecognised_period():
    result = tools.get_dora_metrics(period="next sprint")
    assert result["ok"] is False and "Unrecognised period" in result["error"]


@FIXTURE
def test_compare_periods_with_phrases():
    result = tools.compare_periods(period_a="2023", period_b="2024")
    assert result["period_a"]["deployment_frequency"]["merged_prs_total"] == year(2023)["merged"] == 16
    assert result["period_b"]["resolved_period"]["label"] == "2024"


@ANY
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


@ANY
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


@FIXTURE
@pytest.mark.parametrize("humans_only", [False, True])
def test_2024_values_equal_the_case_table_answers(humans_only):
    # The case table's own answers (plain Python): all 16 merges of 2024 have median 12.0, p90 144.0, mean 39.81;
    # the 10 made by people have median 36.0, p90 168.0, mean 62.8.
    expected = year(2024, humans_only)
    result = tools.get_dora_metrics(period="2024", include_bots=not humans_only)
    lead = result["lead_time_for_changes"]
    assert result["deployment_frequency"]["merged_prs_total"] == expected["merged"] == (10 if humans_only else 16)
    assert lead["median_hours"] == pytest.approx(expected["median"], abs=0.01)
    assert lead["p90_hours"] == pytest.approx(expected["p90"], abs=0.01)
    assert lead["mean_hours_pr_weighted"] == pytest.approx(expected["mean"], abs=0.01)


@FIXTURE
def test_bot_filter_splits_the_total_and_reports_how_many_bot_prs_there_are():
    everyone = tools.get_dora_metrics("2024-01-01", "2024-12-31")["deployment_frequency"]
    humans = tools.get_dora_metrics("2024-01-01", "2024-12-31", include_bots=False)["deployment_frequency"]
    bots_in_2024 = year(2024)["bot_merged"]
    assert everyone["bot_merged_prs_in_period"] == humans["bot_merged_prs_in_period"] == bots_in_2024 == 6
    assert everyone["merged_prs_total"] - humans["merged_prs_total"] == bots_in_2024


@FIXTURE
def test_humans_only_headline_differs_from_all_prs():
    # Why the filter exists: fast bot merges pull the all-PR median down (2023: 18.0 hours with bots, 24.0 without).
    all_prs = tools.get_dora_metrics(period="2023")["lead_time_for_changes"]["median_hours"]
    humans = tools.get_dora_metrics(period="2023", include_bots=False)["lead_time_for_changes"]["median_hours"]
    assert all_prs == pytest.approx(year(2023)["median"], abs=0.01) == pytest.approx(18.0, abs=0.01)
    assert humans == pytest.approx(year(2023, True)["median"], abs=0.01) == pytest.approx(24.0, abs=0.01)


@FIXTURE
def test_period_after_the_last_merge_has_no_data_and_does_not_crash():
    quiet = tools.get_dora_metrics("2025-12-01", "2025-12-31")  # the last merge is 2025-11-03
    assert quiet["ok"] is True and quiet["has_data"] is False


@FIXTURE
def test_period_inside_the_data_with_zero_merges_gives_zero_and_no_lead_time():
    # 10-12 September 2024: inside the data (2023-01-02 to 2025-11-03) but September 2024 has no merges at all
    assert fx.period_facts("2024-09-10", "2024-09-12")["merged"] == 0
    result = tools.get_dora_metrics("2024-09-10", "2024-09-12")
    assert result["ok"] is True and result["has_data"] is True
    assert result["deployment_frequency"]["merged_prs_total"] == 0
    assert result["deployment_frequency"]["merged_prs_per_week"] == 0
    lead = result["lead_time_for_changes"]
    assert lead["merged_prs_measured"] == 0
    assert lead["median_hours"] is None and lead["p90_hours"] is None and lead["mean_hours_pr_weighted"] is None


@FIXTURE
def test_compare_periods_includes_p90_change_and_respects_bot_filter():
    result = tools.compare_periods(period_a="2023", period_b="2024", include_bots=False)
    assert result["period_a"]["includes_bot_prs"] is False
    assert result["period_a"]["deployment_frequency"]["merged_prs_total"] == year(2023, True)["merged"] == 12
    assert result["period_b"]["deployment_frequency"]["merged_prs_total"] == year(2024, True)["merged"] == 10
    assert result["period_a"]["lead_time_for_changes"]["p90_hours"] == pytest.approx(48.0, abs=0.01)
    assert result["period_b"]["lead_time_for_changes"]["p90_hours"] == pytest.approx(168.0, abs=0.01)
    assert result["percent_change_a_to_b"]["lead_time_p90_hours"] == 250.0  # (168 - 48) / 48 = 2.5


@ANY
def test_include_bots_must_be_a_boolean():
    from agent.agent import execute_tool
    result = execute_tool("get_dora_metrics", {"period": "2024", "include_bots": "yes"}, tools.TOOLS)
    assert result["ok"] is False and "include_bots" in result["error"]


@FIXTURE
def test_data_coverage_reports_data_as_of_and_bot_count():
    result = tools.get_data_coverage()
    assert result["data_as_of"].startswith("2026-01-15T12:00")  # the fixture's pretend fetch time
    assert result["bot_pull_requests"] == fx.facts()["bot_prs"] == 14
    assert "not today" in result["data_as_of_note"]
