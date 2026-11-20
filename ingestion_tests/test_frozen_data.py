"""The frozen copy of the real pull of 2026-11-01 (fixtures/frozen/): its files and how they load.

The README's key findings are checked against this copy on every push, so it must never change by accident. Needs no
network and no existing database; every test builds what it needs in a temporary folder.
"""

import csv
import datetime
import hashlib
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

pytestmark = pytest.mark.no_db  # touches no database file (conftest.py checks it)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
sys.path.insert(0, str(FIXTURES))

import freeze_real_data  # noqa: E402
import load_fixture  # noqa: E402

FROZEN = FIXTURES / "frozen"
PULLED = datetime.datetime(2026, 11, 1, 18, 34, 18, 677215)

# The files are real data, pinned on purpose: change them only by re-freezing deliberately and rewriting the findings.
SHA256 = {
    "raw_pull_requests.csv": "5c5e3cca5926c7538feb845566a22ca996dc2112e632bf59957d0f179b8d9fac",
    "raw_pr_reviews.csv": "36e4e4653cc21aeab666bcc5a0bae51f7b7e49c93a3acedeb2e52bc763f849a6",
}  # of the content with Unix line endings (Windows may check files out with CRLF)


def rows(name):
    with (FROZEN / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize("name", SHA256)
def test_the_files_are_byte_for_byte_the_ones_that_were_frozen(name):
    content = bytes(b for b in (FROZEN / name).read_bytes() if b != 13)  # drop carriage returns: Windows may check files out with CRLF
    assert hashlib.sha256(content).hexdigest() == SHA256[name], "the frozen data changed; re-freeze only on purpose"


def test_the_files_have_the_columns_of_the_raw_tables():
    assert list(rows("raw_pull_requests.csv")[0]) == freeze_real_data.PRS_COLUMNS
    assert list(rows("raw_pr_reviews.csv")[0]) == freeze_real_data.REVIEWS_COLUMNS


def test_the_counts_and_the_single_load_time_are_those_of_the_2026_11_01_pull():
    prs, reviews = rows("raw_pull_requests.csv"), rows("raw_pr_reviews.csv")
    assert (len(prs), len(reviews)) == (523, 835)
    assert len({r["number"] for r in prs}) == 523 and len({r["review_id"] for r in reviews}) == 835
    assert {r["_loaded_at"] for r in prs + reviews} == {"2026-11-01 18:34:18.677215"}
    assert sum(r["merged"] == "true" for r in prs) == 288
    assert {r["pr_number"] for r in reviews} <= {r["number"] for r in prs}  # every review belongs to a PR in the copy


def test_loading_gives_the_production_types_and_turns_blanks_into_null(tmp_path):
    db = tmp_path / "frozen.duckdb"
    assert load_fixture.load(db, "frozen") == {"pull_requests": 523, "reviews": 835}
    con = duckdb.connect(str(db), read_only=True)
    types = {t: [tuple(r) for r in con.execute("select column_name, data_type from information_schema.columns where table_schema='raw' and table_name=? order by ordinal_position", [t]).fetchall()]
             for t in ("github_pull_requests", "github_pr_reviews")}
    assert [name for name, _ in types["github_pull_requests"]] == freeze_real_data.PRS_COLUMNS
    assert dict(types["github_pull_requests"])["created_at"] == "VARCHAR" and dict(types["github_pull_requests"])["_loaded_at"] == "TIMESTAMP"
    assert con.execute("select count(*) filter (where merged_at is null), count(*) filter (where closed_at is null), count(*) filter (where merged_at = '' or closed_at = '') from raw.github_pull_requests").fetchone() == (235, 76, 0)
    assert con.execute("select max(_loaded_at) from raw.github_pull_requests").fetchone()[0] == PULLED  # microseconds survive the round trip


def test_freezing_what_was_loaded_gives_the_same_files_back(tmp_path):
    db = tmp_path / "frozen.duckdb"
    load_fixture.load(db, "frozen")
    out = tmp_path / "again"
    assert freeze_real_data.freeze(db, out) == {"pull_requests": 523, "reviews": 835}
    for name in ("raw_pull_requests.csv", "raw_pr_reviews.csv"):
        assert (out / name).read_bytes().replace(b"\r\n", b"\n") == (FROZEN / name).read_bytes().replace(b"\r\n", b"\n")


def test_loading_twice_is_fine(tmp_path):
    db = tmp_path / "frozen.duckdb"
    load_fixture.load(db, "frozen")
    load_fixture.load(db, "frozen")
    assert duckdb.connect(str(db), read_only=True).execute("select count(*) from raw.github_pull_requests").fetchone()[0] == 523


def test_the_two_datasets_never_overwrite_each_other(tmp_path):
    a, b = tmp_path / "a.duckdb", tmp_path / "b.duckdb"
    load_fixture.load(a, "fixture")
    load_fixture.load(b, "frozen")
    with pytest.raises(load_fixture.RefusingToOverwrite):
        load_fixture.load(a, "frozen")
    with pytest.raises(load_fixture.RefusingToOverwrite):
        load_fixture.load(b, "fixture")
    assert duckdb.connect(str(a), read_only=True).execute("select count(*) from raw.github_pull_requests").fetchone()[0] == 50
    assert duckdb.connect(str(b), read_only=True).execute("select count(*) from raw.github_pull_requests").fetchone()[0] == 523


def test_the_command_line_takes_an_optional_dataset_name(tmp_path):
    db = tmp_path / "cli.duckdb"
    run = lambda *args: subprocess.run([sys.executable, "-B", str(FIXTURES / "load_fixture.py"), *args], capture_output=True, text=True)  # noqa: E731
    ok = run(str(db), "frozen")
    assert ok.returncode == 0 and "loaded 523 PRs and 835 reviews" in ok.stdout
    bad = run(str(tmp_path / "x.duckdb"), "nonsense")
    assert bad.returncode != 0 and "usage" in (bad.stdout + bad.stderr)
