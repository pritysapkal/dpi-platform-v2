"""Which database file the dashboard and the agent read: dev.duckdb unless DPIP_DB_PATH says otherwise.

Needs no database. Each check runs in a fresh Python process, because the path is read once at import time.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
SNIPPETS = {
    "dashboard": "import sys; sys.path.insert(0, 'dashboard'); import queries; print(queries.DB_PATH)",
    "agent": "from agent.config import DB_PATH; print(DB_PATH)",
}


def db_path_seen_by(which: str, value: str | None) -> Path:
    env = {k: v for k, v in os.environ.items() if k != "DPIP_DB_PATH"}
    if value is not None:
        env["DPIP_DB_PATH"] = value
    out = subprocess.run([sys.executable, "-B", "-c", SNIPPETS[which]], cwd=PROJECT, env=env, capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


@pytest.mark.parametrize("which", SNIPPETS)
def test_without_the_variable_both_read_dev_duckdb(which):
    assert db_path_seen_by(which, None) == PROJECT / "dev.duckdb"


@pytest.mark.parametrize("which", SNIPPETS)
def test_an_empty_variable_counts_as_not_set(which):
    assert db_path_seen_by(which, "") == PROJECT / "dev.duckdb"


@pytest.mark.parametrize("which", SNIPPETS)
def test_the_variable_points_both_at_another_file(which, tmp_path):
    other = tmp_path / "fixture.duckdb"
    assert db_path_seen_by(which, str(other)) == other
