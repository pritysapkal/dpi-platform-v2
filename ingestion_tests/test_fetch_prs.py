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
