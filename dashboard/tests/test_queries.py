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
