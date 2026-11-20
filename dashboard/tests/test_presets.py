"""Date presets: pure functions, no database and no Streamlit."""

import datetime

import pytest

import presets

pytestmark = pytest.mark.no_db  # touches no database file (conftest.py checks it)

D = datetime.date
FIRST, LAST = D(2023, 1, 1), D(2026, 11, 1)


def test_the_buttons_and_their_ranges():
    assert presets.presets(FIRST, LAST) == {
        "All time": (D(2023, 1, 1), D(2026, 11, 1)),
        "2023": (D(2023, 1, 1), D(2023, 12, 31)),
        "2024": (D(2024, 1, 1), D(2024, 12, 31)),
        "2025": (D(2025, 1, 1), D(2025, 12, 31)),
        "Last 12 months": (D(2025, 11, 2), D(2026, 11, 1)),
    }


def test_last_12_months_is_exactly_a_year_ending_on_the_day_the_data_was_pulled():
    start, end = presets.presets(FIRST, LAST)["Last 12 months"]
    assert end == LAST and (end - start).days + 1 == 365


def test_last_12_months_handles_leap_days():
    start, end = presets.presets(D(2020, 1, 1), D(2024, 2, 29))["Last 12 months"]
    assert (start, end) == (D(2023, 3, 1), D(2024, 2, 29))
    start, end = presets.presets(D(2020, 1, 1), D(2025, 3, 1))["Last 12 months"]
    assert (start, end) == (D(2024, 3, 2), D(2025, 3, 1))


def test_ranges_never_leave_the_data_window():
    short = presets.presets(D(2024, 6, 1), D(2025, 3, 15))
    assert short["All time"] == (D(2024, 6, 1), D(2025, 3, 15))
    assert short["2024"] == (D(2024, 6, 1), D(2024, 12, 31))  # only the part the data covers
    assert short["2025"] == (D(2025, 1, 1), D(2025, 3, 15))
    assert "2023" not in short  # a year entirely before the data is not offered
    assert short["Last 12 months"] == (D(2024, 6, 1), D(2025, 3, 15))  # the data is shorter than a year
    for start, end in short.values():
        assert D(2024, 6, 1) <= start <= end <= D(2025, 3, 15)


@pytest.mark.parametrize(
    "span, expected",
    [
        ((D(2023, 5, 1), D(2023, 6, 1)), (D(2023, 5, 1), D(2023, 6, 1))),  # already inside
        ((D(2022, 1, 1), D(2023, 6, 1)), (D(2023, 1, 1), D(2023, 6, 1))),  # starts too early
        ((D(2025, 1, 1), D(2030, 1, 1)), (D(2025, 1, 1), D(2026, 11, 1))),  # ends too late
        ((D(2020, 1, 1), D(2020, 2, 1)), (D(2023, 1, 1), D(2026, 11, 1))),  # entirely outside: whole window
        ((D(2024, 5, 1), D(2024, 4, 1)), (D(2024, 4, 1), D(2024, 5, 1))),  # given backwards
    ],
)
def test_clamp(span, expected):
    assert presets.clamp(span, FIRST, LAST) == expected
