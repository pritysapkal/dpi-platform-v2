"""Run the real Streamlit app headlessly (no browser) and read what it shows."""

import datetime

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
