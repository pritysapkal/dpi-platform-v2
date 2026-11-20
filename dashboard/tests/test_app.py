"""Run the real Streamlit app headlessly (no browser) and read what it shows."""

import datetime
import json

import pytest
from streamlit.testing.v1 import AppTest

import queries

pytestmark = pytest.mark.skipif(not queries.DB_PATH.exists(), reason="dev.duckdb not built")

APP = str(queries.PROJECT_DIR / "dashboard" / "app.py")
Y2024 = (datetime.date(2024, 1, 1), datetime.date(2024, 12, 31))
NO_MERGES = (datetime.date(2025, 12, 1), datetime.date(2025, 12, 3))  # humans: nothing at all; with bots: 1 bot PR opened
REVIEWS_ONLY = (datetime.date(2026, 3, 30), datetime.date(2026, 9, 30))  # 12 reviews, but no PR opened or merged


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


def test_default_view_has_the_five_tiles_and_no_days_tracked():
    t = tiles(run_app())
    assert list(t)[:5] == ["Merged PRs", "Median lead time", "p90 lead time", "Review coverage", "Median time to first review"]
    assert "Days Tracked" not in t


def test_bots_are_excluded_by_default_and_the_toggle_adds_them():
    assert run_app().checkbox(key="include_bots").value is False
    assert tiles(run_app())["Merged PRs"] == "207"
    assert tiles(run_app(include_bots=True))["Merged PRs"] == "288"


def test_data_as_of_is_shown():
    text = " ".join(c.value for c in run_app().caption)
    assert "Data as of 2026-11-01 18:34 UTC" in text


# The Step 2 acceptance check, through the real app: 2024, bots on and off.
def test_2024_tiles_with_bots_equal_mf_query_and_agent():
    t = tiles(run_app(date_range=Y2024, include_bots=True))
    assert t["Merged PRs"] == "109"
    assert t["Median lead time"] == "23.9 h"  # 23.91 in mf query and the agent
    assert t["p90 lead time"] == "495.8 h"  # 495.83


def test_2024_tiles_humans_only_equal_mf_query_and_agent():
    t = tiles(run_app(date_range=Y2024, include_bots=False))
    assert t["Merged PRs"] == "92"
    assert t["Median lead time"] == "23.2 h"  # 23.18
    assert t["p90 lead time"] == "697.9 h"  # 697.91


def test_range_with_activity_but_no_merges_shows_dashes_a_warning_and_no_trend_charts():
    at = run_app(date_range=REVIEWS_ONLY)  # 12 reviews were submitted, nothing was opened or merged
    t = tiles(at)
    assert t["Merged PRs"] == "0" and t["Median lead time"] == "–" and t["Review coverage"] == "–"
    assert any("No PRs were merged" in w.value for w in at.warning)
    assert any("no trends to show" in i.value for i in at.info)
    assert not any("merged_prs" in c.proto.spec for c in charts(at))  # no empty trend chart with a 0.000000 axis
    assert len(at.radio) == 0  # the weekly/monthly selector has nothing to switch, so it isn't shown

def test_picking_only_a_start_date_waits_for_the_end_date():
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.date_input(key="date_range").set_value((datetime.date(2024, 1, 1),)).run()
    assert not at.exception
    assert any("end date" in i.value for i in at.info)


# ---------- trend charts ----------

def charts(at):
    return at.get("vega_lite_chart")  # st.bar_chart / st.line_chart


def chart_data(chart):
    """The DataFrame a chart was drawn from, read back out of the rendered page."""
    import streamlit.dataframe_util as dataframe_util
    return dataframe_util.convert_arrow_bytes_to_pandas_df(chart.proto.datasets[0].data.data)


def test_trend_section_has_a_bar_chart_and_two_line_charts_and_defaults_to_monthly():
    at = run_app()
    assert at.radio(key="grain").value == "Monthly"
    bar, median_line, p90_line = charts(at)[:3]
    assert '"bar"' in bar.proto.spec and "merged_prs" in bar.proto.spec
    assert "median_lead_time_hours" in median_line.proto.spec
    assert "p90_lead_time_hours" in p90_line.proto.spec


def test_switching_to_weekly_changes_the_data_not_the_layout():
    at = run_app(date_range=Y2024, include_bots=True)
    charts_before = len(charts(at))
    monthly = chart_data(charts(at)[0])
    at.radio(key="grain").set_value("Weekly").run()
    assert not at.exception
    weekly = chart_data(charts(at)[0])
    assert len(charts(at)) == charts_before  # same page, only the trend data changes
    assert len(monthly) == 12 and len(weekly) > 50
    assert int(monthly["merged_prs"].sum()) == int(weekly["merged_prs"].sum()) == 109
    assert any("per week" in m.value for m in at.markdown)


def test_trend_charts_respect_the_bot_toggle_and_date_range():
    with_bots = chart_data(charts(run_app(date_range=Y2024, include_bots=True))[0])
    humans = chart_data(charts(run_app(date_range=Y2024, include_bots=False))[0])
    assert int(with_bots["merged_prs"].sum()) == 109 and len(with_bots) == 12
    assert int(humans["merged_prs"].sum()) == 92 and len(humans) == 12


def test_trend_chart_data_equals_the_query_result():
    at = run_app(date_range=Y2024, include_bots=False)
    shown = chart_data(charts(at)[0])
    expected = queries.trend(*Y2024, False, "month")
    assert shown["merged_prs"].tolist() == expected["merged_prs"].tolist()


# ---------- lead-time breakdown ----------

def test_breakdown_section_shows_the_stage_table_and_share_chart():
    at = run_app()
    assert any("waiting for a first review vs. in review" in s.value for s in at.subheader)
    table = at.dataframe[0].value
    assert table["Stage"].tolist() == ["Waiting for first review", "In review (first review to merge)", "Whole lead time"]
    expected = queries.lead_time_breakdown(datetime.date(2023, 1, 1), datetime.date(2026, 11, 1))
    assert table["Median (h)"].tolist() == [round(expected["median_wait_hours"], 1),
                                            round(expected["median_in_review_hours"], 1),
                                            round(expected["median_lead_time_hours"], 1)]
    shares = chart_data(charts(at)[3])
    assert shares["share of hours (%)"].sum() == pytest.approx(100.0)


def test_breakdown_ignores_the_bot_toggle_and_says_so():
    humans = run_app(include_bots=False).dataframe[0].value
    everyone = run_app(include_bots=True).dataframe[0].value
    assert humans.equals(everyone)
    assert any("Human-authored PRs only, whatever the bot checkbox says" in c.value for c in run_app().caption)


def test_breakdown_follows_the_date_range():
    at = run_app(date_range=Y2024)
    expected = queries.lead_time_breakdown(*Y2024)
    assert at.dataframe[0].value["Median (h)"].tolist()[0] == round(expected["median_wait_hours"], 1)
    assert any(f"Based on {expected['split_prs']:,} PRs" in c.value for c in at.caption)


def test_breakdown_with_nothing_to_split_shows_a_message_not_an_error():
    at = run_app(date_range=REVIEWS_ONLY)
    assert any("nothing to split" in i.value for i in at.info)
    assert not any("Stage" in table.value.columns for table in at.dataframe)  # no breakdown table

def funnel_table(at):
    return next(t.value for t in at.dataframe if "% of opened" in t.value.columns)


def test_funnel_section_matches_the_query_and_defaults_to_human_prs():
    at = run_app()
    f = queries.review_funnel(datetime.date(2023, 1, 1), datetime.date(2026, 11, 1), False)
    table = funnel_table(at)
    assert table["stage"].tolist() == ["Opened", "Reviewed", "Approved", "Merged"]
    assert table["PRs"].tolist() == [f["opened"], f["reviewed"], f["approved"], f["merged"]] == [419, 255, 201, 207]
    assert table["% of opened"].tolist()[0] == 100.0
    assert [c.proto.spec for c in charts(at)][4].count("PRs") > 0


def test_funnel_follows_the_bot_toggle_and_the_date_range():
    everyone = funnel_table(run_app(include_bots=True))["PRs"].tolist()
    assert everyone[0] == 523 and everyone[3] == 288
    y2024 = funnel_table(run_app(date_range=Y2024, include_bots=False))["PRs"].tolist()
    expected = queries.review_funnel(*Y2024, False)
    assert y2024 == [expected["opened"], expected["reviewed"], expected["approved"], expected["merged"]]


def test_funnel_chart_keeps_the_stages_in_funnel_order():
    chart = charts(run_app())[4]
    assert chart_data(chart)["stage"].tolist() == ["Opened", "Reviewed", "Approved", "Merged"]
    assert json.loads(chart.proto.spec)["encoding"]["y"]["sort"] is None  # not re-sorted alphabetically or by size


def test_funnel_with_no_opened_prs_shows_a_message_not_an_error():
    at = run_app(date_range=(datetime.date(2026, 3, 30), datetime.date(2026, 9, 30)))
    assert any("No PRs were opened" in i.value for i in at.info)


def test_funnel_caption_is_honest_that_it_is_not_a_strict_funnel():
    text = " ".join(c.value for c in run_app().caption)
    assert "not approved before they were merged" in text and "still open when the data was pulled" in text


# ---------- open PRs ----------

def open_table(at):
    return next(t.value for t in at.dataframe if "Age (days)" in t.value.columns)


def test_open_pr_section_shows_tiles_and_the_ten_oldest_human_prs_by_default():
    at = run_app()
    t = tiles(at)
    expected = queries.open_pr_age(datetime.date(2023, 1, 1), datetime.date(2026, 11, 1), False)
    assert t["Open PRs"] == f"{expected['open_prs']:,}" == "73"
    assert t["Median age"] == f"{expected['median_age_days']:,.0f} days"
    assert t["Never reviewed"] == "45"
    table = open_table(at)
    assert table["PR"].tolist() == expected["oldest"]["pr_number"].tolist() and len(table) == 10
    assert table["Age (days)"].is_monotonic_decreasing


def test_open_pr_section_follows_the_bot_toggle_and_says_ages_are_as_of_the_pull_date():
    assert tiles(run_app(include_bots=True))["Open PRs"] == "76"
    text = " ".join(c.value for c in run_app().caption)
    assert "measured up to 2026-11-01" in text and "not today" in text


def test_open_pr_age_chart_keeps_the_age_bands_in_order():
    chart = charts(run_app())[5]
    assert chart_data(chart)["age"].tolist() == [b[0] for b in queries.AGE_BUCKETS]
    assert chart_data(chart)["open PRs"].sum() == 73


def test_open_pr_section_with_nothing_open_shows_a_message_not_an_error():
    at = run_app(date_range=(datetime.date(2026, 3, 30), datetime.date(2026, 9, 30)))
    assert any("still open when the data was pulled" in i.value for i in at.info)


# ---------- who reviews and who writes ----------

QUIET = (datetime.date(2026, 3, 30), datetime.date(2026, 8, 28))


def person_tables(at):
    reviewers = next(t.value for t in at.dataframe if "PRs reviewed" in t.value.columns)
    authors = next(t.value for t in at.dataframe if "PRs opened" in t.value.columns)
    return reviewers, authors


def test_people_section_shows_top_reviewers_and_authors_from_the_queries():
    at = run_app()
    reviewers, authors = person_tables(at)
    assert reviewers["reviewer"].tolist()[:3] == ["lovelydinosaur", "Kludex", "zanieb"]
    assert reviewers["PRs reviewed"].tolist()[0] == 158
    assert authors["author"].tolist()[:2] == ["lovelydinosaur", "karpetrosyan"]
    assert "dependabot[bot]" not in authors["author"].tolist()
    assert not any(name.endswith("[bot]") for name in reviewers["reviewer"])


def test_people_section_ignores_the_bot_toggle_and_follows_the_date_range():
    off = person_tables(run_app(include_bots=False))
    on = person_tables(run_app(include_bots=True))
    assert off[0].equals(on[0]) and off[1].equals(on[1])
    y2024 = person_tables(run_app(date_range=Y2024))
    assert y2024[0]["PRs reviewed"].tolist() == queries.top_reviewers(*Y2024)["reviewers"]["prs_reviewed"].tolist()


def test_people_section_explains_how_reviews_are_counted():
    text = " ".join(c.value for c in run_app().caption)
    assert "188 comments that authors left on their own PRs are not counted" in text
    assert "different PRs" in text and "People only" in text


def test_reviewer_chart_keeps_the_ranking_order():
    chart = next(c for c in charts(run_app()) if "prs_reviewed" in c.proto.spec)
    assert chart_data(chart)["reviewer"].tolist()[:2] == ["lovelydinosaur", "Kludex"]


def test_people_section_with_no_people_shows_messages_not_errors():
    # one bot PR was opened and nobody reviewed anything: the page renders, the people section has nothing to rank
    at = run_app(date_range=NO_MERGES, include_bots=True)
    text = " ".join(i.value for i in at.info)
    assert "Nobody reviewed anyone else's PR" in text and "No PRs were opened by people" in text


# ---------- empty states: only the message, never an empty chart ----------

QUIET = (datetime.date(2026, 3, 30), datetime.date(2026, 8, 28))  # nothing at all, with or without bots


def numeric_values(chart):
    df = chart_data(chart)
    return [v for column in df.columns if str(df[column].dtype).startswith(("int", "float")) for v in df[column].dropna()]


@pytest.mark.parametrize("include_bots", [False, True])
def test_a_range_with_no_activity_shows_only_the_message(include_bots):
    at = run_app(date_range=QUIET, include_bots=include_bots)
    assert [i.value for i in at.info] == ["No pull request activity in this date range. Try a wider date range."]
    assert len(at.metric) == 0 and len(charts(at)) == 0 and len(at.dataframe) == 0 and len(at.subheader) == 0
    assert len(at.radio) == 0


def test_a_range_with_only_bot_activity_tells_you_to_include_bots_and_then_renders():
    humans_only = run_app(date_range=NO_MERGES, include_bots=False)
    assert len(humans_only.info) == 1 and "Include bot PRs" in humans_only.info[0].value
    assert len(humans_only.metric) == 0 and len(charts(humans_only)) == 0  # only the message
    with_bots = run_app(date_range=NO_MERGES, include_bots=True)
    assert len(with_bots.metric) > 0 and any("No PRs were merged" in w.value for w in with_bots.warning)
    assert not any("merged_prs" in c.proto.spec for c in charts(with_bots))  # still no empty trend chart


@pytest.mark.parametrize(
    "date_range, include_bots",
    [(None, False), (None, True), (Y2024, False), (Y2024, True), (REVIEWS_ONLY, False), (NO_MERGES, True)],
    ids=["full-humans", "full-all", "2024-humans", "2024-all", "reviews-only", "one-bot-pr"],
)
def test_no_chart_is_ever_drawn_with_nothing_in_it(date_range, include_bots):
    at = run_app(date_range=date_range, include_bots=include_bots)
    for chart in charts(at):
        assert any(v > 0 for v in numeric_values(chart)), f"an empty chart was drawn: {chart.proto.spec[:120]}"


# ---------- date presets ----------

import duckdb  # noqa: E402

import presets  # noqa: E402

PRESET_KEYS = {
    "All time": "preset_all_time", "Last 12 months": "preset_last_12_months",
    "2023": "preset_2023", "2024": "preset_2024", "2025": "preset_2025",
}
FIRST_DAY, LAST_DAY = datetime.date(2023, 1, 1), datetime.date(2026, 11, 1)
EXPECTED_RANGES = presets.presets(FIRST_DAY, LAST_DAY)


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


def test_the_five_preset_buttons_are_there_in_order():
    at = run_app()
    assert [b.label for b in at.sidebar.button] == ["All time", "Last 12 months", "2023", "2024", "2025"]
    assert [b.key for b in at.sidebar.button] == list(PRESET_KEYS.values())


@pytest.mark.parametrize("include_bots", [False, True], ids=["humans", "all-prs"])
@pytest.mark.parametrize("label", list(PRESET_KEYS))
def test_each_preset_sets_the_date_picker_and_the_figures_follow(label, include_bots):
    at = run_app(include_bots=include_bots)
    at.button(key=PRESET_KEYS[label]).click().run()
    assert not at.exception
    span = EXPECTED_RANGES[label]
    assert tuple(at.date_input(key="date_range").value) == span
    assert tiles(at)["Merged PRs"] == f"{raw_merged_count(span, include_bots):,}"
    assert any(f"{span[0]:%Y-%m-%d} to {span[1]:%Y-%m-%d}" in c.value for c in at.caption)


def test_known_preset_results_for_humans():
    expected = {"All time": "207", "2023": "88", "2024": "92", "2025": "26"}
    for label, merged in expected.items():
        at = run_app()
        at.button(key=PRESET_KEYS[label]).click().run()
        assert tiles(at)["Merged PRs"] == merged, label


def test_the_active_preset_is_highlighted_and_a_custom_range_highlights_none():
    at = run_app()
    assert button_types(at) == {"All time": "primary", "Last 12 months": "secondary", "2023": "secondary",
                                "2024": "secondary", "2025": "secondary"}
    at.button(key="preset_2024").click().run()
    assert [label for label, kind in button_types(at).items() if kind == "primary"] == ["2024"]
    at.date_input(key="date_range").set_value((datetime.date(2024, 3, 1), datetime.date(2024, 4, 30))).run()
    assert "primary" not in button_types(at).values()
    assert not at.exception


def test_a_preset_rescues_a_half_finished_date_pick():
    at = run_app()
    at.date_input(key="date_range").set_value((datetime.date(2024, 5, 1),)).run()
    assert any("end date" in i.value for i in at.info)
    assert len(at.date_input(key="date_range").value) == 1  # the half-finished pick was left alone, not reset
    at.button(key="preset_2024").click().run()
    assert not any("end date" in i.value for i in at.info)
    assert tiles(at)["Merged PRs"] == "92"


def test_presets_keep_the_bot_toggle_and_the_weekly_monthly_choice():
    at = run_app(include_bots=True)
    at.radio(key="grain").set_value("Weekly").run()
    at.button(key="preset_2024").click().run()
    assert at.checkbox(key="include_bots").value is True and at.radio(key="grain").value == "Weekly"
    assert tiles(at)["Merged PRs"] == "109"


def test_a_preset_can_be_followed_by_a_manual_change_and_another_preset():
    at = run_app()
    at.button(key="preset_2023").click().run()
    at.date_input(key="date_range").set_value((datetime.date(2023, 2, 1), datetime.date(2023, 2, 28))).run()
    assert tiles(at)["Merged PRs"] == f"{raw_merged_count((datetime.date(2023, 2, 1), datetime.date(2023, 2, 28)), False):,}"
    at.button(key="preset_all_time").click().run()
    assert tiles(at)["Merged PRs"] == "207" and not at.exception


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


def test_whole_number_axes_also_hold_for_tiny_counts():
    # a window with just one PR: the count axis must still be whole numbers, not 0.5 steps
    at = run_app(date_range=NO_MERGES, include_bots=True)
    for spec in chart_specs(at):
        channel = count_channel(spec)
        if channel:
            assert channel["axis"]["format"] == "d" and channel["axis"]["tickMinStep"] == 1


def test_the_non_count_charts_are_left_alone():
    # hours and shares are not PR counts, so they keep Streamlit's own axes (no integer format forced on them)
    for spec in chart_specs(run_app(date_range=Y2024, include_bots=True)):
        if count_channel(spec) is None:
            assert '"format": "d"' not in json.dumps(spec)


def test_the_monthly_bars_sit_on_a_band_axis_so_they_are_wide():
    at = run_app(date_range=Y2024, include_bots=True)
    spec = json.loads(charts(at)[0].proto.spec)
    assert spec["mark"]["type"] == "bar"
    x = spec["encoding"]["x"]
    assert x["type"] == "ordinal" and x["sort"] is None and x["field"] == "period_label"  # not a time axis of slivers
    assert x["scale"]["paddingInner"] <= 0.2
    labels = chart_data(charts(at)[0])["period_label"].tolist()
    assert labels == [f"2024-{m:02d}" for m in range(1, 13)]  # 12 monthly bars, in calendar order


def test_weekly_bars_are_labelled_with_their_monday_and_stay_in_order():
    at = run_app(date_range=Y2024, include_bots=True)
    at.radio(key="grain").set_value("Weekly").run()
    data = chart_data(charts(at)[0])
    labels = data["period_label"].tolist()
    assert len(labels) > 50 and labels == sorted(labels)
    assert all(datetime.date.fromisoformat(label).weekday() == 0 for label in labels)  # every label is a Monday
    assert labels == [d.strftime("%Y-%m-%d") for d in data["period_start"]]


def test_the_bar_labels_line_up_with_the_figures_for_both_groupings():
    for grain_label, grain in (("Monthly", "month"), ("Weekly", "week")):
        at = run_app(date_range=Y2024, include_bots=False)
        at.radio(key="grain").set_value(grain_label).run()
        shown = chart_data(charts(at)[0])
        expected = queries.trend(*Y2024, False, grain)
        assert shown["merged_prs"].tolist() == expected["merged_prs"].tolist()
        assert [pd_ts.date() for pd_ts in shown["period_start"]] == [d.date() for d in expected["period_start"]]
