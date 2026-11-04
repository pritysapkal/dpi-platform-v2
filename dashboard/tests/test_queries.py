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


# ---------- lead-time breakdown: waiting for first review vs in review ----------

RAW_BREAKDOWN_SQL = """
with first_review as (
    select pr_number, min(cast(submitted_at as timestamp)) as first_at
    from raw.github_pr_reviews group by 1
),
prs as (
    select
        cast(p.created_at as timestamp) as opened,
        cast(p.merged_at as timestamp) as merged,
        r.first_at,
        p.author like '%[bot]' as is_bot
    from raw.github_pull_requests p
    left join first_review r on r.pr_number = p.number
    where p.merged_at is not null
      and cast(cast(p.merged_at as timestamp) as date) between ? and ?
),
split as (
    select
        (epoch(first_at) - epoch(opened)) / 3600.0 as wait_h,
        (epoch(merged) - epoch(first_at)) / 3600.0 as review_h,
        (epoch(merged) - epoch(opened)) / 3600.0 as lead_h
    from prs
    where not is_bot and first_at is not null and first_at <= merged
)
select count(*), median(wait_h), quantile_cont(wait_h, 0.9), median(review_h), quantile_cont(review_h, 0.9),
       median(lead_h), quantile_cont(lead_h, 0.9),
       100.0 * sum(wait_h) / (sum(wait_h) + sum(review_h)),
       (select count(*) from prs where not is_bot and (first_at is null or first_at > merged))
from split
"""


def raw_breakdown(start, end):
    con = duckdb.connect(str(queries.DB_PATH), read_only=True)
    try:
        return con.execute(RAW_BREAKDOWN_SQL, [start, end]).fetchone()
    finally:
        con.close()


@pytest.mark.parametrize("window", [FULL, Y2023, Y2024, (datetime.date(2025, 1, 1), datetime.date(2025, 12, 31))],
                         ids=["full", "2023", "2024", "2025"])
def test_breakdown_matches_independent_calculation_from_raw_tables(window):
    n, wait_med, wait_p90, rev_med, rev_p90, lead_med, lead_p90, wait_share, not_split = raw_breakdown(*window)
    b = queries.lead_time_breakdown(*window)
    assert b["split_prs"] == n and b["not_split_prs"] == not_split
    assert b["median_wait_hours"] == pytest.approx(wait_med, abs=0.01)
    assert b["p90_wait_hours"] == pytest.approx(wait_p90, abs=0.02)
    assert b["median_in_review_hours"] == pytest.approx(rev_med, abs=0.01)
    assert b["p90_in_review_hours"] == pytest.approx(rev_p90, abs=0.02)
    assert b["median_lead_time_hours"] == pytest.approx(lead_med, abs=0.01)
    assert b["p90_lead_time_hours"] == pytest.approx(lead_p90, abs=0.02)
    assert b["wait_share_pct"] == pytest.approx(wait_share, abs=0.05)


def test_breakdown_shares_add_to_100_and_stages_add_up_to_lead_time():
    b = queries.lead_time_breakdown(*FULL)
    assert b["wait_share_pct"] + b["in_review_share_pct"] == pytest.approx(100.0)
    # for every reviewed-before-merge PR, wait + in review = lead time (to the model's 2-decimal rounding)
    worst = queries.run(
        "select max(abs(review_wait_hours + time_in_review_hours - lead_time_hours)) "
        "from main.fct_pull_requests where is_merged and was_reviewed_before_merge"
    )[0][0]
    assert worst <= 0.011


def test_breakdown_counts_every_human_merge_exactly_once():
    b = queries.lead_time_breakdown(*FULL)
    humans = queries.kpis(*FULL, False)["merged_prs"]
    assert b["split_prs"] + b["not_split_prs"] == humans == 207


def test_breakdown_is_human_only_known_full_range_values():
    b = queries.lead_time_breakdown(*FULL)
    assert (b["split_prs"], b["not_split_prs"]) == (183, 24)
    assert round(b["median_wait_hours"], 2) == 6.88 and round(b["median_in_review_hours"], 2) == 0.56
    assert round(b["wait_share_pct"], 1) == 55.9


def test_breakdown_of_an_empty_range_has_no_values_not_zeros():
    b = queries.lead_time_breakdown(datetime.date(2025, 12, 1), datetime.date(2025, 12, 3))
    assert b["split_prs"] == 0 and b["not_split_prs"] == 0
    assert b["median_wait_hours"] is None and b["wait_share_pct"] is None


# ---------- review funnel ----------

RAW_FUNNEL_SQL = """
with prs as (
    select
        p.number,
        cast(p.merged_at as timestamp) as merged,
        p.merged_at is not null as is_merged,
        p.state,
        exists (select 1 from raw.github_pr_reviews r where r.pr_number = p.number
                and (p.merged_at is null or cast(r.submitted_at as timestamp) <= cast(p.merged_at as timestamp))) as reviewed,
        exists (select 1 from raw.github_pr_reviews r where r.pr_number = p.number and r.review_state = 'APPROVED') as approved
    from raw.github_pull_requests p
    where cast(cast(p.created_at as timestamp) as date) between ? and ?
      and (? or p.author not like '%[bot]')
)
select count(*), count(*) filter (where reviewed), count(*) filter (where approved), count(*) filter (where is_merged),
       count(*) filter (where is_merged and not reviewed), count(*) filter (where is_merged and not approved),
       count(*) filter (where not is_merged and state = 'closed'), count(*) filter (where state = 'open')
from prs
"""


@pytest.mark.parametrize("include_bots", [False, True])
@pytest.mark.parametrize("window", [FULL, Y2023, Y2024], ids=["full", "2023", "2024"])
def test_funnel_matches_independent_calculation_from_raw_tables(window, include_bots):
    con = duckdb.connect(str(queries.DB_PATH), read_only=True)
    try:
        expected = con.execute(RAW_FUNNEL_SQL, [*window, include_bots]).fetchone()
    finally:
        con.close()
    f = queries.review_funnel(*window, include_bots)
    assert (f["opened"], f["reviewed"], f["approved"], f["merged"], f["merged_without_review"],
            f["merged_without_approval"], f["closed_without_merge"], f["still_open"]) == tuple(expected)


@pytest.mark.parametrize("include_bots", [False, True])
def test_funnel_stages_are_consistent(include_bots):
    f = queries.review_funnel(*FULL, include_bots)
    assert f["opened"] == f["merged"] + f["closed_without_merge"] + f["still_open"]  # every PR is in exactly one end state
    assert f["approved"] <= f["reviewed"] <= f["opened"]
    assert f["merged_without_review"] <= f["merged_without_approval"] <= f["merged"]


def test_funnel_known_values_and_bot_effect():
    everyone = queries.review_funnel(*FULL, True)
    assert (everyone["opened"], everyone["merged"]) == (523, 288)
    humans = queries.review_funnel(*FULL, False)
    assert (humans["opened"], humans["reviewed"], humans["approved"], humans["merged"]) == (419, 255, 201, 207)
    assert everyone["opened"] - humans["opened"] == 104  # the bot PRs


def test_funnel_of_a_range_with_no_opened_prs_is_all_zero():
    # no PR was opened between 2026-03-30 and 2026-09-30 (the repo went quiet)
    f = queries.review_funnel(datetime.date(2026, 3, 30), datetime.date(2026, 9, 30), True)
    assert set(f.values()) == {0}


# ---------- open-PR age ----------

RAW_OPEN_SQL = """
select
    p.number,
    (epoch(cast(p._loaded_at as timestamp)) - epoch(cast(p.created_at as timestamp))) / 86400.0 as age_days,
    not exists (select 1 from raw.github_pr_reviews r where r.pr_number = p.number) as never_reviewed
from raw.github_pull_requests p
where p.state = 'open'
  and cast(cast(p.created_at as timestamp) as date) between ? and ?
  and (? or p.author not like '%[bot]')
order by age_days desc, p.number
"""


def raw_open(start, end, include_bots):
    con = duckdb.connect(str(queries.DB_PATH), read_only=True)
    try:
        return con.execute(RAW_OPEN_SQL, [start, end, include_bots]).fetchall()
    finally:
        con.close()


def bucket_counts(ages):
    edges = [7, 30, 90, 180, 365]
    counts = [0] * 6
    for a in ages:
        counts[next((i for i, e in enumerate(edges) if a < e), 5)] += 1
    return counts


@pytest.mark.parametrize("include_bots", [False, True])
@pytest.mark.parametrize("window", [FULL, Y2024, (datetime.date(2026, 1, 1), datetime.date(2026, 11, 1))],
                         ids=["full", "2024", "2026"])
def test_open_pr_age_matches_independent_calculation_from_raw_tables(window, include_bots):
    import statistics
    rows = raw_open(*window, include_bots)
    ages = [r[1] for r in rows]
    o = queries.open_pr_age(*window, include_bots)
    assert o["open_prs"] == len(rows)
    assert o["median_age_days"] == pytest.approx(statistics.median(ages), abs=0.001)
    assert o["oldest_age_days"] == pytest.approx(max(ages), abs=0.001)
    assert o["never_reviewed"] == sum(r[2] for r in rows)
    assert [n for _, n in o["age_buckets"]] == bucket_counts(ages)
    assert o["oldest"]["pr_number"].tolist() == [r[0] for r in rows[:10]]


def test_open_pr_age_is_measured_from_data_as_of_not_from_today():
    import inspect
    source = inspect.getsource(queries.open_pr_age).lower()
    assert not any(clock in source for clock in ("now()", "current_date", "current_timestamp", "date.today", "datetime.now"))
    # ...and the ages equal "pull date minus opened date" exactly, which a today-based figure would not
    o = queries.open_pr_age(*FULL, True)
    pulled = queries.data_window()[2]
    first = o["oldest"].iloc[0]
    opened = queries.run("select opened_at from main.fct_pull_requests where pr_number = ?", [int(first["pr_number"])])[0][0]
    assert first["age_days"] == pytest.approx((pulled - opened).total_seconds() / 86400.0, abs=1e-4)  # whole-second resolution


def test_open_pr_age_known_values_and_every_bucket_is_present():
    o = queries.open_pr_age(*FULL, True)
    assert (o["open_prs"], o["never_reviewed"]) == (76, 47)
    assert round(o["median_age_days"], 1) == 369.0 and round(o["oldest_age_days"], 1) == 1199.1
    assert [label for label, _ in o["age_buckets"]] == [b[0] for b in queries.AGE_BUCKETS]
    assert sum(n for _, n in o["age_buckets"]) == 76
    humans = queries.open_pr_age(*FULL, False)
    assert humans["open_prs"] == 73


def test_open_pr_age_with_no_open_prs_is_empty_not_an_error():
    o = queries.open_pr_age(datetime.date(2026, 3, 30), datetime.date(2026, 9, 30), True)
    assert o["open_prs"] == 0 and o["median_age_days"] is None and o["oldest_age_days"] is None
    assert o["never_reviewed"] == 0 and len(o["oldest"]) == 0
    assert all(n == 0 for _, n in o["age_buckets"])
