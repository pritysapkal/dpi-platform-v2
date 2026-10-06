"""Period resolution tests. Deterministic: `today` and data coverage are injected,
so no database and no API are needed. Expected dates are written out by hand."""

import datetime

import pytest

from agent.periods import parse_period, periods_in_question, resolve_period

TODAY = datetime.date(2026, 10, 6)  # a Tuesday, in Q4 2026
COVERAGE = {"min": "2023-02-06", "max": "2026-02-23"}


def dates(phrase, today=TODAY):
    start, end, _ = parse_period(phrase, today)
    return start.isoformat(), end.isoformat()


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("today", ("2026-10-06", "2026-10-06")),
        ("yesterday", ("2026-10-05", "2026-10-05")),
        ("this week", ("2026-10-05", "2026-10-06")),  # Monday to today
        ("last week", ("2026-09-28", "2026-10-04")),  # previous Monday to Sunday
        ("this month", ("2026-10-01", "2026-10-06")),
        ("last month", ("2026-09-01", "2026-09-30")),
        ("this quarter", ("2026-10-01", "2026-10-06")),
        ("last quarter", ("2026-07-01", "2026-09-30")),  # the t03 case: Q3, not Q1
        ("this year", ("2026-01-01", "2026-10-06")),
        ("last year", ("2025-01-01", "2025-12-31")),
        ("last 7 days", ("2026-09-30", "2026-10-06")),
        ("last 30 days", ("2026-09-07", "2026-10-06")),
        ("last 1 day", ("2026-10-06", "2026-10-06")),
        ("last 2 weeks", ("2026-09-23", "2026-10-06")),
        ("last 3 months", ("2026-07-07", "2026-10-06")),
        ("last 12 months", ("2025-10-07", "2026-10-06")),
        ("Q1 2025", ("2025-01-01", "2025-03-31")),
        ("q4 2024", ("2024-10-01", "2024-12-31")),
        ("2025 Q2", ("2025-04-01", "2025-06-30")),
        ("Q3 of 2024", ("2024-07-01", "2024-09-30")),
        ("H1 2024", ("2024-01-01", "2024-06-30")),
        ("second half of 2024", ("2024-07-01", "2024-12-31")),
        ("October 2024", ("2024-10-01", "2024-10-31")),  # October has 31 days
        ("sep 2024", ("2024-09-01", "2024-09-30")),
        ("2024-11", ("2024-11-01", "2024-11-30")),
        ("2024", ("2024-01-01", "2024-12-31")),
    ],
)
def test_supported_phrases(phrase, expected):
    assert dates(phrase) == expected


@pytest.mark.parametrize(
    "phrase",
    ["the last quarter", "Last Quarter?", "previous quarter", "in the last quarter", "  last   quarter "],
)
def test_phrasing_variants_mean_the_same(phrase):
    assert dates(phrase) == ("2026-07-01", "2026-09-30")


@pytest.mark.parametrize("phrase", ["past 30 days", "last 30 days"])
def test_past_is_a_synonym_for_last(phrase):
    assert dates(phrase) == ("2026-09-07", "2026-10-06")


# ---------- boundaries ----------

def test_last_quarter_in_january_is_previous_years_q4():
    jan_5 = datetime.date(2026, 1, 5)
    assert dates("last quarter", jan_5) == ("2025-10-01", "2025-12-31")
    assert dates("this quarter", jan_5) == ("2026-01-01", "2026-01-05")


def test_year_boundary_month_and_week():
    jan_5 = datetime.date(2026, 1, 5)  # a Monday
    assert dates("last month", jan_5) == ("2025-12-01", "2025-12-31")
    assert dates("last week", jan_5) == ("2025-12-29", "2026-01-04")
    assert dates("this week", jan_5) == ("2026-01-05", "2026-01-05")
    assert dates("last year", jan_5) == ("2025-01-01", "2025-12-31")


def test_leap_year_february():
    assert dates("last month", datetime.date(2024, 3, 15)) == ("2024-02-01", "2024-02-29")
    assert dates("last month", datetime.date(2025, 3, 15)) == ("2025-02-01", "2025-02-28")
    assert dates("February 2024") == ("2024-02-01", "2024-02-29")
    assert dates("Q1 2024") == ("2024-01-01", "2024-03-31")


def test_rolling_month_clamps_to_short_months():
    # one month before Mar 31, 2024 is Feb 29 (clamped), so the window starts Mar 1
    assert dates("last 1 month", datetime.date(2024, 3, 31)) == ("2024-03-01", "2024-03-31")


def test_last_month_has_correct_31_day_end():
    assert dates("last month", datetime.date(2026, 11, 3)) == ("2026-10-01", "2026-10-31")


# ---------- unrecognised phrases: error, never a guess ----------

@pytest.mark.parametrize(
    "phrase", ["next sprint", "", "   ", "Q5 2024", "last fortnight", "2024-13", "since the release", "last 0 days", None]
)
def test_unrecognised_phrase_returns_error(phrase):
    result = resolve_period(phrase, today=TODAY, coverage=COVERAGE)
    assert result["ok"] is False
    assert "error" in result and "start_date" not in result


# ---------- data coverage ----------

def test_last_quarter_has_no_data_and_cites_last_date():
    result = resolve_period("last quarter", today=TODAY, coverage=COVERAGE)
    assert result["ok"] is True
    assert (result["start_date"], result["end_date"]) == ("2026-07-01", "2026-09-30")
    assert result["has_data"] is False
    assert "2026-02-23" in result["message"]
    assert result["data_coverage"] == COVERAGE


def test_period_inside_coverage_has_data():
    result = resolve_period("2024", today=TODAY, coverage=COVERAGE)
    assert result["has_data"] is True and "coverage_note" not in result


def test_partially_covered_period_has_data_with_note():
    result = resolve_period("this year", today=TODAY, coverage=COVERAGE)  # data stops 2026-02-23
    assert result["has_data"] is True and "2026-02-23" in result["coverage_note"]


def test_result_shape():
    result = resolve_period("Q1 2025", today=TODAY, coverage=COVERAGE)
    assert set(result) >= {"ok", "start_date", "end_date", "label", "has_data", "data_coverage"}
    assert result["label"] == "Q1 2025"


# ---------- finding periods in a question (used by the loop's date check) ----------

def test_periods_in_question_finds_relative_and_explicit():
    relative, explicit = periods_in_question("Compare last quarter with Q1 2025 and with 2024", TODAY)
    assert [(p["start_date"], p["end_date"]) for p in relative] == [("2026-07-01", "2026-09-30")]
    assert [(p["start_date"], p["end_date"]) for p in explicit] == [
        ("2025-01-01", "2025-03-31"),
        ("2024-01-01", "2024-12-31"),
    ]


def test_periods_in_question_ignores_full_dates():
    relative, explicit = periods_in_question("Merges from 2024-03-01 to 2024-04-01?", TODAY)
    assert relative == [] and explicit == []
