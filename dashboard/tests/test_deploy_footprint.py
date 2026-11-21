"""What the public deployment installs and runs: the dashboard only, with its own small requirements file.

Streamlit Community Cloud looks for a dependency file next to the entrypoint (dashboard/app.py) first and only then at
the repository root, and uses the first one it finds. So dashboard/requirements.txt is what gets installed, and it must
cover everything the dashboard imports, and nothing heavy: no dbt, no Gemini SDK. These checks read source files only.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.no_db

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "dashboard"
APP_FILES = sorted(DASHBOARD.glob("*.py"))  # what the deployed app runs (tests excluded)
LOCAL_MODULES = {p.stem for p in APP_FILES}
# the import name of each package, where it differs from the name pip installs
PACKAGE_OF = {}


def imported_top_level_names(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def requirements() -> dict[str, str]:
    lines = (DASHBOARD / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pinned = [re.match(r"^([A-Za-z0-9_.-]+)==(\S+)$", line.strip()) for line in lines if line.strip() and not line.startswith("#")]
    assert all(pinned), "every line of dashboard/requirements.txt is pinned as name==version"
    return {m.group(1).lower(): m.group(2) for m in pinned}


def test_the_app_files_are_the_ones_expected():
    assert LOCAL_MODULES == {"app", "charts", "data_source", "presets", "queries"}


def test_the_dashboard_never_imports_the_agent_or_its_sdk():
    for path in APP_FILES:
        names = imported_top_level_names(path)
        assert not names & {"agent", "google", "dotenv", "dbt", "metricflow", "httpx"}, f"{path.name} imports {names}"


def test_dashboard_requirements_have_no_gemini_sdk_and_no_dbt():
    reqs = requirements()
    assert "google-genai" not in reqs and not any(name.startswith(("dbt", "google")) for name in reqs)


def test_dashboard_requirements_cover_every_package_the_dashboard_imports():
    third_party = set()
    for path in APP_FILES:
        third_party |= imported_top_level_names(path) - LOCAL_MODULES - set(sys.stdlib_module_names)
    needed = {PACKAGE_OF.get(name, name).lower() for name in third_party}
    assert needed == {"streamlit", "duckdb", "altair", "pandas"}
    assert needed <= set(requirements())


def test_the_dashboard_pins_the_same_versions_as_the_rest_of_the_project():
    root = {line.split("==")[0].lower(): line.split("==")[1].strip()
            for path in (ROOT / "requirements.txt", ROOT / "agent" / "requirements.txt")
            for line in path.read_text(encoding="utf-8").splitlines() if "==" in line}
    for name, version in requirements().items():
        if name in root:
            assert root[name] == version, f"{name}: dashboard pins {version}, the project pins {root[name]}"


def test_no_other_dependency_file_next_to_the_entrypoint_could_take_precedence():
    # Community Cloud's order within a folder: uv.lock, Pipfile, environment.yml, requirements.txt, pyproject.toml
    others = ["uv.lock", "Pipfile", "environment.yml", "environment.yaml", "pyproject.toml"]
    assert [name for name in others if (DASHBOARD / name).exists()] == []
    assert (DASHBOARD / "requirements.txt").is_file()


def test_no_database_file_can_be_committed_so_the_deployment_reads_the_published_snapshot():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.duckdb" in ignored
