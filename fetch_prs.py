import datetime
import os
import random
import time
import httpx
import duckdb
import pandas as pd

# ---------- settings ----------
OWNER = "encode"
REPO = "httpx"
PR_LIMIT = 500
DB_PATH = "dev.duckdb"

# Every HTTP call gives up after these many seconds instead of hanging forever.
# connect is short (a dead network should fail fast); read is longer (GitHub can be slow).
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
REQUEST_PAUSE_SECONDS = 0.05  # stay polite to GitHub's secondary rate limits
REVIEWS_PER_PAGE = 100  # GitHub's maximum page size
MAX_REVIEW_PAGES = 50  # safety stop: 5,000 reviews on one PR means something is wrong

# Retry policy for get_with_retry().
MAX_ATTEMPTS = 5  # per request, including the first try
BASE_DELAY_SECONDS = 2.0  # backoff: 2s, 4s, 8s, 16s (+ up to 1s of random jitter)
MAX_BACKOFF_SECONDS = 60.0
# If GitHub tells us to wait longer than this (e.g. the hourly quota is used up),
# stop with a clear error instead of leaving a CI job hanging.
MAX_SERVER_WAIT_SECONDS = 900.0
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class GitHubFetchError(Exception):
    """A GitHub request failed for good (not retryable, or retries ran out)."""


def utc_now_naive():
    """Current time in UTC, stored WITHOUT a timezone marker.

    A timezone-aware value would be converted to this machine's local time when it
    goes into DuckDB, so a laptop and a CI runner would record different clock times.
    Plain UTC, always, keeps `_loaded_at` identical in meaning everywhere.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def add_loaded_at(df, loaded_at):
    """Stamp every row of a raw table with when this fetch ran (one value per run)."""
    df = df.copy()
    df["_loaded_at"] = loaded_at
    return df


def build_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


def _server_wait_seconds(response, now):
    """How long GitHub itself says to wait, or None if it gave no hint.

    - Retry-After: seconds to wait (sent with 429s and secondary rate limits).
    - X-RateLimit-Remaining: 0 plus X-RateLimit-Reset (a Unix timestamp): the
      hourly quota is used up, wait until it resets.
    """
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            return max(float(reset) - now() + 1, 0.0)
    return None


def get_with_retry(url, headers, params=None, http_get=httpx.get, sleep=time.sleep, now=time.time):
    """GET a URL and return the response, retrying temporary failures.

    Retried, with exponential backoff plus jitter (or GitHub's own wait hint):
      - 429 and 5xx (500/502/503/504)
      - 403 that carries a Retry-After or an exhausted X-RateLimit header, which is
        how GitHub signals a secondary rate limit / used-up quota
      - timeouts and network errors
    Not retried (fails at once, since waiting can't fix it): every other 4xx, including
    a plain 403 (bad permissions), 401 (bad token) and 404.

    http_get, sleep and now are parameters only so tests can pass fakes and run offline.
    """
    last_problem = "no attempt made"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait_hint = None
        try:
            response = http_get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_problem = f"{type(exc).__name__}: {exc}"
        else:
            if response.is_success:
                return response
            wait_hint = _server_wait_seconds(response, now)
            retryable = response.status_code in TRANSIENT_STATUS_CODES or (
                response.status_code == 403 and wait_hint is not None
            )
            if not retryable:
                raise GitHubFetchError(
                    f"GET {url} failed with HTTP {response.status_code} (not retryable): "
                    f"{response.text[:200]}"
                )
            last_problem = f"HTTP {response.status_code}"

        if attempt == MAX_ATTEMPTS:
            break

        if wait_hint is not None:
            if wait_hint > MAX_SERVER_WAIT_SECONDS:
                raise GitHubFetchError(
                    f"GET {url}: {last_problem}, and GitHub asks us to wait {wait_hint:.0f}s "
                    f"(more than the {MAX_SERVER_WAIT_SECONDS:.0f}s limit). Try again later."
                )
            delay = wait_hint
        else:
            delay = min(BASE_DELAY_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS) + random.uniform(0, 1)
        print(f"  [retry] {last_problem}; waiting {delay:.1f}s (attempt {attempt}/{MAX_ATTEMPTS})")
        sleep(delay)

    raise GitHubFetchError(f"GET {url} still failing after {MAX_ATTEMPTS} attempts ({last_problem}).")


def fetch_recent_prs(owner, repo, limit, headers):
    """Page through /pulls, newest first, until `limit` PRs are collected."""
    prs = []
    page = 1
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"

    while len(prs) < limit:
        params = {
            "state": "all",
            "sort": "created",
            "direction": "desc",
            "per_page": 100,
            "page": page,
        }
        response = get_with_retry(url, headers, params)
        batch = response.json()
        if not batch:
            break  # ran out of PRs before hitting the limit
        prs.extend(batch)
        page += 1

    return prs[:limit]


def fetch_reviews_for_pr(owner, repo, pr_number, headers, get=get_with_retry):
    """All reviews on one PR. GitHub returns at most 100 per page, so keep asking for
    the next page until one comes back with fewer than 100 (or empty)."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    reviews = []
    for page in range(1, MAX_REVIEW_PAGES + 1):
        batch = get(url, headers, {"per_page": REVIEWS_PER_PAGE, "page": page}).json()
        reviews.extend(batch)
        if len(batch) < REVIEWS_PER_PAGE:
            return reviews
    raise GitHubFetchError(
        f"PR #{pr_number} has more than {MAX_REVIEW_PAGES * REVIEWS_PER_PAGE} reviews; "
        "stopping rather than looping forever."
    )


def main():
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("GH_TOKEN not set. Run: export GH_TOKEN='your_token'")
    headers = build_headers(token)

    print(f"Fetching pull requests for {OWNER}/{REPO}...")
    raw_prs = fetch_recent_prs(OWNER, REPO, PR_LIMIT, headers)
    print(f"Found {len(raw_prs)} pull requests.")

    pr_rows = []
    for pr in raw_prs:
        pr_rows.append({
            "number": pr["number"],
            "title": pr["title"],
            "state": pr["state"],
            "merged": pr.get("merged_at") is not None,
            "author": pr["user"]["login"] if pr.get("user") else None,
            "created_at": pr["created_at"],
            "updated_at": pr["updated_at"],
            "merged_at": pr.get("merged_at"),
            "closed_at": pr.get("closed_at"),
        })

    pr_df = pd.DataFrame(pr_rows)

    print("Fetching reviews for each pull request (one API call per PR)...")
    review_rows = []
    for i, pr in enumerate(raw_prs):
        pr_number = pr["number"]
        for review in fetch_reviews_for_pr(OWNER, REPO, pr_number, headers):
            review_rows.append({
                "pr_number": pr_number,
                "review_id": review["id"],
                "reviewer_login": review["user"]["login"] if review.get("user") else None,
                "review_state": review["state"],
                "submitted_at": review.get("submitted_at"),
            })

        if (i + 1) % 50 == 0:
            print(f"  ...reviews fetched for {i + 1}/{len(raw_prs)} PRs")

        time.sleep(REQUEST_PAUSE_SECONDS)

    reviews_df = pd.DataFrame(review_rows)
    print(f"Found {len(reviews_df)} reviews across {len(raw_prs)} pull requests.")

    # One timestamp for the whole run, so both raw tables say they were loaded together.
    loaded_at = utc_now_naive()
    pr_df = add_loaded_at(pr_df, loaded_at)
    reviews_df = add_loaded_at(reviews_df, loaded_at)

    con = duckdb.connect(DB_PATH)
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("CREATE OR REPLACE TABLE raw.github_pull_requests AS SELECT * FROM pr_df")
    con.execute("CREATE OR REPLACE TABLE raw.github_pr_reviews AS SELECT * FROM reviews_df")

    print(f"Data written to raw.github_pull_requests and raw.github_pr_reviews in {DB_PATH}")
    print(pr_df.head())
    print(reviews_df.head())

    con.close()


if __name__ == "__main__":
    main()
