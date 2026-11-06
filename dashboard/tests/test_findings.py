"""The README's "Key findings" must match the data.

Runs every query in analyses/key_findings.sql against dev.duckdb and checks the results, then
checks that the numbers written in the README's Key findings section are the ones the queries
produce. If the data is refreshed and a finding changes, this fails until the README is updated.

Needs dev.duckdb. Run from the project root:  python -m pytest dashboard/tests/test_findings.py -v
"""

import datetime
import re
from pathlib import Path

import pytest

import queries

pytestmark = pytest.mark.skipif(not queries.DB_PATH.exists(), reason="dev.duckdb not built")

PROJECT = queries.DB_PATH.parent
SQL_FILE = PROJECT / "analyses" / "key_findings.sql"
README = PROJECT / "README.md"


def run_blocks() -> dict[str, list[tuple]]:
    """Each '-- @query name' block of the analysis file, with {{ ref('x') }} turned into main.x."""
    parts = re.split(r"^-- @query (\w+)\s*$", SQL_FILE.read_text(encoding="utf-8"), flags=re.M)
    results = {}
    for name, sql in zip(parts[1::2], parts[2::2]):
        sql = re.sub(r"\{\{\s*ref\('(\w+)'\)\s*\}\}", r"main.\1", sql)
        results[name] = queries.run(sql)
    return results


@pytest.fixture(scope="module")
def r():
    return run_blocks()


EXPECTED_BLOCKS = {
    "data_window",
    "f1_throughput_by_year", "f1_outcome_of_prs_by_year_opened", "f1_since_the_last_merge", "f1_backlog",
    "f2_review_coverage_by_year", "f2_unreviewed_merges_by_author",
    "f3_wait_vs_review", "f3_wait_by_year",
    "f4_lead_time_shape", "f4_old_dashboard_figure", "f4_bots_vs_humans", "f4_bot_share_by_year",
    "f5_review_concentration",
}


def test_every_block_runs_and_returns_rows(r):
    assert set(r) == EXPECTED_BLOCKS  # nothing dropped, renamed or left untested
    assert all(rows for rows in r.values())


def test_scope(r):
    assert r["data_window"] == [
        (datetime.date(2023, 1, 1), datetime.date(2026, 10, 1), datetime.date(2026, 2, 23), datetime.date(2026, 11, 1))
    ]


def test_finding_1_throughput_and_backlog(r):
    assert r["f1_throughput_by_year"] == [(2023, 123, 88, 29.4), (2024, 159, 92, 23.2), (2025, 108, 26, 22.8), (2026, 29, 1, 23.4)]
    assert r["f1_outcome_of_prs_by_year_opened"] == [
        (2023, 123, 89, 4, 30), (2024, 159, 93, 12, 54), (2025, 108, 24, 36, 48), (2026, 29, 1, 21, 7),
    ]
    assert round(100 * 89 / 123) == 72 and round(100 * 24 / 108) == 22
    assert r["f1_since_the_last_merge"] == [(datetime.date(2026, 2, 23), 8, 7)]
    assert r["f1_backlog"] == [(73, 45, 39, 372.0)]


def test_finding_2_review_coverage(r):
    assert r["f2_review_coverage_by_year"] == [(2023, 88, 88, 100.0), (2024, 92, 76, 82.6), (2025, 26, 18, 69.2), (2026, 1, 1, 100.0)]
    assert r["f2_unreviewed_merges_by_author"] == [(24, 22, 3)]


def test_finding_3_waiting_for_the_first_review(r):
    assert r["f3_wait_vs_review"] == [(183, 6.88, 0.56, 55.9, 60.7, 86.9, 54.6)]
    assert r["f3_wait_by_year"] == [(2023, 88, 3.6, 50.0), (2024, 76, 7.3, 43.5), (2025, 18, 191.8, 87.7), (2026, 1, 23.4, 100.0)]


def test_finding_4_lead_time_shape_and_bots(r):
    assert r["f4_lead_time_shape"] == [(288, 20.62, 235.64, 483.04, 52.4, 81.3, 23, 77.8)]
    assert r["f4_old_dashboard_figure"] == [(260.78,)]
    assert round(260.78 / 20.62) == 13 and round(100 * 23 / 288) == 8
    assert r["f4_bots_vs_humans"] == [(False, 207, 24.15, 789.7), (True, 81, 15.91, 114.5)]
    assert r["f4_bot_share_by_year"] == [(2023, 149, 61, 40.9), (2024, 109, 17, 15.6), (2025, 29, 3, 10.3), (2026, 1, 0, 0.0)]
    assert round(100 * 81 / 288) == 28


def test_finding_5_review_concentration(r):
    assert r["f5_review_concentration"] == [(335, 61, 158, 246, 291, 330, 136)]
    assert [round(100 * n / 335, 1) for n in (158, 246, 291)] == [47.2, 73.4, 86.9]
    assert round(100 * 136 / 330, 1) == 41.2


def findings_text() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Key findings")
    end = text.index("\n## ", start + 5)
    return text[start:end]


# Every figure the README quotes in its findings, as written there.
README_NUMBERS = [
    # scope
    "523 PRs, 288 merged, 835 reviews", "2023-01-01", "2026-11-01",
    # finding 1
    "88 (2023), 92 (2024), 26 (2025), 1 (2026", "2026-02-23", "123, 159, 108, 29", "72%", "22%", "36 are still",
    "73 human PRs are open", "372 days", "39 are older than a year", "45 have never had a review", "Seven human PRs",
    "29.4, 23.2, 22.8", "23.4 hours",
    # finding 2
    "100% in 2023 (88 of 88)", "82.6% in 2024 (76 of 92)", "69.2% in 2025 (18 of 26)", "24 human merges", "22 were",
    # finding 3
    "183", "6.9 h", "0.6 h", "54.6%", "55.9%", "60.7%", "86.9%", "3.6 h (2023, 88 PRs)", "7.3 h (2024, 76 PRs)",
    "191.8 h (2025, only 18 PRs",
    # finding 4
    "20.6 h", "52.4%", "81.3%", "23 PRs (8%)", "77.8%", "235.6 h", "483.0 h", "260.8 h", "81 of the 288 merged PRs (28%",
    "40.9% in 2023, 15.6% in 2024", "10.3% in 2025", "15.9 h", "114.5 h", "24.2 h", "789.7 h",
    # finding 5
    "335 PRs", "61 different people", "158 (47.2%)", "246 (73.4%)", "291 (86.9%)", "136 of the 330 approvals (41.2%)",
]


@pytest.mark.parametrize("quoted", README_NUMBERS)
def test_readme_quotes_this_number(quoted):
    squashed = " ".join(findings_text().split())  # undo the line wrapping
    assert quoted in squashed


def test_readme_findings_do_not_name_individuals():
    # the findings describe a repo's process; real people's logins stay out of the claims
    names = set(queries.top_authors(datetime.date(2023, 1, 1), datetime.date(2026, 11, 1), limit=300)["author"])
    names |= set(queries.top_reviewers(datetime.date(2023, 1, 1), datetime.date(2026, 11, 1), limit=300)["reviewers"]["reviewer"])
    text = findings_text()
    mentioned = [n for n in names if len(n) > 4 and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)]
    assert mentioned == []
