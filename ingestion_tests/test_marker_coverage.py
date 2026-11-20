"""Every test is classified, and every test runs in at least one automated workflow.

The four markers (no_db, any_data, fixture_numbers, frozen_data) say what a test needs. Two things must always be true,
otherwise a test could sit in the repository and never run anywhere:
  1. each test carries exactly one of the four markers;
  2. each test is selected by at least one workflow's `-m` expression (workflow_selections.py), the same table the
     workflow files read.
This asks pytest itself (`--collect-only`) which tests each expression selects, so it checks what will really run.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import workflow_selections

pytestmark = pytest.mark.no_db

ROOT = Path(__file__).resolve().parent.parent
MARKERS = ["no_db", "any_data", "fixture_numbers", "frozen_data"]


COLLECTOR = r"""
import json, sys, pytest

class Dump:
    def pytest_collection_modifyitems(self, items):
        rows = {i.nodeid: sorted({m.name for m in i.iter_markers()}) for i in items}
        sys.stdout.write("\n@@" + json.dumps(rows) + "@@\n")

pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "agent/tests", "dashboard/tests", "ingestion_tests"], plugins=[Dump()])
"""


def collect_markers() -> dict[str, set[str]]:
    """{test id: its markers}, from one collection of the whole suite (about 20 seconds)."""
    result = subprocess.run([sys.executable, "-B", "-c", COLLECTOR], cwd=ROOT, capture_output=True, text=True)
    payload = result.stdout.split("@@")[1]
    return {nodeid: set(names) for nodeid, names in json.loads(payload).items()}


def selected_by(expression: str, markers: dict[str, set[str]]) -> set[str]:
    """The tests `pytest -m expression` would pick, using pytest's own expression evaluator."""
    from _pytest.mark.expression import Expression
    compiled = Expression.compile(expression)
    return {nodeid for nodeid, names in markers.items() if compiled.evaluate(lambda name: name in names)}


@pytest.fixture(scope="module")
def everything():
    found = collect_markers()
    assert len(found) > 400, "the collection looks too small to be the whole suite"
    return found


def test_every_test_carries_exactly_one_marker(everything):
    for nodeid, names in everything.items():
        carried = [m for m in MARKERS if m in names]
        assert len(carried) == 1, f"{nodeid} carries {carried or 'none of'} {MARKERS}"


def test_every_test_is_selected_by_at_least_one_workflow(everything):
    selected = set()
    for name, (expression, _database) in workflow_selections.WORKFLOWS.items():
        picked = selected_by(expression, everything)
        assert picked, f"workflow {name} selects no tests"
        selected |= picked
    never = set(everything) - selected
    assert not never, f"{len(never)} tests run in no workflow, e.g. {sorted(never)[:5]}"


def test_the_selection_helper_agrees_with_pytest_itself():
    # trust, but check: the evaluator above must pick the same tests as a real `pytest -m` run
    markers = collect_markers()
    expression = "frozen_data"
    command = [sys.executable, "-B", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", expression,
               "agent/tests", "dashboard/tests", "ingestion_tests"]
    real = {line for line in subprocess.run(command, cwd=ROOT, capture_output=True, text=True).stdout.splitlines() if "::" in line}
    assert real == selected_by(expression, markers) and len(real) == 7
