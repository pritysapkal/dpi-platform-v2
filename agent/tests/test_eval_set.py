"""The eval set itself: valid, and the questions about bots and the 90th percentile can tell a right answer
from a wrong one. Needs a database (the ground truth is SQL); no API key.

The first test holds on any data. The rest pin the exact answers on the 50-PR fixture, worked out in plain Python by
fixtures/build_fixture.py (period_facts), not copied from the pipeline's output.
"""

import json

import pytest

import build_fixture as fx  # the answer key; put on the path by conftest.py
from agent.evals.run_evals import EVAL_SET_PATH, compute_ground_truth, score

ITEMS = {q["id"]: q for q in json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))["questions"]}
TOOLS = [{"tool": "get_dora_metrics", "args": {}}]


def graded(qid, answer):
    item = ITEMS[qid]
    record = {"answer": answer, "status": "answered", "tool_calls": TOOLS, "ground_truth": compute_ground_truth(item)}
    return score(item, record)


@pytest.mark.any_data
def test_eval_set_ids_are_unique_and_every_ground_truth_query_runs():
    ids = [q["id"] for q in json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))["questions"]]
    assert len(ids) == len(set(ids)) == 23
    for item in ITEMS.values():
        compute_ground_truth(item)  # raises if any SQL is broken or returns no row


@pytest.mark.fixture_numbers
def test_new_questions_have_the_expected_ground_truth():
    y2024, humans_2024 = fx.period_facts("2024-01-01", "2024-12-31"), fx.period_facts("2024-01-01", "2024-12-31", humans_only=True)
    humans_2023 = fx.period_facts("2023-01-01", "2023-12-31", humans_only=True)
    expected = {
        "q15": [humans_2024["median"], humans_2024["merged"]],  # 36.0 hours, 10 PRs
        "q16": [y2024["p90"]],                                  # 144.0 hours
        "q17": [humans_2023["p90"], humans_2023["merged"]],     # 48.0 hours, 12 PRs
    }
    truth = {qid: [n["value"] for n in compute_ground_truth(ITEMS[qid])["numbers"]] for qid in expected}
    assert truth == {qid: pytest.approx(values, abs=0.01) for qid, values in expected.items()}
    assert expected == {"q15": [36.0, 10], "q16": [144.0], "q17": [48.0, 12]}  # and the hand-checked figures themselves


@pytest.mark.fixture_numbers
def test_humans_only_question_passes_with_the_humans_figure_and_fails_if_bots_were_included():
    assert graded("q15", "Humans only, 2024: median 36 hours across 10 merged PRs.")["passed"]
    ignored_the_filter = graded("q15", "The median lead time in 2024 was 12.0 hours across 16 merged PRs.")
    assert ignored_the_filter["numbers"] is False and not ignored_the_filter["passed"]


@pytest.mark.fixture_numbers
def test_p90_question_fails_if_the_model_reports_the_median_or_the_mean_instead():
    assert graded("q16", "The 90th percentile lead time for 2024 was 144.0 hours.")["passed"]
    assert not graded("q16", "The median lead time for 2024 was 12.0 hours.")["passed"]
    assert not graded("q16", "The mean lead time for 2024 was 39.81 hours.")["passed"]


@pytest.mark.fixture_numbers
def test_combined_question_needs_both_the_humans_p90_and_the_count():
    assert graded("q17", "For human-written PRs in 2023 the p90 was 48.0 hours, with 12 merged.")["passed"]
    # with bots the p90 is also 48.0 on this data, so only the count (16, not 12) shows the filter was ignored
    assert not graded("q17", "In 2023 the p90 was 48.0 hours with 16 merged PRs (bots included).")["passed"]
    assert not graded("q17", "The human p90 in 2023 was 48.0 hours.")["passed"]  # forgot the count
