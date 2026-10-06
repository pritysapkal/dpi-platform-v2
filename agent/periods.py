"""Turn time phrases ("last quarter", "Q1 2025", "last 30 days") into exact dates, in code.

Why this exists: in eval t03 the model was asked for "last quarter" on 2026-10-06 and
chose Q1 2026 instead of Q3 2026, even though the prompt gave it today's date. Date
arithmetic is something code does reliably and LLMs don't, so the model now passes the
user's phrase and this module computes the dates.

Conventions (also what the eval ground truth assumes):
- Quarters are calendar quarters: Q1 = Jan-Mar ... Q4 = Oct-Dec.
- Weeks start on Monday.
- "this week/month/quarter/year" run from the period start up to today (to date).
- "last week/month/quarter/year" are the previous complete calendar period.
- "last N days/weeks/months" are rolling windows ending today, today included:
  last 7 days on 2026-10-06 = 2026-09-30 .. 2026-10-06.
"""

import calendar
import datetime
import re

from agent import db

MONTHS = {
    name.lower(): number
    for number in range(1, 13)
    for name in (calendar.month_name[number], calendar.month_abbr[number])
}

SUPPORTED = (
    "today, yesterday, this/last week, this/last month, this/last quarter, this/last year, "
    "last N days/weeks/months, 'Q1 2025', 'H1 2024' or 'first half of 2024', "
    "'October 2024' or '2024-10', '2024'"
)


class PeriodError(ValueError):
    """The phrase is not one we can resolve. We never guess."""


# ---------- date helpers ----------

def month_end(year: int, month: int) -> datetime.date:
    """Last day of a month. calendar.monthrange knows Oct has 31 days and Feb 2024 has 29."""
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def add_months(day: datetime.date, months: int) -> datetime.date:
    """Same day N months later/earlier, clamped to the month's length (Mar 31 - 1 month = Feb 28/29)."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def quarter_range(year: int, quarter: int) -> tuple[datetime.date, datetime.date]:
    first_month = 3 * (quarter - 1) + 1
    return datetime.date(year, first_month, 1), month_end(year, first_month + 2)


def quarter_of(day: datetime.date) -> int:
    return (day.month - 1) // 3 + 1


# ---------- parsing ----------

def _normalize(phrase: str) -> str:
    text = re.sub(r"\s+", " ", phrase.strip().lower()).strip(" ?.!,")
    text = re.sub(r"^(?:in|during|for|over|from) ", "", text)
    text = re.sub(r"^the ", "", text)
    text = re.sub(r"^(?:previous|prior)\b", "last", text)
    text = re.sub(r"^past\b", "last", text)
    return text


def parse_period(phrase: str, today: datetime.date) -> tuple[datetime.date, datetime.date, str]:
    """Return (start, end, label) for a phrase, or raise PeriodError. Pure: no database."""
    if not isinstance(phrase, str) or not phrase.strip():
        raise PeriodError("period phrase is empty")
    text = _normalize(phrase)

    if text == "today":
        return today, today, "today"
    if text == "yesterday":
        day = today - datetime.timedelta(days=1)
        return day, day, "yesterday"

    match = re.fullmatch(r"(this|last) (week|month|quarter|year)", text)
    if match:
        which, unit = match.groups()
        if unit == "week":
            monday = today - datetime.timedelta(days=today.weekday())
            if which == "this":
                return monday, today, "this week (to date)"
            return monday - datetime.timedelta(days=7), monday - datetime.timedelta(days=1), "last week"
        if unit == "month":
            if which == "this":
                return today.replace(day=1), today, "this month (to date)"
            previous = add_months(today.replace(day=1), -1)
            return previous, month_end(previous.year, previous.month), "last month"
        if unit == "quarter":
            year, quarter = today.year, quarter_of(today)
            if which == "this":
                return quarter_range(year, quarter)[0], today, f"this quarter, Q{quarter} {year} (to date)"
            year, quarter = (year - 1, 4) if quarter == 1 else (year, quarter - 1)
            start, end = quarter_range(year, quarter)
            return start, end, f"last quarter, Q{quarter} {year}"
        if which == "this":
            return datetime.date(today.year, 1, 1), today, f"this year, {today.year} (to date)"
        year = today.year - 1
        return datetime.date(year, 1, 1), datetime.date(year, 12, 31), f"last year, {year}"

    match = re.fullmatch(r"last (\d+) (day|week|month)s?", text)
    if match:
        n, unit = int(match.group(1)), match.group(2)
        if n < 1:
            raise PeriodError("N must be at least 1")
        if unit == "day":
            start = today - datetime.timedelta(days=n - 1)
        elif unit == "week":
            start = today - datetime.timedelta(days=7 * n - 1)
        else:
            start = add_months(today, -n) + datetime.timedelta(days=1)
        return start, today, f"last {n} {unit}{'s' if n > 1 else ''} (rolling, ending today)"

    match = re.fullmatch(r"q([1-4]) (?:of )?(\d{4})|(\d{4}) q([1-4])", text)
    if match:
        quarter = int(match.group(1) or match.group(4))
        year = int(match.group(2) or match.group(3))
        start, end = quarter_range(year, quarter)
        return start, end, f"Q{quarter} {year}"

    match = re.fullmatch(r"(?:h([12])|(first|second) half of) (\d{4})", text)
    if match:
        half = int(match.group(1)) if match.group(1) else (1 if match.group(2) == "first" else 2)
        year = int(match.group(3))
        if half == 1:
            return datetime.date(year, 1, 1), datetime.date(year, 6, 30), f"H1 {year}"
        return datetime.date(year, 7, 1), datetime.date(year, 12, 31), f"H2 {year}"

    match = re.fullmatch(r"([a-z]+) (\d{4})", text)
    if match and match.group(1) in MONTHS:
        year, month = int(match.group(2)), MONTHS[match.group(1)]
        return datetime.date(year, month, 1), month_end(year, month), f"{calendar.month_name[month]} {year}"

    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match and 1 <= int(match.group(2)) <= 12:
        year, month = int(match.group(1)), int(match.group(2))
        return datetime.date(year, month, 1), month_end(year, month), f"{calendar.month_name[month]} {year}"

    match = re.fullmatch(r"(?:year )?(\d{4})", text)
    if match:
        year = int(match.group(1))
        return datetime.date(year, 1, 1), datetime.date(year, 12, 31), str(year)

    raise PeriodError(f"Unrecognised period {phrase!r}. Supported: {SUPPORTED}.")


# ---------- with data coverage ----------

def data_coverage() -> dict:
    """First and last day with DORA data (the mart spans first to last merge)."""
    _, rows, _ = db.run_query(
        "select cast(min(metric_date) as date), cast(max(metric_date) as date) "
        "from main.mart_dora_metrics_daily"
    )
    first, last = rows[0]
    return {"min": first, "max": last}


def resolve_period(phrase: str, today: datetime.date | None = None, coverage: dict | None = None) -> dict:
    """Resolve a time phrase to exact dates and say whether DPIP has data for it.

    today and coverage are injectable so tests are deterministic and need no database.
    """
    today = today or datetime.date.today()
    try:
        start, end, label = parse_period(phrase, today)
    except PeriodError as exc:
        return {"ok": False, "error": str(exc)}

    coverage = coverage or data_coverage()
    start_s, end_s = start.isoformat(), end.isoformat()
    result = {
        "ok": True,
        "phrase": phrase,
        "label": label,
        "start_date": start_s,
        "end_date": end_s,
        "data_coverage": coverage,
    }
    # ISO date strings compare correctly as text.
    if end_s < coverage["min"] or start_s > coverage["max"]:
        result["has_data"] = False
        result["message"] = (
            f"No data for {label} ({start_s} to {end_s}). DPIP data covers "
            f"{coverage['min']} to {coverage['max']}; the last date with data is {coverage['max']}."
        )
    else:
        result["has_data"] = True
        if start_s < coverage["min"] or end_s > coverage["max"]:
            result["coverage_note"] = (
                f"Only part of {label} has data: DPIP data covers {coverage['min']} to {coverage['max']}."
            )
    return result


# ---------- finding periods mentioned in a question (used by the agent loop's check) ----------

_RELATIVE_RE = re.compile(
    r"\b(today|yesterday|(?:this|last|previous|prior) (?:week|month|quarter|year)"
    r"|(?:last|past) \d+ (?:days?|weeks?|months?))\b",
    re.IGNORECASE,
)
_EXPLICIT_RE = re.compile(
    r"\b(q[1-4] (?:of )?\d{4}|\d{4} q[1-4]|h[12] \d{4}|(?:first|second) half of \d{4}"
    r"|(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r") \d{4})\b"
    r"|(?<![\d-])(\d{4})(?![\d-])",
    re.IGNORECASE,
)


def periods_in_question(question: str, today: datetime.date) -> tuple[list[dict], list[dict]]:
    """(relative, explicit) periods mentioned in a question, each as {phrase, start_date, end_date}."""
    def resolve_all(matches):
        found = []
        for phrase in matches:
            try:
                start, end, _ = parse_period(phrase, today)
            except PeriodError:
                continue
            found.append({"phrase": phrase, "start_date": start.isoformat(), "end_date": end.isoformat()})
        return found

    relative = resolve_all(m.group(0) for m in _RELATIVE_RE.finditer(question))
    explicit = resolve_all(m.group(0) for m in _EXPLICIT_RE.finditer(question))
    return relative, explicit
