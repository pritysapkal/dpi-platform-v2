"""Offline checks for fetch_prs.py: fake HTTP responses and a fake sleep, so no network,
no GitHub token, no database, and no real waiting. Run from the project root:

    python -m pytest ingestion_tests -v
"""

import httpx
import pytest

import fetch_prs
from fetch_prs import GitHubFetchError, get_with_retry

URL = "https://api.github.com/repos/encode/httpx/pulls"
NOW = 1_000_000.0


def fake_response(status, headers=None, json=None):
    # A real httpx.Response (with a request attached), so is_success/text behave as in production.
    return httpx.Response(status, headers=headers or {}, json=json if json is not None else {},
                          request=httpx.Request("GET", URL))


class Script:
    """Replays a list of responses/exceptions in order and records every call and sleep."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.sleeps = []

    def http_get(self, url, headers=None, params=None, timeout=None):
        assert timeout is fetch_prs.HTTP_TIMEOUT, "every request must carry the explicit timeout"
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def run(self):
        return get_with_retry(URL, {}, http_get=self.http_get, sleep=self.sleep, now=lambda: NOW)


def test_success_first_try_does_not_sleep():
    s = Script(fake_response(200, json=[{"number": 1}]))
    assert s.run().json() == [{"number": 1}]
    assert s.calls == 1 and s.sleeps == []


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_retried_with_backoff_then_succeeds(status):
    s = Script(fake_response(status), fake_response(status), fake_response(200))
    assert s.run().is_success
    assert s.calls == 3
    # backoff: 2s then 4s, each plus 0-1s of jitter
    assert 2 <= s.sleeps[0] <= 3 and 4 <= s.sleeps[1] <= 5


def test_429_with_retry_after_waits_exactly_what_github_says():
    s = Script(fake_response(429, {"retry-after": "7"}), fake_response(200))
    s.run()
    assert s.sleeps == [7.0]


def test_429_without_hint_uses_backoff():
    s = Script(fake_response(429), fake_response(200))
    s.run()
    assert 2 <= s.sleeps[0] <= 3


def test_secondary_rate_limit_403_with_retry_after_is_retried():
    s = Script(fake_response(403, {"retry-after": "5"}), fake_response(200))
    s.run()
    assert s.calls == 2 and s.sleeps == [5.0]


def test_used_up_quota_403_waits_until_reset():
    reset = int(NOW) + 30
    s = Script(fake_response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}),
               fake_response(200))
    s.run()
    assert s.sleeps == [31.0]  # 30s until reset + 1s margin


def test_quota_reset_too_far_away_fails_instead_of_hanging():
    reset = int(NOW) + 3600
    s = Script(fake_response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}))
    with pytest.raises(GitHubFetchError, match="Try again later"):
        s.run()
    assert s.calls == 1 and s.sleeps == []


def test_plain_403_is_not_retried():
    s = Script(fake_response(403, {"x-ratelimit-remaining": "4999"}))  # permissions problem, not a limit
    with pytest.raises(GitHubFetchError, match="403"):
        s.run()
    assert s.calls == 1 and s.sleeps == []


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_other_4xx_fail_fast(status):
    s = Script(fake_response(status))
    with pytest.raises(GitHubFetchError, match=str(status)):
        s.run()
    assert s.calls == 1 and s.sleeps == []


def test_gives_up_after_max_attempts():
    s = Script(*[fake_response(503) for _ in range(fetch_prs.MAX_ATTEMPTS)])
    with pytest.raises(GitHubFetchError, match="still failing"):
        s.run()
    assert s.calls == fetch_prs.MAX_ATTEMPTS
    assert len(s.sleeps) == fetch_prs.MAX_ATTEMPTS - 1  # no pointless sleep after the last try


def test_timeout_and_network_errors_are_retried():
    s = Script(httpx.ReadTimeout("slow"), httpx.ConnectError("down"), fake_response(200))
    assert s.run().is_success
    assert s.calls == 3 and len(s.sleeps) == 2


def test_backoff_is_capped():
    s = Script(*[fake_response(500) for _ in range(fetch_prs.MAX_ATTEMPTS)])
    with pytest.raises(GitHubFetchError):
        s.run()
    assert max(s.sleeps) <= fetch_prs.MAX_BACKOFF_SECONDS + 1


# ---------- review pagination ----------

class FakeReviewsApi:
    """Serves `total` reviews in pages, like GitHub: honours per_page and page, records the calls."""

    def __init__(self, total):
        self.reviews = [{"id": i} for i in range(1, total + 1)]
        self.pages_requested = []

    def get(self, url, headers, params):
        page, per_page = params["page"], params["per_page"]
        self.pages_requested.append(page)
        start = (page - 1) * per_page
        return fake_response(200, json=self.reviews[start:start + per_page])


def fetch_all(total):
    api = FakeReviewsApi(total)
    got = fetch_prs.fetch_reviews_for_pr("encode", "httpx", 1, {}, get=api.get)
    return api, got


def test_250_reviews_are_all_fetched_across_three_pages():
    api, got = fetch_all(250)
    assert [r["id"] for r in got] == list(range(1, 251))  # none lost, none duplicated, in order
    assert api.pages_requested == [1, 2, 3]  # 100 + 100 + 50, stops at the short page


def test_exactly_100_reviews_needs_one_extra_empty_page():
    api, got = fetch_all(100)
    assert len(got) == 100
    assert api.pages_requested == [1, 2]  # a full page might not be the last, so ask once more


def test_few_reviews_use_a_single_call():
    api, got = fetch_all(3)
    assert len(got) == 3 and api.pages_requested == [1]


def test_no_reviews():
    api, got = fetch_all(0)
    assert got == [] and api.pages_requested == [1]


def test_runaway_pagination_stops_with_an_error():
    api = FakeReviewsApi((fetch_prs.MAX_REVIEW_PAGES + 1) * fetch_prs.REVIEWS_PER_PAGE)
    with pytest.raises(GitHubFetchError, match="more than"):
        fetch_prs.fetch_reviews_for_pr("encode", "httpx", 1, {}, get=api.get)
    assert len(api.pages_requested) == fetch_prs.MAX_REVIEW_PAGES


# ---------- _loaded_at ----------

def test_utc_now_naive_has_no_timezone_and_is_utc():
    import datetime
    stamp = fetch_prs.utc_now_naive()
    assert stamp.tzinfo is None
    real_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    assert abs((real_utc - stamp).total_seconds()) < 5  # UTC, whatever this machine's local zone is


def test_add_loaded_at_stamps_every_row_with_one_value_and_leaves_input_alone():
    import datetime
    import pandas as pd
    original = pd.DataFrame({"number": [1, 2, 3]})
    stamp = datetime.datetime(2026, 1, 2, 3, 4, 5)
    result = fetch_prs.add_loaded_at(original, stamp)
    assert set(result["_loaded_at"]) == {pd.Timestamp(stamp)}
    assert "_loaded_at" not in original.columns


def test_loaded_at_lands_in_duckdb_as_a_plain_timestamp():
    import datetime
    import duckdb
    import pandas as pd
    df = fetch_prs.add_loaded_at(pd.DataFrame({"number": [1]}), datetime.datetime(2026, 1, 2, 3, 4, 5))
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t AS SELECT * FROM df")
    assert con.execute("select typeof(_loaded_at), _loaded_at from t").fetchone() == (
        "TIMESTAMP", datetime.datetime(2026, 1, 2, 3, 4, 5))


# ---------- PR window (START_DATE) and de-duplication ----------

def pr(number, created):
    return {"number": number, "created_at": created + "T10:00:00Z"}


class FakePullsApi:
    """Serves a fixed list of PR pages (newest first) and records which pages were requested."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.requested = []

    def get(self, url, headers, params):
        assert params["sort"] == "created" and params["direction"] == "desc" and params["state"] == "all"
        self.requested.append(params["page"])
        index = params["page"] - 1
        return fake_response(200, json=self.pages[index] if index < len(self.pages) else [])


def numbers(prs):
    return [p["number"] for p in prs]


def test_stops_at_start_date_and_does_not_request_later_pages():
    api = FakePullsApi(
        [pr(10, "2024-05-01"), pr(9, "2023-06-01")],
        [pr(8, "2023-01-02"), pr(7, "2022-12-31"), pr(6, "2022-12-01")],  # 7 is the first too-old PR
        [pr(5, "2022-01-01")],  # must never be requested
    )
    got = fetch_prs.fetch_prs_since("encode", "httpx", "2023-01-01", {}, get=api.get)
    assert numbers(got) == [10, 9, 8]
    assert api.requested == [1, 2]


def test_pr_created_exactly_on_start_date_is_included():
    api = FakePullsApi([pr(2, "2023-01-01"), pr(1, "2022-12-31")])
    got = fetch_prs.fetch_prs_since("encode", "httpx", "2023-01-01", {}, get=api.get)
    assert numbers(got) == [2]


def test_duplicates_from_shifting_pages_are_removed():
    # A PR opened mid-run pushes #5 from the end of page 1 onto the top of page 2.
    api = FakePullsApi(
        [pr(7, "2024-03-01"), pr(6, "2024-02-01"), pr(5, "2024-01-01")],
        [pr(5, "2024-01-01"), pr(4, "2023-12-01"), pr(3, "2022-01-01")],
    )
    got = fetch_prs.fetch_prs_since("encode", "httpx", "2023-01-01", {}, get=api.get)
    assert numbers(got) == [7, 6, 5, 4]
    assert len(set(numbers(got))) == len(got)


def test_whole_repo_newer_than_start_date_ends_on_the_empty_page():
    api = FakePullsApi([pr(2, "2024-01-01"), pr(1, "2023-05-01")])
    got = fetch_prs.fetch_prs_since("encode", "httpx", "2023-01-01", {}, get=api.get)
    assert numbers(got) == [2, 1]
    assert api.requested == [1, 2]  # page 2 came back empty


def test_no_prs_at_all():
    api = FakePullsApi()
    assert fetch_prs.fetch_prs_since("encode", "httpx", "2023-01-01", {}, get=api.get) == []


def test_start_date_constant_is_2023():
    assert fetch_prs.START_DATE == "2023-01-01"
