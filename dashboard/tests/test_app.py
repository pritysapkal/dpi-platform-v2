"""Run the real Streamlit app headlessly (no browser) and read what it shows."""

import datetime
import json

import pytest
from streamlit.testing.v1 import AppTest

import queries

pytestmark = pytest.mark.skipif(not queries.DB_PATH.exists(), reason="dev.duckdb not built")

APP = str(queries.DB_PATH.parent / "dashboard" / "app.py")
Y2024 = (datetime.date(2024, 1, 1), datetime.date(2024, 12, 31))
NO_MERGES = (datetime.date(2025, 12, 1), datetime.date(2025, 12, 3))


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
    assert list(t) == ["Merged PRs", "Median lead time", "p90 lead time", "Review coverage", "Median time to first review"]
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


def test_empty_range_shows_dashes_and_a_warning_instead_of_crashing():
    at = run_app(date_range=NO_MERGES)
    t = tiles(at)
    assert t["Merged PRs"] == "0" and t["Median lead time"] == "–" and t["Review coverage"] == "–"
    assert any("No PRs were merged" in w.value for w in at.warning)


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
    monthly = chart_data(charts(at)[0])
    at.radio(key="grain").set_value("Weekly").run()
    assert not at.exception
    weekly = chart_data(charts(at)[0])
    assert len(charts(at)) == 5  # 3 trend charts + the lead-time breakdown + the funnel
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
    at = run_app(date_range=NO_MERGES)
    assert any("nothing to split" in i.value for i in at.info)
    assert not any("Stage" in table.value.columns for table in at.dataframe)  # no breakdown table


# ---------- review funnel ----------

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
    assert "never approved" in text and "still open when the data was pulled" in text
