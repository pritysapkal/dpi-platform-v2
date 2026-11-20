"""was_approved_before_merge on fct_pull_requests, next to the older any-time was_approved.

The rule: a merged PR counts as approved before merge when its first APPROVED review came at or before merged_at.
The fixture has the awkward cases (PR 1010 approved one second after the merge, PR 1011 two days after), and the expected
figures are worked out from the case table in plain Python (fixtures/build_fixture.py), not from the pipeline.
"""

import pytest

import build_fixture as fx
import queries


def first_approval(pr):
    stamps = [x[3] for x in pr.reviews if x[2] == "APPROVED"]
    return min(stamps) if stamps else None


def approved_before_merge(pr):  # ISO timestamps compare correctly as text
    return bool(pr.merged) and first_approval(pr) is not None and first_approval(pr) <= pr.merged


def flags():
    return {n: (a, b) for n, a, b in queries.run("select pr_number, was_approved, was_approved_before_merge from main.fct_pull_requests")}


@pytest.mark.any_data
def test_approved_before_merge_is_always_a_stricter_version_of_the_other_flags():
    rows = queries.run(
        "select count(*) from main.fct_pull_requests where was_approved_before_merge "
        "and (not was_approved or not was_reviewed_before_merge or not is_merged)"
    )
    assert rows == [(0,)]


@pytest.mark.fixture_numbers
def test_a_merged_pr_approved_only_after_the_merge_is_not_approved_before_it_but_is_still_approved():
    by_number = {p.number: p for p in fx.CASES}
    # 1010: merged 22:00:00, approved 22:00:01 (one second late); 1011: approved two days after the merge
    assert by_number[1010].reviews[0][3] > by_number[1010].merged and by_number[1011].reviews[0][3] > by_number[1011].merged
    got = flags()
    assert got[1010] == (True, False) and got[1011] == (True, False)  # (was_approved at any time, was_approved_before_merge)
    assert got[1009] == (True, True)  # a review exactly at the merge instant is in time


@pytest.mark.fixture_numbers
def test_every_pr_matches_the_case_table_and_the_totals_are_22_and_25():
    got = flags()
    for pr in fx.CASES:
        assert got[pr.number] == (first_approval(pr) is not None, approved_before_merge(pr)), pr.number
    assert sum(a for a, _ in got.values()) == 25  # approved at any time, including 1010, 1011 and the open approved PRs
    assert sum(b for _, b in got.values()) == 22  # approved before the merge
