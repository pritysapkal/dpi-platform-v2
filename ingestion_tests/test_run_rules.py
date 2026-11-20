"""The rules in conftest.py: which data a marked test may run on, and that skips cannot hide gaps.

Each case writes a tiny throw-away project (conftest + one test file + a tiny database) in a temporary folder and runs
pytest on it in a separate process, so the real rules are exercised end to end. No network, no real database.
"""

import datetime
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import workflow_selections  # noqa: E402

FIXTURE_TIME = datetime.datetime(2026, 1, 15, 12, 0, 0)
FROZEN_TIME = datetime.datetime(2026, 11, 1, 18, 34, 18, 677215)  # the real pull has microseconds; they must not matter
LIVE_TIME = datetime.datetime(2026, 11, 20, 2, 47, 0)


def make_db(path: Path, as_of: datetime.datetime) -> Path:
    con = duckdb.connect(str(path))
    con.execute("create table fct_pull_requests (data_as_of timestamp)")
    con.execute("insert into fct_pull_requests values (?)", [as_of])
    con.close()
    return path


def run_pytest(tmp_path, test_source: str, db: Path | None = None, fail_on_skip: bool = False, extra=()):
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    for name in ("conftest.py", "pytest.ini"):
        shutil.copy(ROOT / name, project / name)
    (project / "test_case.py").write_text(textwrap.dedent(test_source), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DPIP_")}
    if db is not None:
        env["DPIP_DB_PATH"] = str(db)
    if fail_on_skip:
        env["DPIP_FAIL_ON_SKIP"] = "1"
    result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", *extra], cwd=project, env=env, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


ONE_TEST = """
    import pytest
    @pytest.mark.{marker}
    def test_it():
        pass
"""


# ---------- a test pinned to a dataset only runs on that dataset ----------

@pytest.mark.parametrize("marker, as_of", [("fixture_numbers", FIXTURE_TIME), ("frozen_data", FROZEN_TIME), ("any_data", LIVE_TIME), ("any_data", FIXTURE_TIME)])
def test_a_test_runs_on_the_data_it_asks_for(tmp_path, marker, as_of):
    code, out = run_pytest(tmp_path, ONE_TEST.format(marker=marker), make_db(tmp_path / "d.duckdb", as_of))
    assert code == 0 and "1 passed" in out, out


@pytest.mark.parametrize("marker, wrong, found", [
    ("fixture_numbers", FROZEN_TIME, "frozen"), ("fixture_numbers", LIVE_TIME, "live"),
    ("frozen_data", FIXTURE_TIME, "fixture"), ("frozen_data", LIVE_TIME, "live"),
])
def test_pinned_numbers_on_the_wrong_data_fail_loudly_and_are_never_skipped(tmp_path, marker, wrong, found):
    code, out = run_pytest(tmp_path, ONE_TEST.format(marker=marker), make_db(tmp_path / "d.duckdb", wrong))
    assert code == 1 and "1 error" in out and "skipped" not in out  # a failure while setting the test up pytest calls an error
    assert f"holds the {found} data" in out


# ---------- a missing database ----------

def test_a_missing_database_skips_normally_but_fails_when_skips_are_forbidden(tmp_path):
    missing = tmp_path / "nope.duckdb"
    code, out = run_pytest(tmp_path / "a", ONE_TEST.format(marker="any_data"), missing)
    assert code == 0 and "1 skipped" in out and "nope.duckdb not built" in out
    code, out = run_pytest(tmp_path / "b", ONE_TEST.format(marker="any_data"), missing, fail_on_skip=True)
    assert code == 1 and "1 error" in out and "skipped" not in out and "nope.duckdb not built" in out


# ---------- the zero-skip guard ----------

SKIPS = """
    import pytest
    def test_plain_skip():
        pytest.skip("not today")
    @pytest.mark.skipif(True, reason="always")
    def test_skipif():
        pass
    @pytest.mark.xfail(reason="known")
    def test_expected_failure():
        assert False
    def test_fine():
        pass
"""


def test_skips_are_allowed_normally(tmp_path):
    code, out = run_pytest(tmp_path, SKIPS)
    assert code == 0 and "2 skipped" in out and "1 passed" in out and "1 xfailed" in out


def test_with_the_flag_every_skip_is_a_failure_that_names_its_reason(tmp_path):
    code, out = run_pytest(tmp_path, SKIPS, fail_on_skip=True)
    summary = out.strip().splitlines()[-1]  # the skipif skip happens while setting up (an error), the plain skip inside the test (a failure)
    assert code == 1 and "1 failed" in summary and "1 error" in summary and "1 passed" in summary and "1 xfailed" in summary and "skipped" not in summary
    assert "not today" in out and "always" in out


# ---------- no_db tests cannot touch the database ----------

NO_DB = """
    import duckdb, os, pytest
    pytestmark = pytest.mark.no_db
    def test_memory_is_fine():
        duckdb.connect().close()
    def test_other_file_is_fine(tmp_path):
        duckdb.connect(str(tmp_path / "other.duckdb")).close()
    def test_the_project_database_is_not():
        duckdb.connect(os.environ["DPIP_DB_PATH"], read_only=True)
"""


def test_a_no_db_test_fails_if_it_opens_the_database_file(tmp_path):
    code, out = run_pytest(tmp_path, NO_DB, make_db(tmp_path / "d.duckdb", LIVE_TIME))
    assert code == 1 and "1 failed" in out and "2 passed" in out
    assert "marked no_db but opened" in out


# ---------- markers are checked, and the workflow table covers them ----------

def test_a_misspelt_marker_is_an_error(tmp_path):
    code, out = run_pytest(tmp_path, ONE_TEST.format(marker="fixture_number"))
    assert code != 0 and "fixture_number" in out


def test_every_marker_runs_in_at_least_one_workflow_and_live_runs_never_include_pinned_numbers():
    markers = {"no_db", "any_data", "fixture_numbers", "frozen_data"}

    def selects(expression, marker):  # evaluate the -m expression the way pytest does: a marker is "on" only for that test
        return eval(expression, {"__builtins__": {}}, {m: m == marker for m in markers})

    for marker in markers:
        assert any(selects(expr, marker) for expr, _ in workflow_selections.WORKFLOWS.values()), f"{marker} tests run nowhere"
    live_expression = workflow_selections.WORKFLOWS["scheduled-live"][0]
    assert not selects(live_expression, "fixture_numbers") and not selects(live_expression, "frozen_data")
    assert workflow_selections.WORKFLOWS["push-fixture"][1] == "fixture" and workflow_selections.WORKFLOWS["push-frozen"][1] == "frozen"
