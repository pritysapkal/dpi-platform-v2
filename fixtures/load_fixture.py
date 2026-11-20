"""Load the fixture CSVs into the `raw` schema of a DuckDB file, exactly where `fetch_prs.py` puts live data.

After this, `dbt build` runs on the fixture the same way it runs on live data: the staging models, the fact table,
the tests and the unit tests do not know the difference.

    python fixtures/load_fixture.py dev.duckdb

Safety: this REPLACES the two raw tables. So it refuses to touch a database whose raw tables hold anything that is not
this fixture (for example your real `dev.duckdb`), and the path is a required argument with no default. A fixture
database can be reloaded freely; a database with real data cannot be overwritten by accident.
"""

import csv
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_fixture as fixture  # noqa: E402

# The same column types `fetch_prs.py` produces (dates are text there; only `_loaded_at` is a real timestamp).
PRS_DDL = """
    create table raw.github_pull_requests (
        number bigint, title varchar, state varchar, merged boolean, author varchar,
        created_at varchar, updated_at varchar, merged_at varchar, closed_at varchar, _loaded_at timestamp
    )
"""
REVIEWS_DDL = """
    create table raw.github_pr_reviews (
        pr_number bigint, review_id bigint, reviewer_login varchar, review_state varchar,
        submitted_at varchar, _loaded_at timestamp
    )
"""


class RefusingToOverwrite(RuntimeError):
    """The target database holds data that is not this fixture."""


def _read(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def _blank_to_none(value: str):
    return value if value != "" else None


def check_safe_to_replace(con) -> None:
    """Raise unless the raw tables are absent, empty, or hold only the fixture (recognised by its load timestamp)."""
    for table in ("github_pull_requests", "github_pr_reviews"):
        exists = con.execute(
            "select count(*) from information_schema.tables where table_schema = 'raw' and table_name = ?", [table]
        ).fetchone()[0]
        if not exists:
            continue
        foreign = con.execute(
            f"select count(*) from raw.{table} where _loaded_at is null or _loaded_at <> cast(? as timestamp)",
            [fixture.LOADED_AT],
        ).fetchone()[0]
        if foreign:
            raise RefusingToOverwrite(
                f"raw.{table} holds {foreign} row(s) that are not fixture data (real data?). Refusing to replace it. "
                "Point this at a new file, or delete the database first if you really mean to."
            )


def load(db_path: str | Path, directory: Path = fixture.FIXTURE_DIR) -> dict:
    """Create (or refresh) the two raw tables from the CSVs. Returns the row counts."""
    con = duckdb.connect(str(db_path))
    try:
        check_safe_to_replace(con)
        con.execute("create schema if not exists raw")
        con.execute("drop table if exists raw.github_pull_requests")
        con.execute("drop table if exists raw.github_pr_reviews")
        con.execute(PRS_DDL)
        con.execute(REVIEWS_DDL)
        prs = [
            (int(r["number"]), r["title"], r["state"], r["merged"] == "true", r["author"], r["created_at"], r["updated_at"],
             _blank_to_none(r["merged_at"]), _blank_to_none(r["closed_at"]), r["_loaded_at"])
            for r in _read(directory / fixture.PRS_CSV.name)
        ]
        reviews = [
            (int(r["pr_number"]), int(r["review_id"]), r["reviewer_login"], r["review_state"], r["submitted_at"], r["_loaded_at"])
            for r in _read(directory / fixture.REVIEWS_CSV.name)
        ]
        con.executemany("insert into raw.github_pull_requests values (?, ?, ?, ?, ?, ?, ?, ?, ?, cast(? as timestamp))", prs)
        con.executemany("insert into raw.github_pr_reviews values (?, ?, ?, ?, ?, cast(? as timestamp))", reviews)
        return {"pull_requests": len(prs), "reviews": len(reviews)}
    finally:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python fixtures/load_fixture.py <path-to-duckdb-file>   (no default, on purpose)")
    try:
        counts = load(sys.argv[1])
    except RefusingToOverwrite as problem:
        sys.exit(f"refused: {problem}")
    print(f"loaded {counts['pull_requests']} PRs and {counts['reviews']} reviews into raw.* of {sys.argv[1]}")
