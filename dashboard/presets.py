"""Quick date-range choices for the sidebar, and a guard that keeps a range inside the data.

Plain functions with no Streamlit in them, so they can be tested directly. Every range is
clamped to the days the data actually covers: asking for 2023 when the data starts on
2023-01-01 gives exactly that, and asking for a year the data only partly covers gives the
covered part.
"""

import datetime

YEARS = (2023, 2024, 2025)  # whole-year buttons; partial years (like 2026) are left to the date picker

ALL_TIME = "All time"
LAST_12_MONTHS = "Last 12 months"


def clamp(span: tuple[datetime.date, datetime.date], first_day: datetime.date, last_day: datetime.date):
    """The part of (start, end) that lies inside [first_day, last_day]; the whole data window if none of it does."""
    start, end = min(span), max(span)
    start, end = max(start, first_day), min(end, last_day)
    return (first_day, last_day) if start > end else (start, end)


def one_year_before(day: datetime.date) -> datetime.date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 February has no equivalent a year earlier
        return day.replace(year=day.year - 1, day=28)


def presets(first_day: datetime.date, last_day: datetime.date) -> dict[str, tuple[datetime.date, datetime.date]]:
    """Label -> (start, end), in the order the buttons are shown.

    "Last 12 months" ends on last_day, the day the data was pulled, not on today's date: the data is only as
    fresh as the last pull, so the button always selects the same window until the data is refreshed.
    """
    choices = {ALL_TIME: (first_day, last_day)}
    for year in YEARS:
        span = (datetime.date(year, 1, 1), datetime.date(year, 12, 31))
        if span[1] >= first_day and span[0] <= last_day:  # only offer years the data touches
            choices[str(year)] = clamp(span, first_day, last_day)
    choices[LAST_12_MONTHS] = clamp((one_year_before(last_day) + datetime.timedelta(days=1), last_day), first_day, last_day)
    return choices
