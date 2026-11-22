"""Incremental loading gives the same warehouse as a full rebuild.

For the fixture and for the frozen real pull, each test makes its own database files (never the project's), and:
  1. loads the dataset, then turns it back into an EARLIER state: the most recently updated PRs are removed (they arrive
     later, as new or updated PRs), and the latest review of an old PR is removed (it arrives later: a late review); the
     remaining rows are stamped as loaded by a full fetch ten days before the dataset's real load time, and a quiet
     incremental fetch one day before it (which wrote nothing) is logged;
  2. builds it with `dbt build` (a first, full build);
  3. writes the missing rows with fetch_prs.upsert, exactly as an incremental fetch does, stamped with the real load time;
  4. builds again: this time fct_pull_requests runs incrementally;
then requires the result to be identical, table for table and row for row, to `dbt run --full-refresh` on the same raw
data, and to a plain full build of the dataset loaded from scratch.

Marked no_db: they never open the database the other tests read, only files under pytest's tmp_path.
"""

import datetime
import os
import shutil
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fixtures"))

import fetch_prs  # noqa: E402
import load_fixture  # noqa: E402

pytestmark = pytest.mark.no_db

MARTS = ["fct_pull_requests", "dim_contributors", "dim_date", "mart_dora_metrics_daily"]
DBT = Path(sys.executable).with_name("dbt.exe" if os.name == "nt" else "dbt")
RECENT = {"fixture": 8, "frozen": 40}  # how many of the most recently updated PRs arrive in the incremental fetch


def dbt(db: Path, tmp: Path, *args: str) -> str:
    import subprocess

    env = {**os.environ, "DPIP_DB_PATH": str(db), "DBT_PROFILES_DIR": str(ROOT / "profiles"), "PYTHONIOENCODING": "utf-8"}
    command = [str(DBT if DBT.exists() else shutil.which("dbt")), *args, "--target-path", str(tmp / "target"), "--log-path", str(tmp / "logs")]
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    return result.stdout


def rows(db: Path, sql: str, params=()):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def assert_same_marts(a: Path, b: Path):
    con = duckdb.connect(str(a), read_only=True)
    try:
        con.execute(f"attach '{b.as_posix()}' as other (read_only)")
        for table in MARTS:
            left, right = f"main.{table}", f"other.main.{table}"
            for x, y in ((left, right), (right, left)):
                (extra,) = con.execute(f"select count(*) from (select * from {x} except all select * from {y})").fetchone()
                assert extra == 0, f"{table}: {extra} row(s) in {x} that are not in {y}"
            counts = [con.execute(f"select count(*) from {t}").fetchone()[0] for t in (left, right)]
            assert counts[0] == counts[1] > 0, (table, counts)
    finally:
        con.close()


class Scenario:
    """A dataset taken back to an earlier state, plus the rows a later incremental fetch brings."""

    def __init__(self, tmp: Path, dataset: str):
        self.tmp, self.dataset = tmp, dataset
        self.db = tmp / "incremental.duckdb"
        load_fixture.load(self.db, dataset)
        con = duckdb.connect(str(self.db))
        try:
            (self.t1,) = con.execute("select max(loaded_at) from raw.github_fetch_runs").fetchone()
            self.t_full = self.t1 - datetime.timedelta(days=10)  # the original full fetch
            self.t0 = self.t1 - datetime.timedelta(days=1)  # yesterday's incremental fetch, which found nothing new
            self.recent = [n for (n,) in con.execute(
                "select number from raw.github_pull_requests order by updated_at desc, number desc limit ?", [RECENT[dataset]]).fetchall()]
            # the oldest PR that has a review and is not among the recent ones: it gets a late review
            self.late_pr, self.late_review = con.execute(
                """select r.pr_number, r.review_id from raw.github_pr_reviews r
                   join raw.github_pull_requests p on p.number = r.pr_number
                   where r.pr_number not in (select unnest(?::bigint[]))
                   order by p.created_at, r.submitted_at desc, r.review_id desc limit 1""", [self.recent]).fetchone()
            changed = self.recent + [self.late_pr]
            self.delta_prs = con.execute("select * exclude (_loaded_at) from raw.github_pull_requests where number in (select unnest(?::bigint[]))", [changed]).df()
            self.delta_reviews = con.execute("select * exclude (_loaded_at) from raw.github_pr_reviews where pr_number in (select unnest(?::bigint[]))", [changed]).df()
            # the earlier state
            con.execute("delete from raw.github_pr_reviews where pr_number in (select unnest(?::bigint[])) or review_id = ?", [self.recent, self.late_review])
            con.execute("delete from raw.github_pull_requests where number in (select unnest(?::bigint[]))", [self.recent])
            con.execute("update raw.github_pull_requests set _loaded_at = ?", [self.t_full])
            con.execute("update raw.github_pr_reviews set _loaded_at = ?", [self.t_full])
            con.execute("delete from raw.github_fetch_runs")
            con.execute("insert into raw.github_fetch_runs values (?, 'full', 0, 0), (?, 'incremental', 0, 0)", [self.t_full, self.t0])
        finally:
            con.close()

    def incremental_fetch(self):
        con = duckdb.connect(str(self.db))
        try:
            fetch_prs.upsert(con, self.delta_prs, self.delta_reviews, self.t1, "incremental")
        finally:
            con.close()


def late_pr_reviews(db, pr):
    return rows(db, "select review_count, last_reviewed_at from main.fct_pull_requests where pr_number = ?", [pr])[0]


@pytest.mark.parametrize("dataset", ["fixture", "frozen"])
def test_an_incremental_build_gives_the_same_tables_as_a_full_refresh_and_as_a_build_from_scratch(tmp_path, dataset):
    s = Scenario(tmp_path, dataset)
    first = dbt(s.db, tmp_path, "build")
    assert "OK created sql incremental model main.fct_pull_requests" in first
    before = late_pr_reviews(s.db, s.late_pr)
    assert rows(s.db, "select count(*) from main.fct_pull_requests where pr_number in (select unnest(?::bigint[]))", [s.recent]) == [(0,)]

    s.incremental_fetch()
    second = dbt(s.db, tmp_path, "build")  # incremental this time; every data test and unit test runs again on the result
    assert "OK created sql incremental model main.fct_pull_requests" in second and "ERROR=0" in second

    # the late review on the old PR was picked up, and the new and updated PRs are there
    after = late_pr_reviews(s.db, s.late_pr)
    assert after[0] == before[0] + 1 and (before[1] is None or after[1] > before[1])
    assert rows(s.db, "select count(*) from main.fct_pull_requests where pr_number in (select unnest(?::bigint[]))", [s.recent]) == [(len(s.recent),)]
    assert rows(s.db, "select count(distinct data_as_of), max(data_as_of) from main.fct_pull_requests") == [(1, s.t1)]

    # the same raw data rebuilt with --full-refresh
    full = tmp_path / "full_refresh.duckdb"
    shutil.copyfile(s.db, full)
    dbt(full, tmp_path, "run", "--full-refresh")
    assert_same_marts(s.db, full)

    # the dataset loaded from scratch and built in full (every raw row stamped with the real load time)
    scratch = tmp_path / "from_scratch.duckdb"
    load_fixture.load(scratch, dataset)
    dbt(scratch, tmp_path, "run")
    assert_same_marts(s.db, scratch)


def test_an_incremental_run_leaves_rows_it_did_not_need_to_recompute_alone(tmp_path):
    """Proves the build really was incremental: a marked row of an untouched PR survives it, a changed PR is rewritten."""
    s = Scenario(tmp_path, "fixture")
    dbt(s.db, tmp_path, "run")
    untouched = rows(s.db, "select min(pr_number) from main.fct_pull_requests where pr_number <> ?", [s.late_pr])[0][0]
    con = duckdb.connect(str(s.db))
    con.execute("update main.fct_pull_requests set pr_title = 'MARK' where pr_number in (?, ?)", [untouched, s.late_pr])
    con.close()
    s.incremental_fetch()
    dbt(s.db, tmp_path, "run", "--select", "fct_pull_requests")
    titles = dict(rows(s.db, "select pr_number, pr_title from main.fct_pull_requests where pr_number in (?, ?)", [untouched, s.late_pr]))
    assert titles[untouched] == "MARK"  # not recomputed
    assert titles[s.late_pr] != "MARK"  # recomputed from its rows
    # a full refresh puts the untouched row right again
    dbt(s.db, tmp_path, "run", "--select", "fct_pull_requests", "--full-refresh")
    assert rows(s.db, "select count(*) from main.fct_pull_requests where pr_title = 'MARK'") == [(0,)]


# ---------- the write path of the fetch ----------

def raw_db(tmp_path):
    con = duckdb.connect(str(tmp_path / "fetched.duckdb"))  # not "raw.duckdb": that catalog name would clash with the raw schema
    fetch_prs.ensure_raw_tables(con)
    return con


def prs_frame(*numbers, updated="2026-01-01T00:00:00Z"):
    return fetch_prs.pr_rows([{"number": n, "title": f"PR {n}", "state": "open", "user": {"login": "a"}, "created_at": "2025-01-01T00:00:00Z",
                               "updated_at": updated, "merged_at": None, "closed_at": None} for n in numbers])


def reviews_frame(*pairs):
    return pd.DataFrame([{"pr_number": pr, "review_id": rid, "reviewer_login": "b", "review_state": "COMMENTED", "submitted_at": "2026-01-01T00:00:00Z"}
                         for pr, rid in pairs], columns=fetch_prs.REVIEW_COLUMNS)


def test_upsert_replaces_prs_by_number_and_all_reviews_of_those_prs_and_logs_the_run(tmp_path):
    con = raw_db(tmp_path)
    t0, t1 = datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 2)
    fetch_prs.upsert(con, prs_frame(1, 2), reviews_frame((1, 10), (1, 11), (2, 20)), t0, "full")
    # PR 1 again: review 11 was deleted on GitHub, review 12 is new; PR 2 not fetched this time
    fetch_prs.upsert(con, prs_frame(1, updated="2026-01-02T00:00:00Z"), reviews_frame((1, 10), (1, 12)), t1, "incremental")
    assert con.execute("select number, updated_at, _loaded_at from raw.github_pull_requests order by 1").fetchall() == [
        (1, "2026-01-02T00:00:00Z", t1), (2, "2026-01-01T00:00:00Z", t0)]
    assert con.execute("select pr_number, review_id, _loaded_at from raw.github_pr_reviews order by 2").fetchall() == [
        (1, 10, t1), (1, 12, t1), (2, 20, t0)]
    assert con.execute("select * from raw.github_fetch_runs order by 1").fetchall() == [(t0, "full", 2, 3), (t1, "incremental", 1, 2)]


def test_a_run_that_finds_nothing_still_records_that_it_ran(tmp_path):
    con = raw_db(tmp_path)
    fetch_prs.upsert(con, prs_frame(), reviews_frame(), datetime.datetime(2026, 1, 3), "incremental")
    assert con.execute("select count(*) from raw.github_pull_requests").fetchone() == (0,)
    assert con.execute("select prs_written, reviews_written from raw.github_fetch_runs").fetchall() == [(0, 0)]


def test_a_failed_write_changes_nothing(tmp_path):
    con = raw_db(tmp_path)
    t0 = datetime.datetime(2026, 1, 1)
    fetch_prs.upsert(con, prs_frame(1), reviews_frame((1, 10)), t0, "full")
    broken = reviews_frame((1, 11))
    broken["review_id"] = "not a number"
    with pytest.raises(duckdb.Error):
        fetch_prs.upsert(con, prs_frame(1, updated="2026-01-09T00:00:00Z"), broken, datetime.datetime(2026, 1, 9), "incremental")
    assert con.execute("select updated_at from raw.github_pull_requests").fetchall() == [("2026-01-01T00:00:00Z",)]
    assert con.execute("select review_id from raw.github_pr_reviews").fetchall() == [(10,)]
    assert con.execute("select count(*) from raw.github_fetch_runs").fetchone() == (1,)


def test_the_fetch_never_recreates_the_raw_tables():
    import ast

    source = (ROOT / "fetch_prs.py").read_text(encoding="utf-8")
    code = source.replace(ast.get_docstring(ast.parse(source), clean=False), "")  # the docstring itself says it never does
    assert "create or replace" not in code.lower() and "drop table" not in code.lower()


def test_the_watermark_is_the_last_fetch_minus_three_days(tmp_path):
    con = raw_db(tmp_path)
    assert fetch_prs.watermark(con) is None  # nothing fetched yet: a full fetch
    con.execute("insert into raw.github_pull_requests (number, _loaded_at) values (1, '2026-01-05 00:00:00')")
    assert fetch_prs.watermark(con) == datetime.datetime(2026, 1, 2)  # an older database without the fetch log
    con.execute("insert into raw.github_fetch_runs values ('2026-01-10 12:00:00', 'incremental', 0, 0), ('2026-01-08 00:00:00', 'full', 1, 0)")
    assert fetch_prs.watermark(con) == datetime.datetime(2026, 1, 7, 12)


class FakeUpdatedApi:
    def __init__(self, *pages):
        self.pages, self.requested = list(pages), []

    def get(self, url, headers, params):
        assert (params["sort"], params["direction"], params["state"]) == ("updated", "desc", "all")
        self.requested.append(params["page"])
        index = params["page"] - 1

        class Response:
            def json(_self):
                return self.pages[index] if index < len(self.pages) else []
        return Response()


def upd(number, updated, created="2024-01-01"):
    return {"number": number, "updated_at": updated, "created_at": created + "T00:00:00Z"}


def test_the_incremental_fetch_pages_by_last_update_and_stops_at_the_watermark():
    api = FakeUpdatedApi(
        [upd(9, "2026-01-10T00:00:00Z"), upd(3, "2026-01-09T00:00:00Z", created="2022-05-01"), upd(8, "2026-01-08T00:00:00Z")],
        [upd(8, "2026-01-08T00:00:00Z"), upd(7, "2026-01-07T00:00:00Z"), upd(6, "2026-01-06T23:59:59Z"), upd(5, "2026-01-09T00:00:00Z")],
        [upd(4, "2026-01-01T00:00:00Z")],  # must never be requested
    )
    got = fetch_prs.fetch_prs_updated_since("encode", "httpx", datetime.datetime(2026, 1, 7), "2023-01-01", {}, get=api.get)
    # 3 was created before the window: skipped, paging goes on; 8 appears twice (pages shifted); 6 is older than the watermark: stop
    assert [p["number"] for p in got] == [9, 8, 7]
    assert api.requested == [1, 2]


def test_main_does_a_full_fetch_first_then_incremental_ones_and_full_when_asked(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(fetch_prs, "DB_PATH", str(tmp_path / "live.duckdb"))
    monkeypatch.setattr(fetch_prs, "REQUEST_PAUSE_SECONDS", 0)
    monkeypatch.setattr(fetch_prs, "fetch_prs_since", lambda *a, **k: calls.append("full") or [
        {"number": 1, "title": "t", "state": "open", "user": None, "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z"}])
    monkeypatch.setattr(fetch_prs, "fetch_prs_updated_since", lambda owner, repo, since, *a, **k: calls.append(("incremental", since)) or [])
    monkeypatch.setattr(fetch_prs, "fetch_reviews_for_pr", lambda *a, **k: [{"id": 5, "user": {"login": "r"}, "state": "APPROVED", "submitted_at": "2024-01-02T00:00:00Z"}])
    stamps = iter([datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 2), datetime.datetime(2026, 1, 3)])
    monkeypatch.setattr(fetch_prs, "utc_now_naive", lambda: next(stamps))
    fetch_prs.main([])
    fetch_prs.main([])
    fetch_prs.main(["--full"])
    assert calls == ["full", ("incremental", datetime.datetime(2025, 12, 29)), "full"]
    con = duckdb.connect(str(tmp_path / "live.duckdb"))
    assert con.execute("select mode from raw.github_fetch_runs order by loaded_at").fetchall() == [("full",), ("incremental",), ("full",)]
    assert con.execute("select count(*) from raw.github_pr_reviews").fetchone() == (1,)
    con.close()
