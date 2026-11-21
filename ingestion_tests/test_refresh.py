"""The daily refresh: its workflow file, the freshness rule, and the alert that opens and closes the failure issue.

No network and no database: the workflow and the dbt source file are read as YAML, and the alert script talks to a fake
`gh` that records every command and answers the way GitHub would.
"""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import refresh_alert  # noqa: E402
import workflow_selections  # noqa: E402

pytestmark = pytest.mark.no_db

RUN_URL = "https://github.com/owner/repo/actions/runs/42"


class FakeGh:
    """Stands in for the `gh` command line: records calls, answers from a small in-memory list of issues."""

    def __init__(self, open_issues=(), failed_step_names=(), step_lookup_fails=False):
        self.open = list(open_issues)
        self.failed_step_names = list(failed_step_names)
        self.step_lookup_fails = step_lookup_fails
        self.calls = []
        self.next_number = 100

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("issue", "list"):
            return "\n".join(str(n) for n in self.open) + ("\n" if self.open else "")
        if args[:2] == ("run", "view"):
            if self.step_lookup_fails:
                raise RuntimeError("HTTP 403")
            return "\n".join(self.failed_step_names)
        if args[:2] == ("issue", "create"):
            self.open.append(self.next_number)
            return f"https://github.com/owner/repo/issues/{self.next_number}\n"
        if args[:2] == ("issue", "close"):
            self.open.remove(int(args[2]))
        return ""

    def made(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == prefix]


# ---------- the alert ----------

def test_a_first_failure_creates_the_label_and_opens_one_issue_with_the_run_link_and_the_failed_step():
    fake = FakeGh(failed_step_names=["Fetch the live data from GitHub"])
    assert refresh_alert.report_failure(fake, RUN_URL, "42") == "opened https://github.com/owner/repo/issues/100"
    (create,) = fake.made("issue", "create")
    body = create[create.index("--body") + 1]
    assert create[create.index("--label") + 1] == "data-refresh-failure" and create[create.index("--title") + 1] == refresh_alert.TITLE
    assert RUN_URL in body and "Fetch the live data from GitHub" in body
    assert fake.made("label", "create")  # the label exists before the issue uses it (--force: fine if it already does)


def test_a_second_failure_comments_on_the_open_issue_instead_of_opening_another():
    fake = FakeGh(open_issues=[7])
    assert refresh_alert.report_failure(fake, RUN_URL, "42") == "commented on issue #7"
    assert not fake.made("issue", "create")
    (comment,) = fake.made("issue", "comment")
    assert comment[2] == "7" and RUN_URL in comment[comment.index("--body") + 1]


def test_the_alert_still_goes_out_when_the_failed_step_cannot_be_looked_up():
    fake = FakeGh(step_lookup_fails=True)
    refresh_alert.report_failure(fake, RUN_URL, "42")
    body = fake.made("issue", "create")[0][-1]
    assert "Open the run to see which step failed" in body


def test_a_success_closes_every_open_failure_issue_with_a_comment():
    fake = FakeGh(open_issues=[7, 9])
    assert refresh_alert.report_success(fake, RUN_URL) == "closed issue(s) #7, #9"
    closed = fake.made("issue", "close")
    assert [c[2] for c in closed] == ["7", "9"] and all(RUN_URL in c[c.index("--comment") + 1] for c in closed)
    assert fake.open == []


def test_a_success_with_nothing_open_does_nothing():
    fake = FakeGh()
    assert refresh_alert.report_success(fake, RUN_URL) == "nothing to close"
    assert [c[:2] for c in fake.calls] == [("issue", "list")]


def test_failure_then_failure_then_success_is_one_issue_opened_once_and_closed_once():
    fake = FakeGh()
    refresh_alert.report_failure(fake, RUN_URL, "1")
    refresh_alert.report_failure(fake, RUN_URL, "2")
    refresh_alert.report_success(fake, RUN_URL)
    assert len(fake.made("issue", "create")) == 1 and len(fake.made("issue", "comment")) == 1 and len(fake.made("issue", "close")) == 1
    assert fake.open == []


def test_the_command_line_needs_a_mode(monkeypatch, capsys):
    monkeypatch.setenv("RUN_URL", RUN_URL)
    assert refresh_alert.main(["refresh_alert.py"], FakeGh()) == 2
    assert refresh_alert.main(["refresh_alert.py", "success"], FakeGh()) == 0 and "nothing to close" in capsys.readouterr().out


# ---------- the workflow file ----------

@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "refresh.yml").read_text(encoding="utf-8"))


def triggers(workflow):
    return workflow.get("on") or workflow.get(True)  # YAML 1.1 reads the bare key `on` as True


def test_it_runs_daily_at_02_47_utc_and_by_hand_with_a_simulate_failure_switch(workflow):
    on = triggers(workflow)
    assert on["schedule"] == [{"cron": "47 2 * * *"}]
    switch = on["workflow_dispatch"]["inputs"]["simulate_failure"]
    assert switch["type"] == "boolean" and switch["default"] is False


def test_permissions_are_the_minimum_each_job_needs(workflow):
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["refresh"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["alert"]["permissions"] == {"contents": "read", "issues": "write", "actions": "read"}
    assert workflow["jobs"]["publish"]["permissions"] == {"contents": "write"}  # the only job that can write; it runs no project code, only git


def test_the_refresh_steps_run_in_the_planned_order_with_zero_skips(workflow):
    job = workflow["jobs"]["refresh"]
    commands = [s.get("run", "") for s in job["steps"]]
    order = ["exit 1", "python fetch_prs.py", "dbt build", "dbt source freshness", "mf validate-configs", "pytest", "export_snapshot.py"]
    positions = [next(i for i, c in enumerate(commands) if needle in c) for needle in order]
    assert positions == sorted(positions)
    assert job["env"]["DPIP_FAIL_ON_SKIP"] == "1" and job["env"]["DPIP_DB_PATH"] == "dev.duckdb"
    simulated = job["steps"][positions[0]]
    assert simulated["if"] == "${{ inputs.simulate_failure }}"  # only when asked for; a scheduled run has no inputs
    fetch = job["steps"][positions[1]]
    assert fetch["env"] == {"GH_TOKEN": "${{ secrets.GH_TOKEN }}"}  # the secret reaches only the step that needs it


def test_the_live_run_selects_only_tests_that_hold_on_any_data(workflow):
    pytest_step = next(s["run"] for s in workflow["jobs"]["refresh"]["steps"] if "pytest" in s.get("run", ""))
    assert "workflow_selections.py scheduled-live" in pytest_step
    assert workflow_selections.WORKFLOWS["scheduled-live"] == ("no_db or any_data", "live")


def test_the_alert_runs_after_success_or_failure_but_not_after_a_cancellation(workflow):
    alert = workflow["jobs"]["alert"]
    assert alert["needs"] == ["refresh", "publish"]
    condition = alert["if"]
    assert condition == ("${{ always() && (needs.refresh.result == 'failure' || needs.publish.result == 'failure' "
                         "|| needs.publish.result == 'success') }}")
    steps = {s.get("if"): s.get("run") for s in alert["steps"] if "run" in s}
    assert steps["${{ needs.refresh.result == 'failure' || needs.publish.result == 'failure' }}"] == "python3 ops/refresh_alert.py failure"
    assert steps["${{ needs.publish.result == 'success' }}"] == "python3 ops/refresh_alert.py success"
    assert alert["env"]["GH_TOKEN"] == "${{ github.token }}"  # the workflow's own short-lived token, no personal token


# ---------- publishing the snapshot ----------

def test_the_snapshot_is_exported_only_after_every_check_and_handed_over_as_an_artifact(workflow):
    steps = workflow["jobs"]["refresh"]["steps"]
    export = next(i for i, s in enumerate(steps) if "export_snapshot.py" in s.get("run", ""))
    assert steps[export]["run"] == "python ops/export_snapshot.py dev.duckdb snapshot" and "if" not in steps[export]
    assert all("if" not in s or s["if"] == "${{ inputs.simulate_failure }}" for s in steps[:export])  # no step is optional
    upload = steps[export + 1]
    assert upload["uses"] == "actions/upload-artifact@v4" and upload["with"]["name"] == "snapshot"
    assert upload["with"]["if-no-files-found"] == "error" and export + 2 == len(steps)


def test_publish_waits_for_a_successful_refresh_and_force_pushes_one_orphan_commit_to_the_data_branch(workflow):
    job = workflow["jobs"]["publish"]
    assert job["needs"] == "refresh" and "if" not in job  # default: only after the refresh job succeeded
    uses = [s.get("uses") for s in job["steps"]]
    assert uses[:2] == ["actions/checkout@v4", "actions/download-artifact@v4"]
    assert job["steps"][1]["with"]["name"] == "snapshot"
    script = job["steps"][2]["run"]
    assert "set -euo pipefail" in script and "--orphan" in script
    assert "git push --force origin HEAD:refs/heads/data" in script
    assert "secrets." not in str(job) and "GH_TOKEN" not in str(job)  # the GitHub API token never reaches this job


# ---------- the freshness rule ----------

def test_raw_sources_warn_after_36_hours_and_fail_after_72():
    source = yaml.safe_load((ROOT / "models" / "staging" / "github" / "sources.yml").read_text(encoding="utf-8"))["sources"][0]
    config = source["config"]
    assert config["loaded_at_field"] == "_loaded_at"
    assert config["freshness"] == {"warn_after": {"count": 36, "period": "hour"}, "error_after": {"count": 72, "period": "hour"}}
    assert {t["name"] for t in source["tables"]} == {"github_pull_requests", "github_pr_reviews"}
