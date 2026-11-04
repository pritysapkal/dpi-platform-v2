"""Dashboard numbers, checked against an independent calculation from the RAW tables.

The expected values come from plain SQL on raw.github_pull_requests / raw.github_pr_reviews
(no dbt models, no dashboard code), so a mistake in the models or in queries.py can't hide.
Needs dev.duckdb. Run from the project root:  python -m pytest dashboard/tests -v
"""

import datetime

import duckdb
import pytest

import queries

pytestmark = pytest.mark.skipif(not queries.DB_PATH.exists(), reason="dev.duckdb not built")

FULL = (datetime.date(2023, 1, 1), datetime.date(2026, 11, 1))
Y2024 = (datetime.date(2024, 1, 1), datetime.date(2024, 12, 31))
Y2023 = (datetime.date(2023, 1, 1), datetime.date(2023, 12, 31))

RAW_SQL = """
with first_review as (
    select pr_number, min(cast(submitted_at as timestamp)) as first_at
    from raw.github_pr_reviews group by 1
),
merged as (
    select
        (epoch(cast(p.merged_at as timestamp)) - epoch(cast(p.created_at as timestamp))) / 3600.0 as lead_h,
        (epoch(r.first_at) - epoch(cast(p.created_at as timestamp))) / 3600.0 as wait_h,
        r.first_at is not null and r.first_at <= cast(p.merged_at as timestamp) as reviewed_before_merge
    from raw.github_pull_requests p
    left join first_review r on r.pr_number = p.number
    where p.merged_at is not null
      and cast(cast(p.merged_at as timestamp) as date) between ? and ?
      and (? or p.author not like '%[bot]')
)
select count(*), median(lead_h), quantile_cont(lead_h, 0.9),
       count(*) filter (where reviewed_before_merge),
       median(wait_h) filter (where reviewed_before_merge)
from merged
"""


def raw_expected(start, end, include_bots):
    con = duckdb.connect(str(queries.DB_PATH), read_only=True)
    try:
        return con.execute(RAW_SQL, [start, end, include_bots]).fetchone()
    finally:
        con.close()


@pytest.mark.parametrize("include_bots", [False, True])
@pytest.mark.parametrize("window", [FULL, Y2024, Y2023], ids=["full", "2024", "2023"])
def test_kpis_match_independent_calculation_from_raw_tables(window, include_bots):
    n, median_lead, p90_lead, reviewed, median_wait = raw_expected(*window, include_bots)
    k = queries.kpis(*window, include_bots)
    assert k["merged_prs"] == n
    # the dbt model rounds each PR's hours to 2 decimals, hence the small tolerance
    assert k["median_lead_time_hours"] == pytest.approx(median_lead, abs=0.01)
    assert k["p90_lead_time_hours"] == pytest.approx(p90_lead, abs=0.01)
    assert k["reviewed_before_merge_prs"] == reviewed
    assert k["review_coverage_pct"] == pytest.approx(100.0 * reviewed / n)
    assert k["median_hours_to_first_review"] == pytest.approx(median_wait, abs=0.01)


def test_known_2024_values_equal_the_step_2_acceptance_check():
    everyone = queries.kpis(*Y2024, True)
    assert everyone["merged_prs"] == 109
    assert round(everyone["median_lead_time_hours"], 2) == 23.91
    assert round(everyone["p90_lead_time_hours"], 2) == 495.83
    humans = queries.kpis(*Y2024, False)
    assert humans["merged_prs"] == 92
    assert round(humans["median_lead_time_hours"], 2) == 23.18
    assert round(humans["p90_lead_time_hours"], 2) == 697.91


def test_full_range_review_coverage_counts_only_reviews_before_the_merge():
    k = queries.kpis(*FULL, True)
    # 288 merged; 23 had no review and 1 was only "reviewed" after merging, so 264 count
    assert (k["merged_prs"], k["reviewed_before_merge_prs"]) == (288, 264)
    assert k["review_coverage_pct"] == pytest.approx(91.667, abs=0.001)


def test_bot_toggle_removes_exactly_the_bot_merges():
    assert queries.kpis(*FULL, True)["merged_prs"] - queries.kpis(*FULL, False)["merged_prs"] == 81


def test_date_range_is_inclusive_on_both_ends():
    day, expected = queries.run(
        "select cast(merged_at as date), count(*) from main.fct_pull_requests where is_merged "
        "group by 1 having count(*) >= 2 order by 1 limit 1"
    )[0]
    assert queries.kpis(day, day, True)["merged_prs"] == expected


def test_empty_range_gives_none_not_zero_for_averages():
    k = queries.kpis(datetime.date(2025, 12, 1), datetime.date(2025, 12, 3), True)  # no merges
    assert k["merged_prs"] == 0
    assert k["median_lead_time_hours"] is None and k["p90_lead_time_hours"] is None
    assert k["review_coverage_pct"] is None and k["median_hours_to_first_review"] is None


def test_data_window_comes_from_the_fact_table():
    first, last, as_of = queries.data_window()
    assert first == datetime.date(2023, 1, 1)
    assert last == as_of.date() == datetime.date(2026, 11, 1)


# ---------- trend (weekly / monthly) ----------

RAW_TREND_SQL = """
select cast(date_trunc('{unit}', cast(merged_at as timestamp)) as date) as period_start,
       count(*),
       median((epoch(cast(merged_at as timestamp)) - epoch(cast(created_at as timestamp))) / 3600.0),
       quantile_cont((epoch(cast(merged_at as timestamp)) - epoch(cast(created_at as timestamp))) / 3600.0, 0.9)
from raw.github_pull_requests
where merged_at is not null
  and cast(cast(merged_at as timestamp) as date) between ? and ?
  and (? or author not like '%[bot]')
group by 1
"""


def raw_trend(unit, start, end, include_bots):
    con = duckdb.connect(str(queries.DB_PATH), read_only=True)
    try:
        rows = con.execute(RAW_TREND_SQL.format(unit=unit), [start, end, include_bots]).fetchall()
    finally:
        con.close()
    return {r[0]: r[1:] for r in rows}


@pytest.mark.parametrize("include_bots", [False, True])
@pytest.mark.parametrize("grain", ["month", "week"])
@pytest.mark.parametrize("window", [FULL, Y2024], ids=["full", "2024"])
def test_trend_matches_independent_calculation_from_raw_tables(window, grain, include_bots):
    expected = raw_trend(grain, *window, include_bots)
    df = queries.trend(*window, include_bots, grain)
    got = {row.period_start.date(): row for row in df.itertuples()}
    # every period that has merges is present and right
    for period, (n, median_h, p90_h) in expected.items():
        assert got[period].merged_prs == n
        assert got[period].median_lead_time_hours == pytest.approx(median_h, abs=0.01)
        assert got[period].p90_lead_time_hours == pytest.approx(p90_h, abs=0.01)
    # and every other period is present as zero merges with no lead time, not missing
    for period, row in got.items():
        if period not in expected:
            assert row.merged_prs == 0
            assert row.median_lead_time_hours != row.median_lead_time_hours  # NaN: no value, not 0


@pytest.mark.parametrize("grain", ["month", "week"])
@pytest.mark.parametrize("include_bots", [False, True])
def test_trend_totals_add_up_to_the_kpi_total(grain, include_bots):
    for window in (FULL, Y2024, Y2023):
        assert queries.trend(*window, include_bots, grain)["merged_prs"].sum() == queries.kpis(*window, include_bots)["merged_prs"]


def test_trend_periods_start_on_the_right_day_and_have_no_gaps():
    months = queries.trend(*Y2024, True, "month")
    assert len(months) == 12 and all(d.day == 1 for d in months["period_start"])
    weeks = queries.trend(*Y2024, True, "week")
    assert all(d.weekday() == 0 for d in weeks["period_start"])  # Monday
    gaps = weeks["period_start"].diff().dropna().dt.days
    assert set(gaps) == {7}


def test_trend_2024_monthly_counts_match_the_raw_tables_month_by_month():
    expected = raw_trend("month", *Y2024, True)
    df = queries.trend(*Y2024, True, "month")
    assert df["merged_prs"].tolist() == [expected.get(d.date(), (0,))[0] for d in df["period_start"]]
    assert int(df["merged_prs"].sum()) == 109
    assert int(queries.trend(*Y2024, False, "month")["merged_prs"].sum()) == 92


def test_trend_rejects_an_unknown_grain():
    with pytest.raises(ValueError, match="grain must be one of"):
        queries.trend(*Y2024, True, "day; drop table x")
