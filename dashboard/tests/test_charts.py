"""The count-bar builder: integer axes and wide bars, checked on the chart specification itself."""

import json

import pandas as pd
import pytest

import charts

pytestmark = pytest.mark.no_db  # touches no database file (conftest.py checks it)


def spec(chart) -> dict:
    return json.loads(chart.to_json())


SMALL = pd.DataFrame({"stage": ["Opened", "Reviewed", "Approved"], "PRs": [1, 1, 0]})  # the kind of data that gave 0.5 ticks


@pytest.mark.parametrize("horizontal", [False, True], ids=["vertical", "horizontal"])
def test_count_axis_is_whole_numbers_starting_at_zero(horizontal):
    s = spec(charts.count_bar(SMALL, "stage", "PRs", category_title="stage", value_title="PRs", horizontal=horizontal))
    count_channel = s["encoding"]["x" if horizontal else "y"]
    assert count_channel["field"] == "PRs" and count_channel["type"] == "quantitative"
    assert count_channel["axis"]["format"] == "d"  # integer labels: never 0.5 or 1.50
    assert count_channel["axis"]["tickMinStep"] == 1  # and no tick between two whole numbers
    assert count_channel["scale"]["domainMin"] == 0


@pytest.mark.parametrize("horizontal", [False, True], ids=["vertical", "horizontal"])
def test_categories_keep_the_given_order_on_a_band_axis_with_little_padding(horizontal):
    s = spec(charts.count_bar(SMALL, "stage", "PRs", category_title="stage", value_title="PRs", horizontal=horizontal))
    category_channel = s["encoding"]["y" if horizontal else "x"]
    assert category_channel["type"] == "ordinal"  # one equal slot per bar, not a time axis
    assert category_channel["sort"] is None  # the order of the rows, not alphabetical
    assert category_channel["scale"]["paddingInner"] <= 0.2  # bars fill their slots, so they are wide


def test_bars_are_not_given_a_fixed_tiny_width():
    s = spec(charts.count_bar(SMALL, "stage", "PRs", category_title="stage", value_title="PRs"))
    assert s["mark"]["type"] == "bar" and "size" not in s["mark"]  # width comes from the band, so it adapts to the page


def test_horizontal_charts_get_a_row_height_per_bar_and_vertical_ones_a_fixed_height():
    assert spec(charts.count_bar(SMALL, "stage", "PRs", category_title="s", value_title="p", horizontal=True))["height"] == {"step": charts.HORIZONTAL_BAR_STEP}
    assert spec(charts.count_bar(SMALL, "stage", "PRs", category_title="s", value_title="p"))["height"] == charts.VERTICAL_HEIGHT


def test_the_chart_carries_the_data_it_was_given():
    s = spec(charts.count_bar(SMALL, "stage", "PRs", category_title="stage", value_title="PRs"))
    rows = s["datasets"][next(iter(s["datasets"]))]
    assert [(r["stage"], r["PRs"]) for r in rows] == [("Opened", 1), ("Reviewed", 1), ("Approved", 0)]


def test_period_labels_for_months_and_weeks():
    starts = pd.Series(pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-04"]))
    assert charts.period_labels(starts, "month").tolist() == ["2024-01", "2024-02", "2024-03"]
    assert charts.period_labels(starts, "week").tolist() == ["2024-01-01", "2024-02-01", "2024-03-04"]
    with pytest.raises(ValueError, match="grain"):
        charts.period_labels(starts, "day")
