"""The eval report: a model that could not be evaluated is labelled as such, not as 'pending'."""

import json

import pytest

from agent.evals import run_evals

UNAVAILABLE = "not evaluated — model unavailable (503/504) on 2026-11-18"
ITEMS = [{"id": "q01", "type": "metric", "question": "How many PRs?", "tools_any_of": [["get_dora_metrics"]],
          "numbers": [{"label": "n", "sql": "select 1", "tolerance": 0}]}]


def saved_run(model, status="answered"):
    return {
        "id": "q01", "question": "How many PRs?", "answer": "There were 109 PRs.", "status": status,
        "run_at": "2026-11-18T19:00:00", "temperature": 0, "model_requested": model, "model_answered": model,
        "tool_calls": [{"tool": "get_dora_metrics", "args": {}}], "duration_s": 1.0, "trace": None,
        "ground_truth": {"numbers": [{"label": "n", "value": 109, "tolerance": 0}], "texts": []},
    }


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(run_evals, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(run_evals, "HISTORY_DIR", tmp_path / "history")
    # write_report() writes the report file as well as returning it, so it MUST be pointed away from the
    # real agent/evals/results.md. An earlier version of this fixture forgot, and the tests overwrote the
    # real report with their one-question dummy data every time they ran.
    monkeypatch.setattr(run_evals, "RESULTS_PATH", tmp_path / "results.md")
    return tmp_path


def write_run(runs_dir, model, **kwargs):
    (runs_dir / f"{model}.jsonl").write_text(json.dumps(saved_run(model, **kwargs)) + "\n", encoding="utf-8")


def test_a_model_with_no_run_that_is_marked_unavailable_says_so_in_both_tables(runs_dir):
    write_run(runs_dir, "gemini-3.5-flash-lite")
    report = run_evals.write_report(ITEMS)
    scorecard = next(line for line in report.splitlines() if line.startswith("| `gemini-3.8-flash`"))
    assert UNAVAILABLE in scorecard and "pending" not in scorecard
    question_row = next(line for line in report.splitlines() if line.startswith("| q01 |"))
    assert "not evaluated" in question_row and "pending" not in question_row
    assert "**PASS**" in question_row  # the model that was evaluated still shows its result


def test_a_model_without_a_reason_still_shows_pending(runs_dir):
    write_run(runs_dir, "gemini-3.8-flash")
    (runs_dir / "gemini-3.8-flash.jsonl").unlink()
    write_run(runs_dir, "gemini-3.5-flash-lite")
    original = dict(run_evals.NOT_EVALUATED)
    run_evals.NOT_EVALUATED.clear()
    try:
        scorecard = next(l for l in run_evals.write_report(ITEMS).splitlines() if l.startswith("| `gemini-3.8-flash`"))
    finally:
        run_evals.NOT_EVALUATED.update(original)
    assert "| pending |" in scorecard


def test_once_a_real_run_exists_it_is_reported_normally_not_as_unavailable(runs_dir):
    write_run(runs_dir, "gemini-3.8-flash")
    write_run(runs_dir, "gemini-3.5-flash-lite")
    report = run_evals.write_report(ITEMS)
    scorecard = next(line for line in report.splitlines() if line.startswith("| `gemini-3.8-flash`"))
    assert UNAVAILABLE not in scorecard and "complete" in scorecard
    assert "not evaluated" not in next(line for line in report.splitlines() if line.startswith("| q01 |"))


def test_the_checked_in_label_matches_the_readme_wording():
    assert run_evals.NOT_EVALUATED["gemini-3.8-flash"] == UNAVAILABLE


def test_building_a_report_in_a_test_never_touches_the_real_results_file(runs_dir):
    real = run_evals.RESULTS_PATH.__class__(run_evals.EVALS_DIR / "results.md")
    before = real.read_bytes() if real.exists() else None
    write_run(runs_dir, "gemini-3.5-flash-lite")
    run_evals.write_report(ITEMS)
    assert (real.read_bytes() if real.exists() else None) == before  # the real report is untouched
    assert run_evals.RESULTS_PATH.parent == runs_dir  # and the report went to the temporary folder
    assert "1 questions" in (runs_dir / "results.md").read_text(encoding="utf-8")


def test_the_real_results_file_describes_the_real_eval_set_not_a_test_fixture():
    # Guards the file that is committed: it must list every question in eval_set.json.
    import json

    items = json.loads(run_evals.EVAL_SET_PATH.read_text(encoding="utf-8"))["questions"]
    text = (run_evals.EVALS_DIR / "results.md").read_text(encoding="utf-8")
    assert f"{len(items)} questions:" in text, "results.md was not generated from the full eval set"
    for item in items:
        assert f"| {item['id']} |" in text
