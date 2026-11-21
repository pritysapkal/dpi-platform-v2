"""The published snapshot: the export, the dashboard reading it, and what happens when it cannot be downloaded.

any_data tests export the database the tests run on (DPIP_DB_PATH: the fixture in CI) to Parquet in a temporary folder,
then read it back through a file:// URL exactly as the public deployment reads raw.githubusercontent.com. Every query
function must give the same answer on the snapshot as on the database. no_db tests check the download, checksum, cache
and fallback rules on a tiny made-up snapshot, with no project database at all.
"""

import datetime
import hashlib
import inspect
import json
import math
import shutil
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import build_fixture as fx
import data_source
import queries

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
import export_snapshot  # noqa: E402

APP = str(ROOT / "dashboard" / "app.py")


# ---------- helpers ----------

@pytest.fixture(autouse=True)
def _fresh_sources():
    data_source.reset()
    yield
    data_source.reset()


def use_local(monkeypatch):
    monkeypatch.setenv("DPIP_DATA_SOURCE", "local")
    data_source.reset()


def use_snapshot(monkeypatch, folder: Path, cache: Path):
    monkeypatch.setenv("DPIP_DATA_SOURCE", "snapshot")
    monkeypatch.setenv("DPIP_SNAPSHOT_URL", folder.resolve().as_uri())
    monkeypatch.setenv("DPIP_SNAPSHOT_CACHE", str(cache))
    data_source.reset()


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    """The test database exported once, as the daily refresh would export dev.duckdb."""
    if not queries.DB_PATH.exists():
        pytest.skip(f"{queries.DB_PATH.name} not built")  # conftest turns this into a failure under DPIP_FAIL_ON_SKIP=1
    folder = tmp_path_factory.mktemp("snapshot")
    manifest = export_snapshot.export(queries.DB_PATH, folder, commit="test-commit")
    return folder, manifest


def db_rows(sql):
    con = duckdb.connect(str(queries.DB_PATH), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ---------- the export ----------

@pytest.mark.any_data
def test_the_manifest_states_version_times_commit_and_per_table_rows_size_and_checksum(exported):
    folder, manifest = exported
    assert manifest["schema_version"] == data_source.SCHEMA_VERSION == 1
    assert manifest["source_commit"] == "test-commit"
    (as_of,), = db_rows("select max(data_as_of) from main.fct_pull_requests")
    assert manifest["data_as_of"] == as_of.replace(microsecond=0).isoformat() + "Z"
    generated = datetime.datetime.fromisoformat(manifest["generated_at"].rstrip("Z"))
    assert abs(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - generated) < datetime.timedelta(minutes=10)
    assert set(manifest["tables"]) == {"fct_pull_requests", "dim_contributors", "dim_date", "pr_reviews"}
    for name, entry in manifest["tables"].items():
        path = folder / entry["file"]
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()  # computed here independently
        assert entry["bytes"] == path.stat().st_size
        (rows,), = db_rows(f"select count(*) from {entry['relation']}")
        assert entry["rows"] == rows
    assert json.loads((folder / "manifest.json").read_text(encoding="utf-8")) == manifest


@pytest.mark.any_data
def test_every_table_in_the_snapshot_is_identical_to_the_database_row_for_row(exported):
    folder, manifest = exported
    for name, entry in manifest["tables"].items():
        parquet = str(folder / entry["file"]).replace("'", "''")
        in_db = sorted(db_rows(f"select * from {entry['relation']}"), key=repr)
        con = duckdb.connect(":memory:")
        in_file = sorted(con.execute(f"select * from read_parquet('{parquet}')").fetchall(), key=repr)
        file_types = con.execute(f"select column_name, column_type from (describe select * from read_parquet('{parquet}'))").fetchall()
        con.close()
        db_types = db_rows(f"select column_name, column_type from (describe select * from {entry['relation']})")
        assert in_file == in_db, name
        assert file_types == db_types, name


@pytest.mark.fixture_numbers
def test_the_fixture_snapshot_has_the_case_tables_row_counts(exported):
    _, manifest = exported
    facts = fx.facts()
    rows = {name: entry["rows"] for name, entry in manifest["tables"].items()}
    first_year, last_year = min(p.created for p in fx.CASES)[:4], fx.PULLED_AT.year
    whole_years = (datetime.date(last_year, 12, 31) - datetime.date(int(first_year), 1, 1)).days + 1  # dim_date: Jan 1 of the first year to Dec 31 of the pull's year
    assert rows == {"fct_pull_requests": facts["prs"], "dim_contributors": facts["logins"], "dim_date": whole_years, "pr_reviews": facts["reviews"]}
    assert rows == {"fct_pull_requests": 50, "dim_contributors": 14, "dim_date": 1461, "pr_reviews": 40}


@pytest.mark.any_data
def test_the_export_refuses_a_snapshot_that_does_not_match_the_database(exported, tmp_path):
    folder, manifest = exported
    copy = tmp_path / "copy"
    shutil.copytree(folder, copy)
    # replace the PR file with one missing a row, and fix its checksum so only the content check can catch it
    target = str(copy / "fct_pull_requests.parquet").replace("'", "''")
    con = duckdb.connect(str(queries.DB_PATH), read_only=True)
    con.execute(f"copy (select * from main.fct_pull_requests order by all offset 1) to '{target}' (format parquet)")
    con.close()
    edited = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    edited["tables"]["fct_pull_requests"].update(sha256=data_source.sha256_of(copy / "fct_pull_requests.parquet"))
    edited["tables"]["fct_pull_requests"]["rows"] -= 1
    (copy / "manifest.json").write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(data_source.SnapshotInvalid, match="fct_pull_requests: 1 row"):
        export_snapshot.check(queries.DB_PATH, copy)


# ---------- every query function, on both sources ----------

QUERY_FUNCTIONS = {"data_window", "trend", "has_activity", "lead_time_breakdown", "review_funnel", "open_pr_age",
                   "top_reviewers", "top_authors", "kpis"}


@pytest.mark.no_db
def test_the_parity_test_covers_every_query_function():
    defined = {name for name, f in inspect.getmembers(queries, inspect.isfunction) if f.__module__ == "queries"}
    assert defined - {"run", "run_df", "source"} == QUERY_FUNCTIONS


def call_all(windows):
    results = {"data_window": queries.data_window()}
    for w in windows:
        for bots in (False, True):
            results[("trend", w, bots, "week")] = queries.trend(*w, bots, "week")
            results[("trend", w, bots, "month")] = queries.trend(*w, bots, "month")
            results[("has_activity", w, bots)] = queries.has_activity(*w, bots)
            results[("review_funnel", w, bots)] = queries.review_funnel(*w, bots)
            results[("open_pr_age", w, bots)] = queries.open_pr_age(*w, bots)
            results[("kpis", w, bots)] = queries.kpis(*w, bots)
        results[("lead_time_breakdown", w)] = queries.lead_time_breakdown(*w)
        results[("top_reviewers", w)] = queries.top_reviewers(*w)
        results[("top_authors", w)] = queries.top_authors(*w)
    return results


# Floats may differ in the last of their ~16 digits: a sum of doubles depends on the order the rows are added, and the
# snapshot stores rows in a different order than the database (seen on the live data: 51.14222049449723 vs ...722).
# 1e-12 is far below anything the page shows; every count, date and text must still be exactly equal.
FLOAT_TOLERANCE = 1e-12


def assert_same(a, b, where):
    if isinstance(a, pd.DataFrame):
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=FLOAT_TOLERANCE, atol=0, obj=str(where))
        for column in a.columns:
            if not pd.api.types.is_float_dtype(a[column]):
                assert a[column].equals(b[column]), (where, column)  # exact for everything that is not a float
    elif isinstance(a, dict):
        assert a.keys() == b.keys(), where
        for key in a:
            assert_same(a[key], b[key], (where, key))
    elif isinstance(a, float) and isinstance(b, float):
        assert math.isclose(a, b, rel_tol=FLOAT_TOLERANCE, abs_tol=0), (where, a, b)
    else:
        assert type(a) is type(b) and a == b, where


@pytest.mark.any_data
def test_every_query_function_gives_the_same_answer_on_the_snapshot_as_on_the_database(exported, tmp_path, monkeypatch):
    folder, _ = exported
    use_local(monkeypatch)
    first, last, _ = queries.data_window()
    windows = [(first, last), (last - datetime.timedelta(days=90), last), (last, last), (first, first + datetime.timedelta(days=400))]
    local = call_all(windows)
    assert queries.source().kind == "local"
    use_snapshot(monkeypatch, folder, tmp_path / "cache")
    snapshot = call_all(windows)
    assert queries.source().kind == "snapshot"
    assert {k if isinstance(k, str) else k[0] for k in local} == QUERY_FUNCTIONS
    assert local.keys() == snapshot.keys()
    for key in local:
        assert_same(local[key], snapshot[key], key)


# ---------- the app on a snapshot ----------

def run_app():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, at.exception
    return at


def page_text(at):
    return " ".join(c.value for c in at.caption)


@pytest.mark.any_data
def test_the_app_on_the_snapshot_shows_the_same_figures_as_on_the_database_and_names_its_source(exported, tmp_path, monkeypatch):
    folder, _ = exported
    use_local(monkeypatch)
    local = run_app()
    use_snapshot(monkeypatch, folder, tmp_path / "cache")
    snap = run_app()
    assert "Source: local database." in page_text(local) and "published snapshot" not in page_text(local)
    assert "Source: published snapshot." in page_text(snap)
    assert [(m.label, m.value) for m in snap.metric] == [(m.label, m.value) for m in local.metric]
    assert [c.value for c in snap.caption][1:] == [c.value for c in local.caption][1:]  # all but the source line


@pytest.mark.fixture_numbers
def test_the_app_on_the_fixture_snapshot_shows_the_hand_counted_figures_and_the_stale_banner(exported, tmp_path, monkeypatch):
    use_snapshot(monkeypatch, exported[0], tmp_path / "cache")
    at = run_app()
    assert {m.label: m.value for m in at.metric}["Merged PRs"] == f"{fx.facts()['human_merged']}" == "27"
    # the fixture was "pulled" on 2026-01-15, far more than 48 hours ago
    assert any("more than 48 hours old" in w.value and "2026-01-15 12:00 UTC" in w.value for w in at.warning)


@pytest.mark.any_data
def test_when_a_new_download_fails_the_app_uses_the_cached_copy_and_says_so(exported, tmp_path, monkeypatch):
    folder, _ = exported
    published = tmp_path / "published"
    shutil.copytree(folder, published)
    use_snapshot(monkeypatch, published, tmp_path / "cache")
    first = run_app()
    assert not any("could not be downloaded" in w.value for w in first.warning)
    # an hour later the branch is mid-publish: a file no longer matches its checksum
    pointer = tmp_path / "cache" / "current.json"
    state = json.loads(pointer.read_text())
    pointer.write_text(json.dumps({**state, "downloaded_at": state["downloaded_at"] - data_source.CACHE_SECONDS - 1}))
    (published / "dim_date.parquet").write_bytes(b"not parquet")
    data_source.reset()
    later = run_app()
    assert any("could not be downloaded" in w.value and "checksum" in w.value for w in later.warning)
    assert [(m.label, m.value) for m in later.metric] == [(m.label, m.value) for m in first.metric]


@pytest.mark.no_db
def test_with_no_database_and_no_snapshot_the_app_shows_an_error_page_not_numbers(tmp_path, monkeypatch):
    empty = tmp_path / "nothing-published"
    empty.mkdir()
    use_snapshot(monkeypatch, empty, tmp_path / "cache")
    at = run_app()
    assert len(at.error) == 1 and "could not be loaded" in at.error[0].value
    assert not at.metric and at.title[0].value == "PR Lifecycle & DORA Metrics"


# ---------- choosing the source, download, checksum, cache (no database) ----------

def make_snapshot(folder: Path, rows=3, schema_version=data_source.SCHEMA_VERSION) -> dict:
    """A tiny made-up snapshot: one integer column per table, with a correct manifest."""
    folder.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    tables = {}
    for name, relation in data_source.TABLES.items():
        path = folder / f"{name}.parquet"
        con.execute(f"copy (select range as n from range({rows})) to '{str(path).replace(chr(39), chr(39) * 2)}' (format parquet)")
        tables[name] = {"file": path.name, "relation": relation, "rows": rows, "bytes": path.stat().st_size, "sha256": data_source.sha256_of(path)}
    con.close()
    manifest = {"schema_version": schema_version, "generated_at": "x", "data_as_of": "x", "source_commit": "x", "tables": tables}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


@pytest.fixture
def counted_downloads(monkeypatch):
    calls = []
    real = data_source._fetch

    def fetch(url, destination):
        calls.append(url)
        real(url, destination)

    monkeypatch.setattr(data_source, "_fetch", fetch)
    return calls


@pytest.mark.no_db
@pytest.mark.parametrize("forced, exists, expected", [
    (None, True, "local"), (None, False, "snapshot"), ("", False, "snapshot"),
    ("snapshot", True, "snapshot"), ("local", False, "local"), (" Local ", True, "local"),
])
def test_the_source_is_the_override_else_the_database_if_present_else_the_snapshot(monkeypatch, tmp_path, forced, exists, expected):
    db = tmp_path / "dev.duckdb"
    if exists:
        db.write_bytes(b"")
    if forced is None:
        monkeypatch.delenv("DPIP_DATA_SOURCE", raising=False)
    else:
        monkeypatch.setenv("DPIP_DATA_SOURCE", forced)
    assert data_source.choose(db) == expected


@pytest.mark.no_db
def test_an_unknown_override_is_an_error_not_a_guess(monkeypatch, tmp_path):
    monkeypatch.setenv("DPIP_DATA_SOURCE", "s3")
    with pytest.raises(ValueError, match="DPIP_DATA_SOURCE"):
        data_source.choose(tmp_path / "dev.duckdb")


@pytest.mark.no_db
def test_the_default_snapshot_address_is_the_data_branch_on_raw_githubusercontent(monkeypatch):
    monkeypatch.delenv("DPIP_SNAPSHOT_URL", raising=False)
    assert data_source.snapshot_url() == "https://raw.githubusercontent.com/pritysapkal/dpi-platform-v2/data"


@pytest.mark.no_db
def test_a_verified_download_opens_as_views_named_like_the_warehouse_tables(tmp_path, monkeypatch):
    make_snapshot(tmp_path / "pub")
    use_snapshot(monkeypatch, tmp_path / "pub", tmp_path / "cache")
    source = data_source.active(tmp_path / "no.duckdb")
    con = source.connect()
    views = con.execute("select schema_name || '.' || view_name from duckdb_views() where not internal order by 1").fetchall()
    assert sorted(v for (v,) in views) == sorted(data_source.TABLES.values())
    assert con.execute("select count(*) from main.stg_github__pr_reviews").fetchone() == (3,)
    con.close()
    assert source.kind == "snapshot" and source.label == "published snapshot" and source.notice is None


@pytest.mark.no_db
def test_within_an_hour_the_cached_copy_is_used_without_downloading_again(tmp_path, monkeypatch, counted_downloads):
    make_snapshot(tmp_path / "pub")
    use_snapshot(monkeypatch, tmp_path / "pub", tmp_path / "cache")
    data_source.active(tmp_path / "no.duckdb")
    assert len(counted_downloads) == 5  # manifest + 4 tables
    data_source.reset()  # a restarted app: nothing in memory, only the cache folder
    data_source.active(tmp_path / "no.duckdb")
    assert len(counted_downloads) == 5


@pytest.mark.no_db
def test_after_an_hour_a_new_snapshot_is_downloaded_and_replaces_the_old_one(tmp_path, monkeypatch, counted_downloads):
    make_snapshot(tmp_path / "pub", rows=3)
    use_snapshot(monkeypatch, tmp_path / "pub", tmp_path / "cache")
    db = tmp_path / "no.duckdb"
    data_source.active(db)
    make_snapshot(tmp_path / "pub", rows=5)  # the next day's publish
    real_time = time.time
    monkeypatch.setattr(data_source.time, "time", lambda: real_time() + data_source.CACHE_SECONDS + 1)
    con = data_source.active(db).connect()
    assert con.execute("select count(*) from main.fct_pull_requests").fetchone() == (5,)
    con.close()
    assert len(counted_downloads) == 10
    assert len(list((tmp_path / "cache").glob("snapshot-*"))) <= 2  # old copies are cleaned up


@pytest.mark.no_db
@pytest.mark.parametrize("damage, message", [
    (lambda pub: (pub / "dim_date.parquet").write_bytes(b"tampered"), "checksum"),
    (lambda pub: (pub / "pr_reviews.parquet").unlink(), "HTTP|No such file|cannot find|FileNotFound|URLError"),
    (lambda pub: make_snapshot(pub, schema_version=99), "schema version 99"),
])
def test_a_bad_download_with_nothing_cached_is_refused(tmp_path, monkeypatch, damage, message):
    make_snapshot(tmp_path / "pub")
    damage(tmp_path / "pub")
    use_snapshot(monkeypatch, tmp_path / "pub", tmp_path / "cache")
    with pytest.raises(data_source.SnapshotUnavailable, match=message):
        data_source.active(tmp_path / "no.duckdb")
    assert not list((tmp_path / "cache").glob("snapshot-*"))  # nothing unverified is ever kept


@pytest.mark.no_db
def test_a_bad_download_falls_back_to_the_last_verified_copy_and_retries_after_five_minutes(tmp_path, monkeypatch, counted_downloads):
    make_snapshot(tmp_path / "pub", rows=3)
    use_snapshot(monkeypatch, tmp_path / "pub", tmp_path / "cache")
    db = tmp_path / "no.duckdb"
    data_source.active(db)
    (tmp_path / "pub" / "manifest.json").write_text("{not json", encoding="utf-8")
    now = time.time() + data_source.CACHE_SECONDS + 1
    monkeypatch.setattr(data_source.time, "time", lambda: now)
    source = data_source.active(db)
    assert "could not be downloaded" in source.notice and "downloaded at" in source.notice
    con = source.connect()
    assert con.execute("select count(*) from main.dim_date").fetchone() == (3,)
    con.close()
    tries = len(counted_downloads)
    monkeypatch.setattr(data_source.time, "time", lambda: now + data_source.RETRY_AFTER_FAILURE_SECONDS - 1)
    data_source.active(db)
    assert len(counted_downloads) == tries  # no new attempt yet
    make_snapshot(tmp_path / "pub", rows=4)  # the branch is fixed
    monkeypatch.setattr(data_source.time, "time", lambda: now + data_source.RETRY_AFTER_FAILURE_SECONDS + 1)
    source = data_source.active(db)
    assert source.notice is None and source.connect().execute("select count(*) from main.dim_date").fetchone() == (4,)


@pytest.mark.no_db
def test_a_manifest_cannot_name_a_file_outside_the_snapshot_folder(tmp_path):
    manifest = make_snapshot(tmp_path / "pub")
    manifest["tables"]["dim_date"]["file"] = "../dim_date.parquet"
    shutil.copy(tmp_path / "pub" / "dim_date.parquet", tmp_path / "dim_date.parquet")
    with pytest.raises(data_source.SnapshotInvalid, match="missing"):
        data_source.verify(tmp_path / "pub", manifest)


@pytest.mark.no_db
def test_a_wrong_row_count_is_refused_even_with_a_matching_checksum(tmp_path):
    manifest = make_snapshot(tmp_path / "pub")
    manifest["tables"]["fct_pull_requests"]["rows"] = 4
    with pytest.raises(data_source.SnapshotInvalid, match="3 rows, the manifest says 4"):
        data_source.open_snapshot(tmp_path / "pub", manifest)


@pytest.mark.no_db
@pytest.mark.parametrize("age_hours, stale", [(1, False), (47.9, False), (48, False), (48.1, True), (24 * 20, True)])
def test_data_is_stale_after_more_than_48_hours(age_hours, stale):
    now = datetime.datetime(2026, 11, 22, 12, 0)
    assert data_source.is_stale(now - datetime.timedelta(hours=age_hours), now=now) is stale
