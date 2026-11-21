"""Open, update or close the GitHub issue that says the daily data refresh is failing.

Called by the `alert` job of .github/workflows/refresh.yml after the refresh job finishes:

    python ops/refresh_alert.py failure    # the refresh failed: open an issue, or comment on the one already open
    python ops/refresh_alert.py success    # the refresh worked: comment on any open failure issue and close it

There is only ever one open failure issue, recognised by its label, so a week of failures is one issue with a comment per
failed run, not seven issues. The issue links the failed run and names the step that failed, so whoever reads it knows
where to look. Everything goes through the `gh` command line, authenticated by the workflow's own short-lived token.

Environment: RUN_URL (link to this run), RUN_ID (to look up which step failed), GH_REPO (owner/name), GH_TOKEN.
"""

import os
import subprocess
import sys
from typing import Callable

LABEL = "data-refresh-failure"
TITLE = "Daily data refresh is failing"
LABEL_COLOUR = "d73a4a"

Runner = Callable[..., str]


def gh(*args: str) -> str:
    """Run one `gh` command and return what it printed. Raises if it fails."""
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def open_issues(run: Runner) -> list[int]:
    out = run("issue", "list", "--label", LABEL, "--state", "open", "--json", "number", "--jq", ".[].number")
    return sorted(int(n) for n in out.split())


def failed_steps(run: Runner, run_id: str) -> list[str]:
    """The names of the steps that failed in this run. Best effort: an empty list if the lookup itself fails."""
    if not run_id:
        return []
    query = '.jobs[] | select(.conclusion == "failure") | .steps[] | select(.conclusion == "failure") | .name'
    try:
        out = run("run", "view", run_id, "--json", "jobs", "--jq", query)
    except Exception:  # noqa: BLE001 - a missing detail must never stop the alert itself
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def failure_message(run_url: str, steps: list[str]) -> str:
    where = f"Failed step: **{', '.join(steps)}**." if steps else "Open the run to see which step failed."
    return (
        f"The daily data refresh failed: {run_url}\n\n{where}\n\n"
        "Until a run succeeds, the data is not being refreshed and the dashboard shows the last good pull. "
        "This issue closes itself when the next refresh succeeds."
    )


def report_failure(run: Runner, run_url: str, run_id: str = "") -> str:
    """Comment on the open failure issue, or open one. Returns what it did, for the log."""
    message = failure_message(run_url, failed_steps(run, run_id))
    existing = open_issues(run)
    if existing:
        run("issue", "comment", str(existing[0]), "--body", message)
        return f"commented on issue #{existing[0]}"
    run("label", "create", LABEL, "--color", LABEL_COLOUR, "--description", "The scheduled data refresh failed", "--force")
    created = run("issue", "create", "--title", TITLE, "--label", LABEL, "--body", message)
    return f"opened {created.strip()}"


def report_success(run: Runner, run_url: str) -> str:
    """Close every open failure issue with a comment that links the run that recovered."""
    existing = open_issues(run)
    for number in existing:
        run("issue", "close", str(number), "--comment", f"The daily data refresh succeeded again: {run_url}")
    return f"closed issue(s) {', '.join(f'#{n}' for n in existing)}" if existing else "nothing to close"


def main(argv: list[str], run: Runner = gh) -> int:
    if len(argv) != 2 or argv[1] not in ("failure", "success"):
        print("usage: python ops/refresh_alert.py failure|success", file=sys.stderr)
        return 2
    run_url = os.environ.get("RUN_URL", "(run link unavailable)")
    if argv[1] == "failure":
        print(report_failure(run, run_url, os.environ.get("RUN_ID", "")))
    else:
        print(report_success(run, run_url))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
