"""Save the real GitHub pull as two CSV files, so tests can use the same real data on every machine.

    python fixtures/freeze_real_data.py dev.duckdb      # reads the raw tables, read-only; writes fixtures/frozen/*.csv

This was run once, on the pull of 2026-11-01 18:34 UTC (523 PRs, 835 reviews). The files are meant to stay exactly as they
are: the README's key findings are checked against them. Only re-run it on purpose, when the findings are rewritten for
newer data, and then update the numbers pinned in the tests. Both source tables are public GitHub data (encode/httpx).
"""

import csv
import sys
from pathlib import Path

import duckdb

FROZEN_DIR = Path(__file__).resolve().parent / "frozen"
PRS_COLUMNS = ["number", "title", "state", "merged", "author", "created_at", "updated_at", "merged_at", "closed_at", "_loaded_at"]
REVIEWS_COLUMNS = ["pr_number", "review_id", "reviewer_login", "review_state", "submitted_at", "_loaded_at"]


def _cell(value) -> str:
    if value is None:
        return ""  # the loader turns blanks back into NULL
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "isoformat"):  # _loaded_at, with its microseconds
        return value.isoformat(sep=" ")
    return str(value)


def _write(path: Path, columns, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([[_cell(v) for v in row] for row in rows])


def freeze(source: str | Path, directory: Path = FROZEN_DIR) -> dict:
    con = duckdb.connect(str(source), read_only=True)
    try:
        prs = con.execute(f"select {', '.join(PRS_COLUMNS)} from raw.github_pull_requests order by number").fetchall()
        reviews = con.execute(f"select {', '.join(REVIEWS_COLUMNS)} from raw.github_pr_reviews order by pr_number, review_id").fetchall()
    finally:
        con.close()
    directory.mkdir(exist_ok=True)
    _write(directory / "raw_pull_requests.csv", PRS_COLUMNS, prs)
    _write(directory / "raw_pr_reviews.csv", REVIEWS_COLUMNS, reviews)
    return {"pull_requests": len(prs), "reviews": len(reviews)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python fixtures/freeze_real_data.py <path-to-the-database-holding-the-real-raw-tables>")
    print(freeze(sys.argv[1]))
