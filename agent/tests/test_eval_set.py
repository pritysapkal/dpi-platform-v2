"""The eval set itself: valid, and the questions about bots and the 90th percentile can tell a right answer
from a wrong one. Needs dev.duckdb (ground truth is SQL); no API key."""

import json

import pytest

from agent.config import DB_PATH
from agent.evals.run_evals import EVAL_SET_PATH, compute_ground_truth, score

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="dev.duckdb not built")

ITEMS = {q["id"]: q for q in json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))["questions"]}
TOOLS = [{"tool": "get_dora_metrics", "args": {}}]


def graded(qid, answer):
    item = ITEMS[qid]
    record = {"answer": answer, "status": "answered", "tool_calls": TOOLS, "ground_truth": compute_ground_truth(item)}
    return score(item, record)


def test_eval_set_ids_are_unique_and_every_ground_truth_query_runs():
    ids = [q["id"] for q in json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))["questions"]]
    assert len(ids) == len(set(ids)) == 23
    for item in ITEMS.values():
        compute_ground_truth(item)  # raises if any SQL is broken or returns no row


def test_new_questions_have_the_expected_ground_truth():
    truth = {qid: [round(n["value"], 2) for n in compute_ground_truth(ITEMS[qid])["numbers"]] for qid in ("q15", "q16", "q17")}
    assert truth == {"q15": [23.18, 92], "q16": [495.83], "q17": [664.95, 88]}  # 23.175 rounds half-up in print


def test_humans_only_question_passes_with_the_humans_figure_and_fails_if_bots_were_included():
    assert graded("q15", "Humans only, 2024: median 23.18 hours across 92 merged PRs.")["passed"]
    ignored_the_filter = graded("q15", "The median lead time in 2024 was 23.91 hours across 109 merged PRs.")
    assert ignored_the_filter["numbers"] is False and not ignored_the_filter["passed"]


def test_p90_question_fails_if_the_model_reports_the_median_or_the_mean_instead():
    assert graded("q16", "The 90th percentile lead time for 2024 was 495.83 hours.")["passed"]
    assert not graded("q16", "The median lead time for 2024 was 23.91 hours.")["passed"]
    assert not graded("q16", "The mean lead time for 2024 was 284.87 hours.")["passed"]


def test_combined_question_needs_both_the_humans_p90_and_the_count():
    assert graded("q17", "For human-written PRs in 2023 the p90 was 664.95 hours, with 88 merged.")["passed"]
    assert not graded("q17", "In 2023 the p90 was 312.98 hours with 149 merged PRs (bots included).")["passed"]
    assert not graded("q17", "The human p90 in 2023 was 664.95 hours.")["passed"]  # forgot the count
