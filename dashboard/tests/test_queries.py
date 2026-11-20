"""Dashboard numbers, checked two independent ways.

any_data tests recompute each figure from the RAW tables with plain SQL (no dbt models, no dashboard code), so a mistake
in the models or in queries.py can't hide; they hold on any data. fixture_numbers tests pin exact figures of the
50-PR fixture, worked out from the case table in plain Python (the helpers below and fixtures/build_fixture.py), never
copied from what the dashboard code prints.
Run from the project root:  DPIP_DB_PATH=fixture.duckdb python -m pytest dashboard/tests -v
"""

import datetime
import statistics

import duckdb
import pytest

import build_fixture as fx  # the answer key; put on the path by conftest.py
import queries
from answer_key import (  # the case table worked out in plain Python (dashboard/tests/answer_key.py)
    activity_expected, approved_before_merge, at, breakdown_expected, first_approval, funnel_expected, people,
    reviewed_before_merge, reviewers_expected,
)

FULL = (datetime.date(2023, 1, 1), datetime.date(2026, 11, 1))  # wider than any dataset we hold
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


@pytest.mark.any_data
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


@pytest.mark.fixture_numbers
def test_known_2024_values_equal_the_case_table_answers():
    everyone, humans = queries.kpis(*Y2024, True), queries.kpis(*Y2024, False)
    all_2024, people_2024 = fx.period_facts("2024-01-01", "2024-12-31"), fx.period_facts("2024-01-01", "2024-12-31", humans_only=True)
    assert (everyone["merged_prs"], humans["merged_prs"]) == (all_2024["merged"], people_2024["merged"]) == (16, 10)
    for got, want in ((everyone, all_2024), (humans, people_2024)):
        assert got["median_lead_time_hours"] == pytest.approx(want["median"], abs=0.01)
        assert got["p90_lead_time_hours"] == pytest.approx(want["p90"], abs=0.01)
    assert (round(everyone["median_lead_time_hours"], 2), round(everyone["p90_lead_time_hours"], 2)) == (12.0, 144.0)
    assert (round(humans["median_lead_time_hours"], 2), round(humans["p90_lead_time_hours"], 2)) == (36.0, 168.0)


@pytest.mark.fixture_numbers
def test_full_range_review_coverage_counts_only_reviews_before_the_merge():
    k = queries.kpis(*FULL, True)
    merged = [p for p in fx.CASES if p.merged]
    assert all(not p.reviews for p in merged if fx.is_bot(p.author))  # no bot PR is reviewed, so only people's PRs can count
    covered = sum(1 for p in merged if reviewed_before_merge(p))
    # 39 merged; 12 bot merges and 5 people's merges (1008, 1010, 1011, 1029, 1045) have no review before the merge: 39 - 17 = 22
    assert (k["merged_prs"], k["reviewed_before_merge_prs"]) == (len(merged), covered) == (fx.facts()["merged"], fx.facts()["human_merged_reviewed_before_merge"]) == (39, 22)
    assert k["review_coverage_pct"] == pytest.approx(100.0 * 22 / 39, abs=0.001)  # 56.410
    # 1010 (review one second after the merge) and 1011 (two days after) are not covered; 1009 (review at the merge instant) is
    by_number = {p.number: p for p in fx.CASES}
    assert not reviewed_before_merge(by_number[1010]) and not reviewed_before_merge(by_number[1011]) and reviewed_before_merge(by_number[1009])


@pytest.mark.fixture_numbers
def test_bot_toggle_removes_exactly_the_bot_merges():
    assert queries.kpis(*FULL, True)["merged_prs"] - queries.kpis(*FULL, False)["merged_prs"] == fx.facts()["bot_merged"] == 12


@pytest.mark.any_data
def test_date_range_is_inclusive_on_both_ends():
    day, expected = queries.run(
        "select cast(merged_at as date), count(*) from main.fct_pull_requests where is_merged "
        "group by 1 having count(*) >= 2 order by 1 limit 1"
    )[0]
    assert queries.kpis(day, day, True)["merged_prs"] == expected


@pytest.mark.any_data
def test_empty_range_gives_none_not_zero_for_averages():
    k = queries.kpis(datetime.date(2025, 12, 1), datetime.date(2025, 12, 3), True)  # no merges
    assert k["merged_prs"] == 0
    assert k["median_lead_time_hours"] is None and k["p90_lead_time_hours"] is None
    assert k["review_coverage_pct"] is None and k["median_hours_to_first_review"] is None


@pytest.mark.fixture_numbers
def test_data_window_comes_from_the_fact_table():
    first, last, as_of = queries.data_window()
    assert first == min(at(p.created).date() for p in fx.CASES) == datetime.date(2023, 1, 1)  # the first PR was opened on 2023-01-01
    assert last == as_of.date() == fx.PULLED_AT.date() == datetime.date(2026, 1, 15)  # the fixture's pretend fetch day


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


@pytest.mark.any_data
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


@pytest.mark.any_data
@pytest.mark.parametrize("grain", ["month", "week"])
@pytest.mark.parametrize("include_bots", [False, True])
def test_trend_totals_add_up_to_the_kpi_total(grain, include_bots):
    for window in (FULL, Y2024, Y2023):
        assert queries.trend(*window, include_bots, grain)["merged_prs"].sum() == queries.kpis(*window, include_bots)["merged_prs"]


@pytest.mark.any_data
def test_trend_periods_start_on_the_right_day_and_have_no_gaps():
    months = queries.trend(*Y2024, True, "month")
    assert len(months) == 12 and all(d.day == 1 for d in months["period_start"])
    weeks = queries.trend(*Y2024, True, "week")
    assert all(d.weekday() == 0 for d in weeks["period_start"])  # Monday
    gaps = weeks["period_start"].diff().dropna().dt.days
    assert set(gaps) == {7}


@pytest.mark.fixture_numbers
def test_trend_2024_monthly_counts_match_the_case_table_month_by_month():
    def per_month(include_bots):
        return [sum(1 for p in people(include_bots) if p.merged and p.merged.startswith(f"2024-{m:02d}")) for m in range(1, 13)]

    everyone, humans = queries.trend(*Y2024, True, "month"), queries.trend(*Y2024, False, "month")
    assert everyone["merged_prs"].tolist() == per_month(True) and humans["merged_prs"].tolist() == per_month(False)
    assert (int(everyone["merged_prs"].sum()), int(humans["merged_prs"].sum())) == (16, 10)
    assert everyone["merged_prs"][8] == 0 and everyone["merged_prs"][11] == 0  # September and December: nothing merged
    assert humans["merged_prs"][7] == 0 < everyone["merged_prs"][7]  # August: only bots merge


@pytest.mark.any_data
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


@pytest.mark.any_data
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


@pytest.mark.any_data
def test_breakdown_shares_add_to_100_and_stages_add_up_to_lead_time():
    b = queries.lead_time_breakdown(*FULL)
    assert b["wait_share_pct"] + b["in_review_share_pct"] == pytest.approx(100.0)
    # for every reviewed-before-merge PR, wait + in review = lead time (to the model's 2-decimal rounding)
    worst = queries.run(
        "select max(abs(review_wait_hours + time_in_review_hours - lead_time_hours)) "
        "from main.fct_pull_requests where is_merged and was_reviewed_before_merge"
    )[0][0]
    assert worst <= 0.011


@pytest.mark.fixture_numbers
def test_breakdown_counts_every_human_merge_exactly_once():
    b = queries.lead_time_breakdown(*FULL)
    humans = queries.kpis(*FULL, False)["merged_prs"]
    assert b["split_prs"] + b["not_split_prs"] == humans == fx.facts()["human_merged"] == 27


@pytest.mark.fixture_numbers
def test_breakdown_is_human_only_known_full_range_values():
    want, b = breakdown_expected(), queries.lead_time_breakdown(*FULL)
    facts = fx.facts()
    assert (b["split_prs"], b["not_split_prs"]) == (want["split"], want["not_split"]) == (facts["human_merged_reviewed_before_merge"], facts["human_merged_not_reviewed_before_merge"]) == (22, 5)
    assert b["median_wait_hours"] == pytest.approx(want["median_wait"], abs=0.01)
    assert b["median_in_review_hours"] == pytest.approx(want["median_review"], abs=0.01)
    assert b["wait_share_pct"] == pytest.approx(want["wait_share"], abs=0.05)


@pytest.mark.any_data
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
        -- "approved" follows the same rule as "reviewed": for a merged PR the approval must be at or before the merge
        exists (select 1 from raw.github_pr_reviews r where r.pr_number = p.number and r.review_state = 'APPROVED'
                and (p.merged_at is null or cast(r.submitted_at as timestamp) <= cast(p.merged_at as timestamp))) as approved
    from raw.github_pull_requests p
    where cast(cast(p.created_at as timestamp) as date) between ? and ?
      and (? or p.author not like '%[bot]')
)
select count(*), count(*) filter (where reviewed), count(*) filter (where approved), count(*) filter (where is_merged),
       count(*) filter (where is_merged and not reviewed), count(*) filter (where is_merged and not approved),
       count(*) filter (where not is_merged and state = 'closed'), count(*) filter (where state = 'open')
from prs
"""


@pytest.mark.any_data
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


@pytest.mark.any_data
@pytest.mark.parametrize("include_bots", [False, True])
def test_funnel_stages_are_consistent(include_bots):
    f = queries.review_funnel(*FULL, include_bots)
    assert f["opened"] == f["merged"] + f["closed_without_merge"] + f["still_open"]  # every PR is in exactly one end state
    assert f["approved"] <= f["reviewed"] <= f["opened"]
    assert f["merged_without_review"] <= f["merged_without_approval"] <= f["merged"]


@pytest.mark.fixture_numbers
def test_funnel_known_values_and_bot_effect():
    facts = fx.facts()
    everyone, humans = queries.review_funnel(*FULL, True), queries.review_funnel(*FULL, False)
    assert everyone == funnel_expected(True) and humans == funnel_expected(False)
    assert (everyone["opened"], everyone["merged"], everyone["closed_without_merge"], everyone["still_open"]) == (facts["prs"], facts["merged"], facts["closed_without_merge"], facts["open"]) == (50, 39, 5, 6)
    assert everyone["opened"] - humans["opened"] == facts["bot_prs"] == 14  # the bot PRs
    assert humans["merged"] == facts["human_merged"] == 27 and humans["merged_without_review"] == facts["human_merged_not_reviewed_before_merge"] == 5


@pytest.mark.fixture_numbers
def test_funnel_approval_means_before_the_merge_so_a_late_approval_is_not_an_approval():
    # PR 1010 is approved one second after its merge and PR 1011 two days after: merged, never approved before the merge.
    by_number = {p.number: p for p in fx.CASES}
    assert all(by_number[n].merged and not approved_before_merge(by_number[n]) and first_approval(by_number[n]) for n in (1010, 1011))
    f = queries.review_funnel(*FULL, True)
    any_time = queries.run("select count(*) from main.fct_pull_requests where is_merged and not was_approved")[0][0]
    assert f["merged_without_approval"] == any_time + 2 == 17  # the two late approvals count as not approved
    assert f["merged_without_review"] == 17 <= f["merged_without_approval"]  # the invariant that used to break
    assert f["approved"] == 23  # 25 PRs approved at some time, minus the 2 approved only after their merge


@pytest.mark.any_data
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


@pytest.mark.any_data
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


@pytest.mark.any_data
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


@pytest.mark.fixture_numbers
def test_open_pr_age_known_values_and_every_bucket_is_present():
    def ages(include_bots):
        return [((fx.PULLED_AT - at(p.created)).total_seconds() / 86400.0, p) for p in people(include_bots) if p.state == "open"]

    both, people_only = ages(True), ages(False)
    o = queries.open_pr_age(*FULL, True)
    assert (o["open_prs"], o["never_reviewed"]) == (len(both), sum(1 for _, p in both if not p.reviews)) == (fx.facts()["open"], fx.facts()["open_never_reviewed"]) == (6, 3)
    assert o["median_age_days"] == pytest.approx(statistics.median(a for a, _ in both), abs=0.001)
    assert o["oldest_age_days"] == pytest.approx(max(a for a, _ in both), abs=0.001)
    assert o["oldest"]["pr_number"].iloc[0] == 1012  # the ancient open PR, created 2023-07 and never reviewed
    assert [label for label, _ in o["age_buckets"]] == [b[0] for b in queries.AGE_BUCKETS]
    assert [n for _, n in o["age_buckets"]] == bucket_counts([a for a, _ in both]) and sum(n for _, n in o["age_buckets"]) == 6
    assert queries.open_pr_age(*FULL, False)["open_prs"] == len(people_only)


@pytest.mark.any_data
def test_open_pr_age_with_no_open_prs_is_empty_not_an_error():
    o = queries.open_pr_age(datetime.date(2026, 3, 30), datetime.date(2026, 9, 30), True)
    assert o["open_prs"] == 0 and o["median_age_days"] is None and o["oldest_age_days"] is None
    assert o["never_reviewed"] == 0 and len(o["oldest"]) == 0
    assert all(n == 0 for _, n in o["age_buckets"])


# ---------- top reviewers and authors ----------

QUIET = (datetime.date(2026, 3, 30), datetime.date(2026, 8, 28))  # no reviews and no PRs opened

RAW_REVIEWERS_SQL = """
select r.reviewer_login, count(distinct r.pr_number), count(*), count(*) filter (where r.review_state = 'APPROVED')
from raw.github_pr_reviews r
join raw.github_pull_requests p on p.number = r.pr_number
where r.reviewer_login not like '%[bot]'
  and r.reviewer_login <> p.author
  and cast(cast(r.submitted_at as timestamp) as date) between ? and ?
group by 1
order by 2 desc, 3 desc, 1
limit 10
"""

RAW_OWN_COMMENTS_SQL = """
select count(*)
from raw.github_pr_reviews r
join raw.github_pull_requests p on p.number = r.pr_number
where r.reviewer_login not like '%[bot]' and r.reviewer_login = p.author
  and cast(cast(r.submitted_at as timestamp) as date) between ? and ?
"""

RAW_AUTHORS_SQL = """
select author, count(*), count(*) filter (where merged_at is not null), count(*) filter (where state = 'open')
from raw.github_pull_requests
where author not like '%[bot]' and cast(cast(created_at as timestamp) as date) between ? and ?
group by 1
order by 2 desc, 3 desc, 1
limit 10
"""


def raw_rows(sql, window):
    con = duckdb.connect(str(queries.DB_PATH), read_only=True)
    try:
        return [tuple(r) for r in con.execute(sql, list(window)).fetchall()]
    finally:
        con.close()


@pytest.mark.any_data
@pytest.mark.parametrize("window", [FULL, Y2023, Y2024], ids=["full", "2023", "2024"])
def test_top_reviewers_match_independent_calculation_from_raw_tables(window):
    got = queries.top_reviewers(*window)
    assert [tuple(r) for r in got["reviewers"].itertuples(index=False)] == raw_rows(RAW_REVIEWERS_SQL, window)
    assert got["own_pr_comments_left_out"] == raw_rows(RAW_OWN_COMMENTS_SQL, window)[0][0]


@pytest.mark.any_data
@pytest.mark.parametrize("window", [FULL, Y2023, Y2024], ids=["full", "2023", "2024"])
def test_top_authors_match_independent_calculation_from_raw_tables(window):
    got = queries.top_authors(*window)
    assert [tuple(r) for r in got.itertuples(index=False)] == raw_rows(RAW_AUTHORS_SQL, window)


@pytest.mark.any_data
def test_reviewers_and_authors_are_people_only():
    names = set(queries.top_reviewers(*FULL, limit=500)["reviewers"]["reviewer"]) | set(queries.top_authors(*FULL, limit=500)["author"])
    assert not any(n.endswith("[bot]") for n in names)


@pytest.mark.fixture_numbers
def test_self_comments_are_left_out_and_reviews_are_ranked_by_distinct_prs():
    got = queries.top_reviewers(*FULL)
    rows, own = reviewers_expected(FULL)
    assert [tuple(r) for r in got["reviewers"].itertuples(index=False)] == rows
    assert got["own_pr_comments_left_out"] == own == 1  # erin's comment on her own PR 1016
    top = got["reviewers"].set_index("reviewer")
    # grace: 7 reviews but only 6 different PRs (two reviews on 1014); the table counts PRs
    assert (top.loc["grace", "prs_reviewed"], top.loc["grace", "reviews"]) == (6, 7)
    # erin left five reviews on one PR (1025), more than carol's four on four PRs; ranking by reviews would put erin
    # above carol, ranking by PRs reviewed puts her below
    assert (top.loc["erin", "prs_reviewed"], top.loc["erin", "reviews"]) == (1, 5)
    assert top.loc["erin", "reviews"] > top.loc["carol", "reviews"] and top.loc["erin", "prs_reviewed"] < top.loc["carol", "prs_reviewed"]
    order = got["reviewers"]["reviewer"].tolist()
    assert order.index("carol") < order.index("erin")


@pytest.mark.any_data
def test_top_lists_are_limited_and_sorted():
    r = queries.top_reviewers(*FULL, limit=3)["reviewers"]
    assert len(r) == 3 and r["prs_reviewed"].is_monotonic_decreasing
    assert len(queries.top_authors(*FULL)) == 10


@pytest.mark.any_data
def test_quiet_window_gives_empty_lists_not_errors():
    r = queries.top_reviewers(*QUIET)
    assert r["reviewers"].empty and r["own_pr_comments_left_out"] == 0
    assert queries.top_authors(*QUIET).empty


# ---------- has_activity ----------

@pytest.mark.fixture_numbers
def test_has_activity_matches_the_known_windows():
    D = datetime.date
    nothing = (D(2023, 11, 1), D(2023, 11, 30))  # November 2023: no PR opened or merged, no review
    reviews_only = (D(2024, 12, 1), D(2024, 12, 31))  # December 2024: PR 1039 opened and reviewed, nothing merged, a person's PR
    bots_only = (D(2024, 8, 1), D(2024, 8, 31))  # August 2024: only dependabot PRs 1032 and 1033 open or merge
    review_day = (D(2023, 2, 7), D(2023, 2, 7))  # one day with a review (on a PR opened earlier) and no PR opened or merged
    for window, bots, expected in [(nothing, True, False), (nothing, False, False), (reviews_only, False, True), (reviews_only, True, True),
                                   (bots_only, False, False), (bots_only, True, True), (review_day, False, True), (review_day, True, True), (FULL, False, True), (FULL, True, True)]:
        assert queries.has_activity(*window, bots) == activity_expected(window, bots) == expected, (window, bots)
    # a quiet window after the last event: nothing at all
    assert not queries.has_activity(D(2026, 3, 30), D(2026, 8, 28), True)


@pytest.mark.any_data
def test_has_activity_agrees_with_the_other_queries_for_every_month():
    # whenever it says there is activity, at least one of the PR or review queries must find something
    for month in queries.trend(*FULL, True, "month")["period_start"]:
        start = month.date()
        end = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
        found = queries.has_activity(start, end, True)
        evidence = (queries.kpis(start, end, True)["merged_prs"] > 0 or queries.review_funnel(start, end, True)["opened"] > 0
                    or queries.run("select count(*) from main.stg_github__pr_reviews where cast(submitted_at as date) between ? and ?", [start, end])[0][0] > 0)
        assert found == evidence, f"{start}: has_activity={found}, evidence={evidence}"
