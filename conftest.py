"""Rules every test run follows: which data a test may run on, and that nothing is skipped silently.

Four markers say what a test needs (registered in pytest.ini):
    no_db            touches no database file; a guard makes it fail if it tries
    any_data         reads the database but asserts only what is true on any data
    fixture_numbers  asserts exact values of the 50-PR fixture
    frozen_data      asserts exact values of the frozen real pull of 2026-11-01

Two environment variables:
    DPIP_DB_PATH        which database file the dashboard and agent read (default dev.duckdb)
    DPIP_FAIL_ON_SKIP   when set to 1, every skipped test becomes a failure (CI sets it, so "skipped" can never hide a gap)
"""

import datetime
import os
import sys
from pathlib import Path

import duckdb
import pytest

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT / "fixtures"))  # tests import build_fixture for the answer key (facts, period_facts)

# The load time of each known dataset (the data_as_of every fact row carries); anything else is "live".
KNOWN_DATASETS = {
    "fixture": datetime.datetime(2026, 1, 15, 12, 0, 0),
    "frozen": datetime.datetime(2026, 11, 1, 18, 34, 18),
}


def db_path() -> Path:
    return Path(os.environ.get("DPIP_DB_PATH") or PROJECT / "dev.duckdb")


def dataset_of(path: Path) -> str:
    """'fixture', 'frozen' or 'live' - recognised by the pull time stored in the fact table."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        (as_of,) = con.execute("select max(data_as_of) from main.fct_pull_requests").fetchone()
    finally:
        con.close()
    as_of = as_of.replace(microsecond=0)
    return next((name for name, stamp in KNOWN_DATASETS.items() if stamp == as_of), "live")


def fail_on_skip() -> bool:
    return os.environ.get("DPIP_FAIL_ON_SKIP") == "1"


def pytest_runtest_setup(item):
    needed = [m for m in ("fixture_numbers", "frozen_data") if item.get_closest_marker(m)]
    uses_db = needed or item.get_closest_marker("any_data")
    if not uses_db:
        return
    path = db_path()
    if not path.exists():
        message = f"{path.name} not built (set DPIP_DB_PATH, or build it first)"
        if fail_on_skip():
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    if needed:
        wanted = "fixture" if needed[0] == "fixture_numbers" else "frozen"
        found = dataset_of(path)
        if found != wanted:  # never a skip: running pinned numbers on the wrong data is an error, not "nothing to check"
            pytest.fail(f"{item.name} is marked {needed[0]} and needs the {wanted} data, but {path.name} holds the {found} data", pytrace=False)


@pytest.fixture(autouse=True)
def _no_db_tests_never_open_the_project_database(request, monkeypatch):
    """A test marked no_db fails if it opens the database file the other tests read."""
    if not request.node.get_closest_marker("no_db"):
        return
    real_connect = duckdb.connect
    protected = {db_path().resolve(), (PROJECT / "dev.duckdb").resolve()}

    def guarded(database=":memory:", *args, **kwargs):
        if str(database) != ":memory:" and Path(str(database)).resolve() in protected:
            raise AssertionError(f"this test is marked no_db but opened {database}")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(duckdb, "connect", guarded)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if fail_on_skip() and report.skipped and not hasattr(report, "wasxfail"):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        report.outcome = "failed"
        report.longrepr = f"skipped, but DPIP_FAIL_ON_SKIP=1 turns every skip into a failure: {reason}"
