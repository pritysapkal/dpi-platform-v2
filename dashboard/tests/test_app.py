"""Run the real Streamlit app headlessly (no browser) and read what it shows.

any_data tests check what the page does on any data: layout, behaviour, "the page shows what the query returns".
fixture_numbers tests pin exact figures of the 50-PR fixture, worked out from the case table in plain Python
(dashboard/tests/answer_key.py and fixtures/build_fixture.py), and use the fixture's own quiet stretches for the
empty-state cases.
"""

import datetime
import json
import statistics

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

import answer_key as ak
import build_fixture as fx
import presets
import queries

APP = str(queries.PROJECT_DIR / "dashboard" / "app.py")
D = datetime.date
Y2024 = (D(2024, 1, 1), D(2024, 12, 31))

# The fixture's data runs from 2023-01-01 to its pretend pull on 2026-01-15. These windows are inside it:
FIRST_DAY, LAST_DAY = D(2023, 1, 1), fx.PULLED_AT.date()
QUIET = (D(2025, 12, 1), D(2025, 12, 31))  # December 2025: nothing at all, with or without bots
REVIEWS_ONLY = (D(2023, 2, 7), D(2023, 2, 7))  # one day: a review (PR 1003), no PR opened or merged
ONE_BOT_PR = (D(2023, 7, 10), D(2023, 7, 10))  # one day: dependabot opens PR 1013; no merge, no review, nobody else


@pytest.mark.no_db
def test_the_windows_used_below_are_what_they_are_said_to_be():
    inside = lambda w: FIRST_DAY <= w[0] <= w[1] <= LAST_DAY  # noqa: E731
    assert all(inside(w) for w in (Y2024, QUIET, REVIEWS_ONLY, ONE_BOT_PR))
    assert min(ak.at(p.created).date() for p in fx.CASES) == FIRST_DAY
    assert not ak.activity_expected(QUIET, True)
    assert ak.activity_expected(REVIEWS_ONLY, False) and not any(ak.opened_in(p, REVIEWS_ONLY) or ak.merged_in(p, REVIEWS_ONLY) for p in fx.CASES)
    assert not ak.activity_expected(ONE_BOT_PR, False) and ak.activity_expected(ONE_BOT_PR, True)
    assert [p.number for p in fx.CASES if ak.opened_in(p, ONE_BOT_PR)] == [1013]


def run_app(date_range=None, include_bots=None):
    at = AppTest.from_file(APP, default_timeout=60).run()
    if date_range is not None:
        at.date_input(key="date_range").set_value(date_range).run()
    if include_bots is not None:
        at.checkbox(key="include_bots").set_value(include_bots).run()
    assert not at.exception, at.exception
    return at


def tiles(at):
    return {m.label: m.value for m in at.metric}


def hours(value):
    return f"{value:.1f} h"


@pytest.mark.any_data
def test_default_view_has_the_five_tiles_and_no_days_tracked():
    t = tiles(run_app())
    assert list(t)[:5] == ["Merged PRs", "Median lead time", "p90 lead time", "Review coverage", "Median time to first review"]
    assert "Days Tracked" not in t


@pytest.mark.fixture_numbers
def test_bots_are_excluded_by_default_and_the_toggle_adds_them():
    facts = fx.facts()
    assert run_app().checkbox(key="include_bots").value is False
    assert tiles(run_app())["Merged PRs"] == f"{facts['human_merged']:,}" == "27"
    assert tiles(run_app(include_bots=True))["Merged PRs"] == f"{facts['merged']:,}" == "39"


@pytest.mark.fixture_numbers
def test_data_as_of_is_shown():
    text = " ".join(c.value for c in run_app().caption)
    assert f"Data as of {fx.PULLED_AT:%Y-%m-%d %H:%M} UTC" in text and "Data as of 2026-01-15 12:00 UTC" in text


# The 2024 figures, through the real app, bots on and off; the expected values are the case table's own answers.
@pytest.mark.fixture_numbers
@pytest.mark.parametrize("include_bots, merged, median, p90", [(True, "16", "12.0 h", "144.0 h"), (False, "10", "36.0 h", "168.0 h")], ids=["with-bots", "humans-only"])
def test_2024_tiles_equal_the_case_table_answers(include_bots, merged, median, p90):
    expected = fx.period_facts("2024-01-01", "2024-12-31", humans_only=not include_bots)
    t = tiles(run_app(date_range=Y2024, include_bots=include_bots))
    assert t["Merged PRs"] == f"{expected['merged']:,}" == merged
    assert t["Median lead time"] == hours(expected["median"]) == median
    assert t["p90 lead time"] == hours(expected["p90"]) == p90


@pytest.mark.fixture_numbers
def test_range_with_activity_but_no_merges_shows_dashes_a_warning_and_no_trend_charts():
    at = run_app(date_range=REVIEWS_ONLY)  # one review was submitted, nothing was opened or merged
    t = tiles(at)
    assert t["Merged PRs"] == "0" and t["Median lead time"] == "–" and t["Review coverage"] == "–"
    assert any("No PRs were merged" in w.value for w in at.warning)
    assert any("no trends to show" in i.value for i in at.info)
    assert not any("merged_prs" in c.proto.spec for c in charts(at))  # no empty trend chart with a 0.000000 axis
    assert len(at.radio) == 0  # the weekly/monthly selector has nothing to switch, so it isn't shown


@pytest.mark.any_data
def test_picking_only_a_start_date_waits_for_the_end_date():
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.date_input(key="date_range").set_value((D(2024, 1, 1),)).run()
    assert not at.exception
    assert any("end date" in i.value for i in at.info)


# ---------- trend charts ----------

def charts(at):
    return at.get("vega_lite_chart")  # st.bar_chart / st.line_chart


def chart_data(chart):
    """The DataFrame a chart was drawn from, read back out of the rendered page."""
    import streamlit.dataframe_util as dataframe_util
    return dataframe_util.convert_arrow_bytes_to_pandas_df(chart.proto.datasets[0].data.data)


@pytest.mark.any_data
def test_trend_section_has_a_bar_chart_and_two_line_charts_and_defaults_to_monthly():
    at = run_app()
    assert at.radio(key="grain").value == "Monthly"
    bar, median_line, p90_line = charts(at)[:3]
    assert '"bar"' in bar.proto.spec and "merged_prs" in bar.proto.spec
    assert "median_lead_time_hours" in median_line.proto.spec
    assert "p90_lead_time_hours" in p90_line.proto.spec


@pytest.mark.fixture_numbers
def test_switching_to_weekly_changes_the_data_not_the_layout():
    at = run_app(date_range=Y2024, include_bots=True)
    charts_before = len(charts(at))
    monthly = chart_data(charts(at)[0])
    at.radio(key="grain").set_value("Weekly").run()
    assert not at.exception
    weekly = chart_data(charts(at)[0])
    assert len(charts(at)) == charts_before  # same page, only the trend data changes
    assert len(monthly) == 12 and len(weekly) > 50
    assert int(monthly["merged_prs"].sum()) == int(weekly["merged_prs"].sum()) == fx.period_facts("2024-01-01", "2024-12-31")["merged"] == 16
    assert any("per week" in m.value for m in at.markdown)


@pytest.mark.fixture_numbers
def test_trend_charts_respect_the_bot_toggle_and_date_range():
    with_bots = chart_data(charts(run_app(date_range=Y2024, include_bots=True))[0])
    humans = chart_data(charts(run_app(date_range=Y2024, include_bots=False))[0])
    assert int(with_bots["merged_prs"].sum()) == 16 and len(with_bots) == 12
    assert int(humans["merged_prs"].sum()) == 10 and len(humans) == 12


@pytest.mark.any_data
def test_trend_chart_data_equals_the_query_result():
    at = run_app(date_range=Y2024, include_bots=False)
    shown = chart_data(charts(at)[0])
    expected = queries.trend(*Y2024, False, "month")
    assert shown["merged_prs"].tolist() == expected["merged_prs"].tolist()


# ---------- lead-time breakdown ----------

@pytest.mark.any_data
def test_breakdown_section_shows_the_stage_table_and_share_chart():
    at = run_app()
    assert any("waiting for a first review vs. in review" in s.value for s in at.subheader)
    table = at.dataframe[0].value
    assert table["Stage"].tolist() == ["Waiting for first review", "In review (first review to merge)", "Whole lead time"]
    first, last, _ = queries.data_window()
    expected = queries.lead_time_breakdown(first, last)
    assert table["Median (h)"].tolist() == [round(expected["median_wait_hours"], 1),
                                            round(expected["median_in_review_hours"], 1),
                                            round(expected["median_lead_time_hours"], 1)]
    shares = chart_data(charts(at)[3])
    assert shares["share of hours (%)"].sum() == pytest.approx(100.0)


@pytest.mark.fixture_numbers
def test_breakdown_table_equals_the_case_table_answers():
    want = ak.breakdown_expected()
    table = run_app().dataframe[0].value
    assert table["Median (h)"].tolist() == [round(want["median_wait"], 1), round(want["median_review"], 1), round(want["median_lead"], 1)]
    assert any(f"Based on {want['split']:,} PRs" in c.value for c in run_app().caption) and want["split"] == 22


@pytest.mark.any_data
def test_breakdown_ignores_the_bot_toggle_and_says_so():
    humans = run_app(include_bots=False).dataframe[0].value
    everyone = run_app(include_bots=True).dataframe[0].value
    assert humans.equals(everyone)
    assert any("Human-authored PRs only, whatever the bot checkbox says" in c.value for c in run_app().caption)


@pytest.mark.any_data
def test_breakdown_follows_the_date_range():
    at = run_app(date_range=Y2024)
    expected = queries.lead_time_breakdown(*Y2024)
    assert at.dataframe[0].value["Median (h)"].tolist()[0] == round(expected["median_wait_hours"], 1)
    assert any(f"Based on {expected['split_prs']:,} PRs" in c.value for c in at.caption)


@pytest.mark.fixture_numbers
def test_breakdown_with_nothing_to_split_shows_a_message_not_an_error():
    at = run_app(date_range=REVIEWS_ONLY)
    assert any("nothing to split" in i.value for i in at.info)
    assert not any("Stage" in table.value.columns for table in at.dataframe)  # no breakdown table


# ---------- review funnel ----------

def funnel_table(at):
    return next(t.value for t in at.dataframe if "% of opened" in t.value.columns)


def funnel_list(expected):
    return [expected["opened"], expected["reviewed"], expected["approved"], expected["merged"]]


@pytest.mark.fixture_numbers
def test_funnel_section_matches_the_case_table_and_defaults_to_human_prs():
    at = run_app()
    want = funnel_list(ak.funnel_expected(False))
    table = funnel_table(at)
    facts = fx.facts()
    assert table["stage"].tolist() == ["Opened", "Reviewed", "Approved", "Merged"]
    assert table["PRs"].tolist() == want
    assert (want[0], want[3]) == (facts["prs"] - facts["bot_prs"], facts["human_merged"]) == (36, 27)
    assert table["% of opened"].tolist()[0] == 100.0
    assert [c.proto.spec for c in charts(at)][4].count("PRs") > 0


@pytest.mark.fixture_numbers
def test_funnel_follows_the_bot_toggle_and_the_date_range():
    everyone = funnel_table(run_app(include_bots=True))["PRs"].tolist()
    assert everyone == funnel_list(ak.funnel_expected(True)) and everyone[0] == 50 and everyone[3] == 39
    y2024 = funnel_table(run_app(date_range=Y2024, include_bots=False))["PRs"].tolist()
    assert y2024 == funnel_list(ak.funnel_expected(False, Y2024))


@pytest.mark.any_data
def test_funnel_chart_keeps_the_stages_in_funnel_order():
    chart = charts(run_app())[4]
    assert chart_data(chart)["stage"].tolist() == ["Opened", "Reviewed", "Approved", "Merged"]
    assert json.loads(chart.proto.spec)["encoding"]["y"]["sort"] is None  # not re-sorted alphabetically or by size


@pytest.mark.fixture_numbers
def test_funnel_with_no_opened_prs_shows_a_message_not_an_error():
    at = run_app(date_range=REVIEWS_ONLY)  # activity (a review) but no PR opened that day
    assert any("No PRs were opened" in i.value for i in at.info)


@pytest.mark.any_data
def test_funnel_caption_is_honest_that_it_is_not_a_strict_funnel():
    text = " ".join(c.value for c in run_app().caption)
    assert "not approved before they were merged" in text and "still open when the data was pulled" in text


@pytest.mark.fixture_numbers
def test_funnel_caption_numbers_nest_because_both_stages_mean_before_the_merge():
    f = ak.funnel_expected(True)
    text = " ".join(c.value for c in run_app(include_bots=True).caption)
    assert f"{f['merged_without_approval']:,} merged PRs were not approved before they were merged, and {f['merged_without_review']:,} of those" in text
    assert f["merged_without_review"] <= f["merged_without_approval"]


# ---------- open PRs ----------

def open_table(at):
    return next(t.value for t in at.dataframe if "Age (days)" in t.value.columns)


@pytest.mark.fixture_numbers
def test_open_pr_section_shows_tiles_and_the_oldest_human_prs_by_default():
    rows = ak.open_prs_expected(False)
    ages = [age for age, _ in rows]
    at = run_app()
    t = tiles(at)
    assert t["Open PRs"] == f"{len(rows):,}" == "5"  # six PRs are open; one is dependabot's
    assert t["Median age"] == f"{statistics.median(ages):,.0f} days"
    assert t["Never reviewed"] == f"{sum(1 for _, p in rows if not p.reviews):,}" == "2"  # 1012 and 1050
    table = open_table(at)
    assert table["PR"].tolist() == [p.number for _, p in rows][:10] and table["PR"].iloc[0] == 1012  # the 2023 PR is the oldest
    assert table["Age (days)"].is_monotonic_decreasing


@pytest.mark.fixture_numbers
def test_open_pr_section_follows_the_bot_toggle_and_says_ages_are_as_of_the_pull_date():
    assert tiles(run_app(include_bots=True))["Open PRs"] == f"{len(ak.open_prs_expected(True)):,}" == "6"
    text = " ".join(c.value for c in run_app().caption)
    assert f"measured up to {fx.PULLED_AT:%Y-%m-%d}" in text and "not today" in text


@pytest.mark.fixture_numbers
def test_open_pr_age_chart_keeps_the_age_bands_in_order():
    chart = charts(run_app())[5]
    assert chart_data(chart)["age"].tolist() == [b[0] for b in queries.AGE_BUCKETS]
    assert chart_data(chart)["open PRs"].sum() == len(ak.open_prs_expected(False)) == 5


@pytest.mark.fixture_numbers
def test_open_pr_section_with_nothing_open_shows_a_message_not_an_error():
    january_2023 = (D(2023, 1, 1), D(2023, 1, 31))  # PRs were opened, but none of them is still open
    assert ak.open_prs_expected(True, january_2023) == [] and any(ak.opened_in(p, january_2023) for p in fx.CASES)
    at = run_app(date_range=january_2023)
    assert any("still open when the data was pulled" in i.value for i in at.info)


# ---------- who reviews and who writes ----------

def person_tables(at):
    reviewers = next(t.value for t in at.dataframe if "PRs reviewed" in t.value.columns)
    authors = next(t.value for t in at.dataframe if "PRs opened" in t.value.columns)
    return reviewers, authors


@pytest.mark.fixture_numbers
def test_people_section_shows_top_reviewers_and_authors_from_the_case_table():
    reviewers, authors = person_tables(run_app())
    want_reviewers, _ = ak.reviewers_expected()
    want_authors = ak.authors_expected()
    assert [tuple(r) for r in reviewers.itertuples(index=False)] == want_reviewers
    assert reviewers["reviewer"].tolist()[:3] == ["alice", "bob", "grace"] and reviewers["PRs reviewed"].tolist()[0] == 8
    assert [tuple(r) for r in authors.itertuples(index=False)] == want_authors
    assert authors["author"].tolist()[:2] == ["alice", "erin"] and len(authors) == 10  # 11 people write PRs; the list stops at 10
    assert "dependabot[bot]" not in authors["author"].tolist()
    assert not any(name.endswith("[bot]") for name in reviewers["reviewer"])


@pytest.mark.any_data
def test_people_section_ignores_the_bot_toggle_and_follows_the_date_range():
    off = person_tables(run_app(include_bots=False))
    on = person_tables(run_app(include_bots=True))
    assert off[0].equals(on[0]) and off[1].equals(on[1])
    y2024 = person_tables(run_app(date_range=Y2024))
    assert y2024[0]["PRs reviewed"].tolist() == queries.top_reviewers(*Y2024)["reviewers"]["prs_reviewed"].tolist()


@pytest.mark.fixture_numbers
def test_people_section_explains_how_reviews_are_counted():
    _, own = ak.reviewers_expected()
    text = " ".join(c.value for c in run_app().caption)
    assert f"{own:,} comments that authors left on their own PRs are not counted" in text and own == 1
    assert "different PRs" in text and "People only" in text


@pytest.mark.fixture_numbers
def test_reviewer_chart_keeps_the_ranking_order():
    chart = next(c for c in charts(run_app()) if "prs_reviewed" in c.proto.spec)
    assert chart_data(chart)["reviewer"].tolist() == [r[0] for r in ak.reviewers_expected()[0]]


@pytest.mark.fixture_numbers
def test_people_section_with_no_people_shows_messages_not_errors():
    # one bot PR was opened and nobody reviewed anything: the page renders, the people section has nothing to rank
    at = run_app(date_range=ONE_BOT_PR, include_bots=True)
    text = " ".join(i.value for i in at.info)
    assert "Nobody reviewed anyone else's PR" in text and "No PRs were opened by people" in text


# ---------- empty states: only the message, never an empty chart ----------

def numeric_values(chart):
    df = chart_data(chart)
    return [v for column in df.columns if str(df[column].dtype).startswith(("int", "float")) for v in df[column].dropna()]


@pytest.mark.fixture_numbers
@pytest.mark.parametrize("include_bots", [False, True])
def test_a_range_with_no_activity_shows_only_the_message(include_bots):
    at = run_app(date_range=QUIET, include_bots=include_bots)
    assert [i.value for i in at.info] == ["No pull request activity in this date range. Try a wider date range."]
    assert len(at.metric) == 0 and len(charts(at)) == 0 and len(at.dataframe) == 0 and len(at.subheader) == 0
    assert len(at.radio) == 0


@pytest.mark.fixture_numbers
def test_a_range_with_only_bot_activity_tells_you_to_include_bots_and_then_renders():
    humans_only = run_app(date_range=ONE_BOT_PR, include_bots=False)
    assert len(humans_only.info) == 1 and "Include bot PRs" in humans_only.info[0].value
    assert len(humans_only.metric) == 0 and len(charts(humans_only)) == 0  # only the message
    with_bots = run_app(date_range=ONE_BOT_PR, include_bots=True)
    assert len(with_bots.metric) > 0 and any("No PRs were merged" in w.value for w in with_bots.warning)
    assert not any("merged_prs" in c.proto.spec for c in charts(with_bots))  # still no empty trend chart


@pytest.mark.any_data
@pytest.mark.parametrize(
    "date_range, include_bots",
    [(None, False), (None, True), (Y2024, False), (Y2024, True), (REVIEWS_ONLY, False), (ONE_BOT_PR, True)],
    ids=["full-humans", "full-all", "2024-humans", "2024-all", "reviews-only", "one-bot-pr"],
)
def test_no_chart_is_ever_drawn_with_nothing_in_it(date_range, include_bots):
    at = run_app(date_range=date_range, include_bots=include_bots)
    for chart in charts(at):
        assert any(v > 0 for v in numeric_values(chart)), f"an empty chart was drawn: {chart.proto.spec[:120]}"


# ---------- date presets ----------

PRESET_KEYS = {
    "All time": "preset_all_time", "Last 12 months": "preset_last_12_months",
    "2023": "preset_2023", "2024": "preset_2024", "2025": "preset_2025",
}


def expected_ranges():
    first, last, _ = queries.data_window()  # the first and last day the data covers, whatever dataset this is
    return presets.presets(first, last)


def raw_merged_count(span, include_bots):
    """Merged PRs in the span, counted straight from the raw table (no dbt model, no dashboard code)."""
    con = duckdb.connect(str(queries.DB_PATH), read_only=True)
    try:
        return con.execute(
            "select count(*) from raw.github_pull_requests where merged_at is not null "
            "and cast(cast(merged_at as timestamp) as date) between ? and ? and (? or author not like '%[bot]')",
            [span[0], span[1], include_bots],
        ).fetchone()[0]
    finally:
        con.close()


def button_types(at):
    return {label: at.button(key=key).proto.type for label, key in PRESET_KEYS.items()}


@pytest.mark.any_data
def test_the_five_preset_buttons_are_there_in_order():
    at = run_app()
    assert [b.label for b in at.sidebar.button] == ["All time", "Last 12 months", "2023", "2024", "2025"]
    assert [b.key for b in at.sidebar.button] == list(PRESET_KEYS.values())


@pytest.mark.any_data
@pytest.mark.parametrize("include_bots", [False, True], ids=["humans", "all-prs"])
@pytest.mark.parametrize("label", list(PRESET_KEYS))
def test_each_preset_sets_the_date_picker_and_the_figures_follow(label, include_bots):
    at = run_app(include_bots=include_bots)
    at.button(key=PRESET_KEYS[label]).click().run()
    assert not at.exception
    span = expected_ranges()[label]
    assert tuple(at.date_input(key="date_range").value) == span
    assert tiles(at)["Merged PRs"] == f"{raw_merged_count(span, include_bots):,}"
    assert any(f"{span[0]:%Y-%m-%d} to {span[1]:%Y-%m-%d}" in c.value for c in at.caption)


@pytest.mark.fixture_numbers
def test_known_preset_results_for_humans():
    def humans(start, end):
        return fx.period_facts(start, end, humans_only=True)["merged"]

    expected = {"All time": fx.facts()["human_merged"], "2023": humans("2023-01-01", "2023-12-31"),
                "2024": humans("2024-01-01", "2024-12-31"), "2025": humans("2025-01-01", "2025-12-31")}
    assert expected == {"All time": 27, "2023": 12, "2024": 10, "2025": 5}
    for label, merged in expected.items():
        at = run_app()
        at.button(key=PRESET_KEYS[label]).click().run()
        assert tiles(at)["Merged PRs"] == f"{merged:,}", label


@pytest.mark.any_data
def test_the_active_preset_is_highlighted_and_a_custom_range_highlights_none():
    at = run_app()
    assert button_types(at) == {"All time": "primary", "Last 12 months": "secondary", "2023": "secondary",
                                "2024": "secondary", "2025": "secondary"}
    at.button(key="preset_2024").click().run()
    assert [label for label, kind in button_types(at).items() if kind == "primary"] == ["2024"]
    at.date_input(key="date_range").set_value((D(2024, 3, 1), D(2024, 4, 30))).run()
    assert "primary" not in button_types(at).values()
    assert not at.exception


@pytest.mark.fixture_numbers
def test_a_preset_rescues_a_half_finished_date_pick():
    at = run_app()
    at.date_input(key="date_range").set_value((D(2024, 5, 1),)).run()
    assert any("end date" in i.value for i in at.info)
    assert len(at.date_input(key="date_range").value) == 1  # the half-finished pick was left alone, not reset
    at.button(key="preset_2024").click().run()
    assert not any("end date" in i.value for i in at.info)
    assert tiles(at)["Merged PRs"] == "10"  # people's merges in 2024


@pytest.mark.fixture_numbers
def test_presets_keep_the_bot_toggle_and_the_weekly_monthly_choice():
    at = run_app(include_bots=True)
    at.radio(key="grain").set_value("Weekly").run()
    at.button(key="preset_2024").click().run()
    assert at.checkbox(key="include_bots").value is True and at.radio(key="grain").value == "Weekly"
    assert tiles(at)["Merged PRs"] == "16"  # everyone's merges in 2024


@pytest.mark.fixture_numbers
def test_a_preset_can_be_followed_by_a_manual_change_and_another_preset():
    at = run_app()
    at.button(key="preset_2023").click().run()
    february = (D(2023, 2, 1), D(2023, 2, 28))
    at.date_input(key="date_range").set_value(february).run()
    assert tiles(at)["Merged PRs"] == f"{raw_merged_count(february, False):,}"
    at.button(key="preset_all_time").click().run()
    assert tiles(at)["Merged PRs"] == "27" and not at.exception


# ---------- whole-number axes and wide bars ----------

COUNT_FIELDS = {"merged_prs", "PRs", "open PRs", "prs_reviewed", "prs_opened"}  # the PR-count charts


def chart_specs(at):
    return [json.loads(c.proto.spec) for c in charts(at)]


def count_channel(spec):
    """The channel (x or y) that holds the PR count, or None if this isn't a PR-count chart."""
    for channel in ("x", "y"):
        encoding = spec.get("encoding", {}).get(channel, {})
        if encoding.get("field") in COUNT_FIELDS:
            return encoding
    return None


@pytest.mark.any_data
def test_every_pr_count_chart_has_a_whole_number_axis_starting_at_zero():
    at = run_app(date_range=Y2024, include_bots=True)
    found = {}
    for spec in chart_specs(at):
        channel = count_channel(spec)
        if channel:
            found[channel["field"]] = channel
    assert set(found) == COUNT_FIELDS  # all five PR-count charts are on the page
    for field, channel in found.items():
        assert channel["axis"]["format"] == "d" and channel["axis"]["tickMinStep"] == 1, field
        assert channel["scale"]["domainMin"] == 0, field


@pytest.mark.fixture_numbers
def test_whole_number_axes_also_hold_for_tiny_counts():
    # a window with just one PR: the count axis must still be whole numbers, not 0.5 steps
    at = run_app(date_range=ONE_BOT_PR, include_bots=True)
    found = [channel for channel in map(count_channel, chart_specs(at)) if channel]
    assert found, "no PR-count chart on the page, so nothing was checked"
    for channel in found:
        assert channel["axis"]["format"] == "d" and channel["axis"]["tickMinStep"] == 1


@pytest.mark.any_data
def test_the_non_count_charts_are_left_alone():
    # hours and shares are not PR counts, so they keep Streamlit's own axes (no integer format forced on them)
    for spec in chart_specs(run_app(date_range=Y2024, include_bots=True)):
        if count_channel(spec) is None:
            assert '"format": "d"' not in json.dumps(spec)


@pytest.mark.any_data
def test_the_monthly_bars_sit_on_a_band_axis_so_they_are_wide():
    at = run_app(date_range=Y2024, include_bots=True)
    spec = json.loads(charts(at)[0].proto.spec)
    assert spec["mark"]["type"] == "bar"
    x = spec["encoding"]["x"]
    assert x["type"] == "ordinal" and x["sort"] is None and x["field"] == "period_label"  # not a time axis of slivers
    assert x["scale"]["paddingInner"] <= 0.2
    labels = chart_data(charts(at)[0])["period_label"].tolist()
    assert labels == [f"2024-{m:02d}" for m in range(1, 13)]  # 12 monthly bars, in calendar order


@pytest.mark.any_data
def test_weekly_bars_are_labelled_with_their_monday_and_stay_in_order():
    at = run_app(date_range=Y2024, include_bots=True)
    at.radio(key="grain").set_value("Weekly").run()
    data = chart_data(charts(at)[0])
    labels = data["period_label"].tolist()
    assert len(labels) > 50 and labels == sorted(labels)
    assert all(datetime.date.fromisoformat(label).weekday() == 0 for label in labels)  # every label is a Monday
    assert labels == [d.strftime("%Y-%m-%d") for d in data["period_start"]]


@pytest.mark.any_data
def test_the_bar_labels_line_up_with_the_figures_for_both_groupings():
    for grain_label, grain in (("Monthly", "month"), ("Weekly", "week")):
        at = run_app(date_range=Y2024, include_bots=False)
        at.radio(key="grain").set_value(grain_label).run()
        shown = chart_data(charts(at)[0])
        expected = queries.trend(*Y2024, False, grain)
        assert shown["merged_prs"].tolist() == expected["merged_prs"].tolist()
        assert [pd_ts.date() for pd_ts in shown["period_start"]] == [d.date() for d in expected["period_start"]]
