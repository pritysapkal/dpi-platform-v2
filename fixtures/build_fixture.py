"""A small, hand-written stand-in for the raw GitHub data, used by tests.

Why it exists: the real data changes every day, so a test that says "there were 288 merged PRs" is only
true on one particular day. These ~50 pull requests never change, and each one is here on purpose: it is
a situation the pipeline has to get right (a review that arrives after the merge, a bot PR, a PR that is
still open after three years...). Every case has a `tags` entry saying what it is for, and a test checks
that none of the cases is quietly removed.

What it writes: two CSV files whose columns match the raw tables that `fetch_prs.py` produces, column
for column (`fixtures/raw_pull_requests.csv`, `fixtures/raw_pr_reviews.csv`). `load_fixture.py` turns them
into the `raw` tables in a DuckDB file, and then the normal `dbt build` runs on top, exactly as on live data.

There is no randomness: running this file always writes identical CSVs. `facts()` works out the expected
answers (merged counts, medians, review coverage...) straight from the case table, in plain Python and
independently of SQL and dbt, so tests can compare the pipeline's answers against it.

    python fixtures/build_fixture.py            # (re)write the two CSV files
    python fixtures/build_fixture.py --facts    # print the expected answers
"""

import csv
import datetime
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
PRS_CSV = FIXTURE_DIR / "raw_pull_requests.csv"
REVIEWS_CSV = FIXTURE_DIR / "raw_pr_reviews.csv"

# When the pretend fetch ran. Everything in the data happened before this moment.
LOADED_AT = "2026-01-15 12:00:00"
PULLED_AT = datetime.datetime(2026, 1, 15, 12, 0, 0)

PR_COLUMNS = ["number", "title", "state", "merged", "author", "created_at", "updated_at", "merged_at", "closed_at", "_loaded_at"]
REVIEW_COLUMNS = ["pr_number", "review_id", "reviewer_login", "review_state", "submitted_at", "_loaded_at"]


@dataclass(frozen=True)
class PR:
    number: int
    title: str
    author: str
    created: str
    merged: str | None = None  # when it was merged; None if it was not
    closed: str | None = None  # when it was closed WITHOUT merging; None if merged or still open
    reviews: tuple = ()  # (review_id, reviewer, state, submitted_at)
    tags: tuple = ()  # what this case is for

    @property
    def state(self) -> str:
        return "closed" if (self.merged or self.closed) else "open"

    @property
    def closed_at(self) -> str | None:
        return self.merged or self.closed  # GitHub reports the merge time as the close time too

    @property
    def updated_at(self) -> str:
        times = [self.created, self.merged, self.closed] + [r[3] for r in self.reviews]
        return max(t for t in times if t)  # same fixed format, so text order is time order


def r(review_id, reviewer, state, at):
    return (review_id, reviewer, state, at)


# fmt: off
CASES = [
    # ---------------------------------------------------------------- 2023
    PR(1001, "Add retry hook to the transport", "alice", "2023-01-01T09:00:00Z", merged="2023-01-02T09:00:00Z",
       reviews=(r(5001, "bob", "APPROVED", "2023-01-01T15:00:00Z"),),
       tags=("window_start_day", "normal_reviewed_merge")),
    PR(1002, "Bump certifi from 2022.12.7 to 2023.1.1", "dependabot[bot]", "2023-01-09T08:00:00Z", merged="2023-01-09T09:00:00Z",
       tags=("bot_merged_no_review",)),
    PR(1003, "Document the timeout parameters", "bob", "2023-02-06T10:00:00Z", merged="2023-02-08T10:00:00Z",
       reviews=(r(5002, "carol", "COMMENTED", "2023-02-06T12:00:00Z"), r(5003, "alice", "APPROVED", "2023-02-07T10:00:00Z")),
       tags=("multi_review",)),
    PR(1004, "Experiment: switch the default pool size", "carol", "2023-02-13T10:00:00Z", closed="2023-02-20T10:00:00Z",
       reviews=(r(5004, "dave", "CHANGES_REQUESTED", "2023-02-14T10:00:00Z"),),
       tags=("human_closed_unmerged_reviewed",)),
    PR(1005, "Fix a typo in the quickstart", "dave", "2023-03-01T10:00:00Z", merged="2023-03-01T20:00:00Z",
       reviews=(r(5005, "alice", "APPROVED", "2023-03-01T11:00:00Z"),),
       tags=("normal_reviewed_merge",)),
    PR(1006, "Bump pytest from 7.2.0 to 7.2.1", "dependabot[bot]", "2023-03-06T08:00:00Z", merged="2023-03-06T09:00:00Z",
       tags=("bot_merged_no_review",)),
    PR(1007, "Rework the redirect handling", "erin", "2023-04-03T10:00:00Z", merged="2023-04-10T10:00:00Z",
       reviews=(r(5006, "frank", "APPROVED", "2023-04-09T10:00:00Z"),),
       tags=("slow_merge_week",)),
    PR(1008, "Tidy the release script", "alice", "2023-04-17T09:00:00Z", merged="2023-04-17T21:00:00Z",
       tags=("human_merged_no_review",)),
    PR(1009, "Handle empty response bodies", "frank", "2023-05-02T10:00:00Z", merged="2023-05-03T10:00:00Z",
       reviews=(r(5007, "grace", "APPROVED", "2023-05-03T10:00:00Z"),),  # the review lands in the same second as the merge
       tags=("review_at_merge_instant",)),
    PR(1010, "Add a changelog entry", "ivan", "2023-06-05T10:00:00Z", merged="2023-06-05T22:00:00Z",
       reviews=(r(5008, "grace", "APPROVED", "2023-06-05T22:00:01Z"),),  # one second AFTER the merge
       tags=("review_one_second_after_merge", "only_post_merge_review")),
    PR(1011, "Support custom user agents", "judy", "2023-06-12T10:00:00Z", merged="2023-06-13T10:00:00Z",
       reviews=(r(5009, "bob", "APPROVED", "2023-06-15T10:00:00Z"),),  # two days after the merge
       tags=("only_post_merge_review",)),
    PR(1012, "Proposal: add HTTP/3 support", "mallory", "2023-07-03T10:00:00Z",
       tags=("open_ancient_never_reviewed",)),
    PR(1013, "Bump idna from 3.3 to 3.4", "dependabot[bot]", "2023-07-10T08:00:00Z", closed="2023-07-17T08:00:00Z",
       tags=("bot_closed_unmerged",)),
    PR(1014, 'Fix "unclosed" client, again', "alice", "2023-08-07T10:00:00Z", merged="2023-08-09T10:00:00Z",
       # review ids are deliberately NOT in time order (6003 is the earliest review)
       reviews=(r(6003, "grace", "CHANGES_REQUESTED", "2023-08-07T14:00:00Z"), r(6001, "grace", "COMMENTED", "2023-08-08T10:00:00Z"),
                r(6002, "bob", "APPROVED", "2023-08-08T16:00:00Z")),
       tags=("changes_requested_then_approved", "out_of_order_review_ids", "multi_review")),
    PR(1015, "Speed up header parsing", "robot-dev", "2023-09-04T10:00:00Z", merged="2023-09-06T10:00:00Z",
       reviews=(r(5010, "alice", "APPROVED", "2023-09-05T10:00:00Z"),),
       tags=("lookalike_human_login", "normal_reviewed_merge")),  # "robot-dev" has "bot" in it but is a person
    PR(1016, "Clarify the proxy docs", "erin", "2023-10-02T10:00:00Z", merged="2023-10-02T16:00:00Z",
       reviews=(r(5011, "erin", "COMMENTED", "2023-10-02T12:00:00Z"), r(5012, "alice", "APPROVED", "2023-10-02T14:00:00Z")),
       tags=("self_comment",)),  # the author replies in their own PR's thread
    PR(1017, "Bump charset-normalizer from 3.0.0 to 3.0.1", "dependabot[bot]", "2023-10-09T08:00:00Z", merged="2023-10-09T09:00:00Z",
       tags=("bot_merged_no_review",)),
    # November 2023: nothing happens at all (no PR opened, merged or reviewed)
    PR(1018, "Add a .gitattributes file", "heidi", "2023-12-04T10:00:00Z", merged="2023-12-05T10:00:00Z",
       reviews=(r(5013, "carol", "APPROVED", "2023-12-04T16:00:00Z"),),
       tags=("normal_reviewed_merge",)),
    PR(1019, "Bump sniffio from 1.3.0 to 1.3.1", "dependabot[bot]", "2023-12-11T08:00:00Z", merged="2023-12-11T10:00:00Z",
       tags=("bot_merged_no_review",)),
    PR(1020, "Refactor the auth flow", "carol", "2023-12-28T10:00:00Z", merged="2024-01-02T10:00:00Z",
       reviews=(r(5014, "dave", "APPROVED", "2024-01-01T10:00:00Z"),),
       tags=("year_boundary",)),
    # ---------------------------------------------------------------- 2024
    PR(1021, "Add a streaming example", "bob", "2024-01-15T10:00:00Z", merged="2024-01-16T10:00:00Z",
       reviews=(r(5015, "alice", "APPROVED", "2024-01-15T13:00:00Z"),),
       tags=("normal_reviewed_merge",)),
    PR(1022, "Bump h11 from 0.14.0 to 0.14.1", "dependabot[bot]", "2024-02-05T08:00:00Z", merged="2024-02-05T09:00:00Z",
       tags=("bot_merged_no_review",)),
    PR(1023, "Fix leap-day date parsing", "alice", "2024-02-29T10:00:00Z", merged="2024-02-29T22:00:00Z",
       reviews=(r(5016, "bob", "APPROVED", "2024-02-29T12:00:00Z"),),
       tags=("leap_day",)),
    PR(1024, "Draft: new connection limits", "carol", "2024-03-04T10:00:00Z",
       reviews=(r(5017, "dave", "COMMENTED", "2024-03-05T10:00:00Z"),),
       tags=("open_reviewed_not_approved",)),
    PR(1025, "Make timeouts configurable per request", "dave", "2024-03-11T10:00:00Z", merged="2024-03-18T10:00:00Z",
       reviews=(r(5018, "erin", "APPROVED", "2024-03-17T10:00:00Z"),),
       tags=("slow_merge_week",)),
    PR(1026, "Bump anyio from 4.2.0 to 4.3.0", "dependabot[bot]", "2024-04-01T08:00:00Z", merged="2024-04-01T10:00:00Z",
       tags=("bot_merged_no_review",)),
    PR(1027, "Try an alternative parser", "ivan", "2024-04-08T10:00:00Z", closed="2024-04-12T10:00:00Z",
       tags=("human_closed_unmerged_unreviewed",)),
    PR(1028, "Improve the error messages", "erin", "2024-05-06T10:00:00Z", merged="2024-05-13T10:00:00Z",
       reviews=(r(5019, "frank", "COMMENTED", "2024-05-07T10:00:00Z"), r(5020, "grace", "APPROVED", "2024-05-12T10:00:00Z")),
       tags=("multi_review", "slow_merge_week")),
    PR(1029, "Update the contributing guide", "alice", "2024-06-03T10:00:00Z", merged="2024-06-03T14:00:00Z",
       tags=("human_merged_no_review",)),
    PR(1030, "Bump cryptography from 42.0.0 to 42.0.1", "dependabot[bot]", "2024-06-10T08:00:00Z", merged="2024-06-10T09:00:00Z",
       tags=("bot_merged_no_review",)),
    PR(1031, "Add typing for the event hooks", "frank", "2024-07-01T10:00:00Z", merged="2024-07-02T10:00:00Z",
       reviews=(r(5021, "bob", "APPROVED", "2024-07-01T11:00:00Z"),),
       tags=("normal_reviewed_merge",)),
    # August 2024: only bots merge anything
    PR(1032, "Bump trio from 0.24.0 to 0.25.0", "dependabot[bot]", "2024-08-05T08:00:00Z", merged="2024-08-05T09:00:00Z",
       tags=("bot_merged_no_review", "bot_only_month")),
    PR(1033, "Bump httpcore from 1.0.4 to 1.0.5", "dependabot[bot]", "2024-08-12T08:00:00Z", merged="2024-08-12T11:00:00Z",
       tags=("bot_merged_no_review", "bot_only_month")),
    PR(1034, "Add an experimental cache layer", "mallory", "2024-09-02T10:00:00Z", closed="2024-09-30T10:00:00Z",
       reviews=(r(5022, "grace", "COMMENTED", "2024-09-03T10:00:00Z"),),
       tags=("human_closed_unmerged_reviewed",)),
    PR(1035, "Fix connection reuse after a timeout", "heidi", "2024-10-07T10:00:00Z", merged="2024-10-09T10:00:00Z",
       reviews=(r(5023, "copilot-pull-request-reviewer[bot]", "COMMENTED", "2024-10-07T10:30:00Z"), r(5024, "carol", "APPROVED", "2024-10-08T10:00:00Z")),
       tags=("bot_reviewer", "multi_review")),
    PR(1036, "Allow trailing slashes in base URLs", "judy", "2024-10-21T10:00:00Z", merged="2024-10-23T10:00:00Z",
       reviews=(r(5025, "alice", "APPROVED", "2024-10-22T10:00:00Z"),),
       tags=("normal_reviewed_merge",)),
    PR(1037, "Bump pydantic from 2.5.0 to 2.5.1", "dependabot[bot]", "2024-11-18T08:00:00Z", merged="2024-11-18T09:00:00Z",
       tags=("bot_merged_no_review", "two_merges_same_day")),
    PR(1038, "Fix the cookie jar copy", "bob", "2024-11-18T10:00:00Z", merged="2024-11-18T22:00:00Z",
       reviews=(r(5026, "carol", "APPROVED", "2024-11-18T12:00:00Z"),),
       tags=("two_merges_same_day",)),
    # December 2024: a PR is opened and reviewed but nothing is merged
    PR(1039, "Prototype: drop support for old TLS", "alice", "2024-12-09T10:00:00Z", closed="2025-02-10T10:00:00Z",
       reviews=(r(5027, "bob", "CHANGES_REQUESTED", "2024-12-10T10:00:00Z"),),
       tags=("human_closed_unmerged_reviewed", "merge_free_month_with_activity")),
    # ---------------------------------------------------------------- 2025
    PR(1040, "Add a progress callback", "alice", "2025-01-13T10:00:00Z", merged="2025-01-14T10:00:00Z",
       reviews=(r(5028, "bob", "APPROVED", "2025-01-13T12:00:00Z"),),
       tags=("normal_reviewed_merge",)),
    PR(1041, "Bump pytest-asyncio from 0.23.0 to 0.23.1", "dependabot[bot]", "2025-02-03T08:00:00Z", merged="2025-02-03T09:00:00Z",
       tags=("bot_merged_no_review",)),
    PR(1042, "Support Brotli content encoding", "dave", "2025-03-03T10:00:00Z", merged="2025-03-10T10:00:00Z",
       reviews=(r(5029, "alice", "APPROVED", "2025-03-04T10:00:00Z"),),
       tags=("slow_merge_week",)),
    PR(1043, "WIP: rewrite the connection pool", "erin", "2025-04-07T10:00:00Z",
       reviews=(r(5030, "frank", "COMMENTED", "2025-04-08T10:00:00Z"),),
       tags=("open_reviewed_not_approved",)),
    PR(1044, "Add a timeout example to the docs", "ivan", "2025-05-05T10:00:00Z", merged="2025-05-06T10:00:00Z",
       reviews=(r(5031, "grace", "APPROVED", "2025-05-05T14:00:00Z"),),
       tags=("normal_reviewed_merge",)),
    PR(1045, "Fix a flaky test", "alice", "2025-06-02T10:00:00Z", merged="2025-06-02T11:00:00Z",
       tags=("human_merged_no_review",)),
    PR(1046, "Bump typing-extensions from 4.9.0 to 4.10.0", "dependabot[bot]", "2025-07-07T08:00:00Z", closed="2025-07-14T08:00:00Z",
       tags=("bot_closed_unmerged",)),
    PR(1047, "Expose the response elapsed time", "frank", "2025-09-01T10:00:00Z", merged="2025-09-03T10:00:00Z",
       reviews=(r(5032, "heidi", "APPROVED", "2025-09-02T10:00:00Z"),),
       tags=("normal_reviewed_merge",)),
    PR(1048, "Bump idna from 3.6 to 3.7", "dependabot[bot]", "2025-11-03T08:00:00Z", merged="2025-11-03T09:00:00Z",
       tags=("bot_merged_no_review",)),
    # December 2025: nothing happens
    # ---------------------------------------------------------------- 2026 (the fetch ran on 15 January)
    PR(1049, "Add support for HTTP proxies with auth", "judy", "2026-01-05T10:00:00Z",
       reviews=(r(5033, "alice", "APPROVED", "2026-01-06T10:00:00Z"),),
       tags=("open_recent_approved",)),
    PR(1050, "Question: how do I disable redirects?", "mallory", "2026-01-12T10:00:00Z",
       tags=("open_recent_never_reviewed",)),
]
# fmt: on


# ---------------------------------------------------------------- writing the CSV files

def pr_rows():
    for p in CASES:
        yield [p.number, p.title, p.state, "true" if p.merged else "false", p.author, p.created, p.updated_at,
               p.merged or "", p.closed_at or "", LOADED_AT]


def review_rows():
    for p in CASES:
        for review_id, reviewer, state, at in p.reviews:
            yield [p.number, review_id, reviewer, state, at, LOADED_AT]


def render(columns, rows) -> str:
    """The exact text of a CSV file (UTF-8, Unix line endings), so two runs can be compared byte for byte."""
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue()


def write_csvs(directory: Path = FIXTURE_DIR) -> None:
    (directory / PRS_CSV.name).write_text(render(PR_COLUMNS, pr_rows()), encoding="utf-8", newline="")
    (directory / REVIEWS_CSV.name).write_text(render(REVIEW_COLUMNS, review_rows()), encoding="utf-8", newline="")


# ---------------------------------------------------------------- the expected answers

def _t(text: str) -> datetime.datetime:
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def _percentile(values, q):
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def is_bot(login: str) -> bool:
    return login.endswith("[bot]")


def facts() -> dict:
    """What the pipeline should report for this data, worked out directly from the case table in plain Python.

    Lead time is hours from opened to merged. "Reviewed before merge" means the first review was submitted at or
    before the merge moment. Nothing here uses SQL or dbt, so it is an independent check on them.
    """
    merged = [p for p in CASES if p.merged]
    human_merged = [p for p in merged if not is_bot(p.author)]

    def hours(p):
        return (_t(p.merged) - _t(p.created)).total_seconds() / 3600

    def reviewed_before_merge(p):
        return bool(p.reviews) and min(_t(x[3]) for x in p.reviews) <= _t(p.merged)

    leads = [hours(p) for p in merged]
    human_leads = [hours(p) for p in human_merged]
    open_prs = [p for p in CASES if p.state == "open"]
    return {
        "prs": len(CASES),
        "reviews": sum(len(p.reviews) for p in CASES),
        "merged": len(merged),
        "closed_without_merge": sum(1 for p in CASES if p.closed),
        "open": len(open_prs),
        "bot_prs": sum(1 for p in CASES if is_bot(p.author)),
        "bot_merged": len(merged) - len(human_merged),
        "human_merged": len(human_merged),
        "merged_by_year": {y: sum(1 for p in merged if p.merged.startswith(str(y))) for y in (2023, 2024, 2025)},
        "human_merged_reviewed_before_merge": sum(1 for p in human_merged if reviewed_before_merge(p)),
        "human_merged_not_reviewed_before_merge": sum(1 for p in human_merged if not reviewed_before_merge(p)),
        "median_lead_time_hours": statistics.median(leads),
        "p90_lead_time_hours": _percentile(leads, 0.9),
        "mean_lead_time_hours": statistics.fmean(leads),
        "human_median_lead_time_hours": statistics.median(human_leads),
        "human_p90_lead_time_hours": _percentile(human_leads, 0.9),
        "open_never_reviewed": sum(1 for p in open_prs if not p.reviews),
        "human_authors": len({p.author for p in CASES if not is_bot(p.author)}),
        "human_reviewers": len({x[1] for p in CASES for x in p.reviews if not is_bot(x[1])}),
        "logins": len({p.author for p in CASES} | {x[1] for p in CASES for x in p.reviews}),
    }


REQUIRED_TAGS = {
    "window_start_day", "normal_reviewed_merge", "multi_review", "slow_merge_week", "bot_merged_no_review",
    "bot_closed_unmerged", "bot_reviewer", "bot_only_month", "lookalike_human_login",
    "human_merged_no_review", "only_post_merge_review", "review_at_merge_instant", "review_one_second_after_merge",
    "changes_requested_then_approved", "out_of_order_review_ids", "self_comment",
    "human_closed_unmerged_reviewed", "human_closed_unmerged_unreviewed",
    "open_ancient_never_reviewed", "open_reviewed_not_approved", "open_recent_approved", "open_recent_never_reviewed",
    "year_boundary", "leap_day", "two_merges_same_day", "merge_free_month_with_activity",
}


# ---------------------------------------------------------------- documentation generated from the data

TAG_MEANING = {
    "window_start_day": "created on 2023-01-01, the first day of the data window",
    "normal_reviewed_merge": "an ordinary PR: reviewed, approved, merged",
    "multi_review": "several reviews from different people on one PR",
    "slow_merge_week": "took a week to merge, so the lead-time tail is not empty",
    "bot_merged_no_review": "a dependabot PR merged within hours, never reviewed",
    "bot_closed_unmerged": "a bot PR closed without merging",
    "bot_reviewer": "the first review comes from a bot (`copilot-pull-request-reviewer[bot]`)",
    "bot_only_month": "a month (August 2024) in which only bots merge anything",
    "lookalike_human_login": "`robot-dev` contains \"bot\" but is a person, so `is_bot` must be false",
    "human_merged_no_review": "a person merged their own PR with no review at all",
    "only_post_merge_review": "the only review arrived after the PR was already merged",
    "review_at_merge_instant": "first review in the same second as the merge: counts as before the merge",
    "review_one_second_after_merge": "first review one second after the merge: does not count as before",
    "changes_requested_then_approved": "CHANGES_REQUESTED, then a comment, then APPROVED",
    "out_of_order_review_ids": "review ids are not in time order, so the first review is not the lowest id",
    "self_comment": "the author comments on their own PR (not a review of someone else's work)",
    "human_closed_unmerged_reviewed": "a person's PR closed without merging, after a review",
    "human_closed_unmerged_unreviewed": "a person's PR closed without merging, never reviewed",
    "open_ancient_never_reviewed": "still open, created in 2023, never reviewed (over two years old when pulled)",
    "open_reviewed_not_approved": "still open, has comments but no approval",
    "open_recent_approved": "still open, created days before the pull, already approved",
    "open_recent_never_reviewed": "still open, created days before the pull, no review yet",
    "year_boundary": "opened in one year, merged in the next",
    "leap_day": "opened and merged on 29 February 2024",
    "two_merges_same_day": "two PRs merged on 2024-11-18 (checks date ranges are inclusive)",
    "merge_free_month_with_activity": "December 2024: a PR is opened and reviewed, but nothing is merged",
}


def catalogue_markdown() -> str:
    by_tag = {}
    for p in CASES:
        for tag in p.tags:
            by_tag.setdefault(tag, []).append(p.number)
    lines = ["| Case (tag) | PRs | What it is for |", "|---|---|---|"]
    for tag in TAG_MEANING:
        numbers = ", ".join(str(n) for n in by_tag.get(tag, []))
        lines.append(f"| `{tag}` | {numbers} | {TAG_MEANING[tag]} |")
    return "\n".join(lines)


FACT_LABELS = [
    ("PRs", "prs"), ("reviews", "reviews"), ("merged", "merged"), ("closed without merge", "closed_without_merge"),
    ("still open", "open"), ("bot-authored PRs", "bot_prs"), ("merged by bots", "bot_merged"),
    ("merged by people", "human_merged"), ("people's merges reviewed before the merge", "human_merged_reviewed_before_merge"),
    ("people's merges not reviewed before the merge", "human_merged_not_reviewed_before_merge"),
    ("open PRs never reviewed", "open_never_reviewed"), ("people who authored a PR", "human_authors"),
    ("people who reviewed", "human_reviewers"), ("distinct logins (people and bots)", "logins"),
]


def facts_markdown() -> str:
    f = facts()
    lines = ["| Fact | Value |", "|---|---|"]
    lines += [f"| {label} | {f[key]} |" for label, key in FACT_LABELS]
    lines += [f"| merged in {y} | {n} |" for y, n in f["merged_by_year"].items()]
    for label, key in (("median lead time, all merged (hours)", "median_lead_time_hours"), ("p90 lead time, all merged (hours)", "p90_lead_time_hours"),
                       ("mean lead time, all merged (hours)", "mean_lead_time_hours"), ("median lead time, people only (hours)", "human_median_lead_time_hours"),
                       ("p90 lead time, people only (hours)", "human_p90_lead_time_hours")):
        lines.append(f"| {label} | {f[key]:.2f} |")
    return "\n".join(lines)


README = FIXTURE_DIR / "README.md"
BLOCKS = {"catalogue": catalogue_markdown, "facts": facts_markdown}


def block_text(readme_text: str, name: str) -> str:
    start, end = f"<!-- {name}:start -->\n", f"\n<!-- {name}:end -->"
    return readme_text[readme_text.index(start) + len(start): readme_text.index(end)]


def update_readme() -> None:
    text = README.read_text(encoding="utf-8")
    for name, make in BLOCKS.items():
        start, end = f"<!-- {name}:start -->\n", f"\n<!-- {name}:end -->"
        i = text.index(start) + len(start)
        text = text[:i] + make() + text[text.index(end):]
    README.write_text(text, encoding="utf-8", newline="")


if __name__ == "__main__":
    if "--facts" in sys.argv:
        for key, value in facts().items():
            print(f"{key:42s} {value}")
    elif "--update-readme" in sys.argv:
        update_readme()
        print("updated the generated tables in fixtures/README.md")
    else:
        write_csvs()
        print(f"wrote {PRS_CSV.name} ({len(CASES)} PRs) and {REVIEWS_CSV.name} ({facts()['reviews']} reviews)")
