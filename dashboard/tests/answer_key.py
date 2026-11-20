"""The expected answers for the 50-PR fixture, worked out from the case table in plain Python.

No SQL, no dbt and no dashboard code is used here, so a test that compares the dashboard with these functions is
checking it against an independent calculation. fixtures/build_fixture.py holds the case table (CASES) and the
simplest totals (facts, period_facts); this file adds the figures that only the dashboard shows.

Conventions: a "window" is two datetime.date values, both ends included. A PR belongs to a window by the day it was
opened (funnel, open-PR age, authors) or the day it was merged (lead-time breakdown) or the day a review was submitted
(reviewers), the same choices the dashboard documents.
"""

import datetime
import statistics

import build_fixture as fx

EVERYTHING = (datetime.date(2000, 1, 1), datetime.date(2100, 1, 1))


def at(stamp):
    return fx._t(stamp)


def first_review(pr):
    return min((at(x[3]) for x in pr.reviews), default=None)


def first_approval(pr):
    return min((at(x[3]) for x in pr.reviews if x[2] == "APPROVED"), default=None)


def reviewed_before_merge(pr):
    return bool(pr.merged) and first_review(pr) is not None and first_review(pr) <= at(pr.merged)


def approved_before_merge(pr):
    return bool(pr.merged) and first_approval(pr) is not None and first_approval(pr) <= at(pr.merged)


def people(include_bots):
    return [p for p in fx.CASES if include_bots or not fx.is_bot(p.author)]


def opened_in(pr, window):
    return window[0] <= at(pr.created).date() <= window[1]


def merged_in(pr, window):
    return bool(pr.merged) and window[0] <= at(pr.merged).date() <= window[1]


def funnel_expected(include_bots, window=EVERYTHING):
    """review_funnel(): PRs opened in the window. "Reviewed" and "approved" mean before the merge for a merged PR."""
    prs = [p for p in people(include_bots) if opened_in(p, window)]
    return {
        "opened": len(prs),
        "reviewed": sum(1 for p in prs if p.reviews and (not p.merged or reviewed_before_merge(p))),
        "approved": sum(1 for p in prs if first_approval(p) and (not p.merged or approved_before_merge(p))),
        "merged": sum(1 for p in prs if p.merged),
        "merged_without_review": sum(1 for p in prs if p.merged and not reviewed_before_merge(p)),
        "merged_without_approval": sum(1 for p in prs if p.merged and not approved_before_merge(p)),
        "closed_without_merge": sum(1 for p in prs if p.closed),
        "still_open": sum(1 for p in prs if p.state == "open"),
    }


def breakdown_expected(window=EVERYTHING):
    """Human PRs merged in the window and reviewed before the merge: hours waiting for the first review, hours in review."""
    merged = [p for p in fx.CASES if not fx.is_bot(p.author) and merged_in(p, window)]
    split = [p for p in merged if reviewed_before_merge(p)]
    wait = [(first_review(p) - at(p.created)).total_seconds() / 3600 for p in split]
    review = [(at(p.merged) - first_review(p)).total_seconds() / 3600 for p in split]
    lead = [(at(p.merged) - at(p.created)).total_seconds() / 3600 for p in split]
    return {
        "split": len(split), "not_split": len(merged) - len(split),
        "median_wait": statistics.median(wait) if split else None,
        "median_review": statistics.median(review) if split else None,
        "median_lead": statistics.median(lead) if split else None,
        "wait_share": 100.0 * sum(wait) / (sum(wait) + sum(review)) if split else None,
    }


def reviewers_expected(window=EVERYTHING, limit=10):
    """(reviewer, PRs reviewed, reviews, approvals) for people, not counting comments on their own PR; plus how many were left out."""
    seen, reviews, approvals, own = {}, {}, {}, 0
    for p in fx.CASES:
        for _, who, state, stamp in p.reviews:
            if fx.is_bot(who) or not window[0] <= at(stamp).date() <= window[1]:
                continue
            if who == p.author:
                own += 1
                continue
            seen.setdefault(who, set()).add(p.number)
            reviews[who] = reviews.get(who, 0) + 1
            approvals[who] = approvals.get(who, 0) + (state == "APPROVED")
    rows = sorted(((who, len(seen[who]), reviews[who], approvals[who]) for who in seen), key=lambda r: (-r[1], -r[2], r[0]))
    return rows[:limit], own


def authors_expected(window=EVERYTHING, limit=10):
    """(author, PRs opened, merged, still open) for people, most PRs first."""
    rows = {}
    for p in fx.CASES:
        if fx.is_bot(p.author) or not opened_in(p, window):
            continue
        n, m, o = rows.get(p.author, (0, 0, 0))
        rows[p.author] = (n + 1, m + bool(p.merged), o + (p.state == "open"))
    return sorted(((a, *v) for a, v in rows.items()), key=lambda r: (-r[1], -r[2], r[0]))[:limit]


def open_prs_expected(include_bots, window=EVERYTHING):
    """The PRs still open when the data was pulled, opened in the window, as (age in days, PR), oldest first."""
    rows = [((fx.PULLED_AT - at(p.created)).total_seconds() / 86400.0, p) for p in people(include_bots) if p.state == "open" and opened_in(p, window)]
    return sorted(rows, key=lambda r: (-r[0], r[1].number))


def activity_expected(window, include_bots):
    """Did anything happen? A PR opened or merged (bot PRs only if include_bots), or any review submitted."""
    for p in people(include_bots):
        if any(stamp and window[0] <= at(stamp).date() <= window[1] for stamp in (p.created, p.merged)):
            return True
    return any(window[0] <= at(x[3]).date() <= window[1] for p in fx.CASES for x in p.reviews)
