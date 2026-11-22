"""Pull every PR of encode/httpx (created since START_DATE) and its reviews into raw.* of a DuckDB file.

Incremental by default: when the database already holds a previous fetch, only PRs updated since that fetch (minus a
three-day lookback, for clock skew and PRs updated while the last run was paging) are requested, newest update first, and
paging stops at the first PR older than that watermark. Fetched PRs are upserted (replaced by number) and their reviews
are replaced as a whole, so an edited, dismissed or late review is picked up. Nothing is ever dropped with CREATE OR REPLACE.
Every run, even one that finds nothing new, is recorded in raw.github_fetch_runs: that is "when the data was last pulled".

    python fetch_prs.py            # incremental (a full fetch when the database has no previous fetch)
    python fetch_prs.py --full     # every PR again
"""

import datetime
import os
import random
import sys
import time
import httpx
import duckdb
import pandas as pd

# ---------- settings ----------
OWNER = "encode"
REPO = "httpx"
# Fetch every PR created on or after this date, so every year in the data is complete.
START_DATE = "2023-01-01"
DB_PATH = os.environ.get("DPIP_DB_PATH") or "dev.duckdb"
# An incremental fetch re-requests PRs updated up to this long before the previous fetch: GitHub's clock and ours may
# differ, and a PR updated while the previous run was paging can have been missed by it.
FETCH_LOOKBACK = datetime.timedelta(days=3)

# The raw tables, with fixed column types (dates stay text, as GitHub sends them; _loaded_at is a real UTC timestamp).
# fixtures/load_fixture.py creates exactly these.
RAW_DDL = {
    "github_pull_requests": """
        create table if not exists raw.github_pull_requests (
            number bigint, title varchar, state varchar, merged boolean, author varchar,
            created_at varchar, updated_at varchar, merged_at varchar, closed_at varchar, _loaded_at timestamp
        )""",
    "github_pr_reviews": """
        create table if not exists raw.github_pr_reviews (
            pr_number bigint, review_id bigint, reviewer_login varchar, review_state varchar,
            submitted_at varchar, _loaded_at timestamp
        )""",
    # one row per fetch run, including runs that found nothing new
    "github_fetch_runs": """
        create table if not exists raw.github_fetch_runs (
            loaded_at timestamp, mode varchar, prs_written bigint, reviews_written bigint
        )""",
}
PR_COLUMNS = ["number", "title", "state", "merged", "author", "created_at", "updated_at", "merged_at", "closed_at"]
REVIEW_COLUMNS = ["pr_number", "review_id", "reviewer_login", "review_state", "submitted_at"]

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


def fetch_prs_since(owner, repo, start_date, headers, get=get_with_retry):
    """Every PR created on or after `start_date` (YYYY-MM-DD), newest first.

    The list is sorted by creation date, newest first, so we page until we reach a PR
    older than start_date and stop there: everything after it is older still.

    Pages can overlap: if a PR is opened while we are paging, every older PR shifts
    down one slot and the last PR of page N reappears at the top of page N+1. So PRs
    are de-duplicated by number (first copy wins).
    """
    prs = {}
    page = 1
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"

    while True:
        params = {
            "state": "all",
            "sort": "created",
            "direction": "desc",
            "per_page": 100,
            "page": page,
        }
        batch = get(url, headers, params).json()
        if not batch:
            break  # no more PRs at all
        reached_older_prs = False
        for pr in batch:
            # created_at looks like "2023-01-05T10:00:00Z"; ISO dates compare correctly as text.
            if pr["created_at"][:10] < start_date:
                reached_older_prs = True
            else:
                prs.setdefault(pr["number"], pr)
        if reached_older_prs:
            break
        page += 1

    return list(prs.values())


def fetch_prs_updated_since(owner, repo, since, start_date, headers, get=get_with_retry):
    """PRs updated at or after `since` (a naive UTC datetime), most recently updated first, created on or after start_date.

    The list is sorted by last update, newest first, so paging stops at the first PR updated before `since`. A PR
    created before start_date can be updated today; it is skipped (it is outside the window the full fetch covers)
    without stopping. As in fetch_prs_since, pages can overlap when a PR is updated mid-run, so PRs are de-duplicated
    by number (the first, most recent copy wins). A PR updated mid-run can also jump ahead of the page being read and
    be missed: the next run's lookback catches it.
    """
    since_text = since.strftime("%Y-%m-%dT%H:%M:%SZ")  # the format GitHub uses; ISO timestamps compare correctly as text
    prs = {}
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    page = 1
    while True:
        params = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 100, "page": page}
        batch = get(url, headers, params).json()
        if not batch:
            break
        reached_older_updates = False
        for pr in batch:
            if pr["updated_at"] < since_text:
                reached_older_updates = True
                break
            if pr["created_at"][:10] >= start_date:
                prs.setdefault(pr["number"], pr)
        if reached_older_updates:
            break
        page += 1
    return list(prs.values())


def ensure_raw_tables(con):
    con.execute("create schema if not exists raw")
    for ddl in RAW_DDL.values():
        con.execute(ddl)


def watermark(con, lookback=FETCH_LOOKBACK):
    """Where an incremental fetch starts: the previous fetch's time minus the lookback, or None for a first (full) fetch.

    The previous fetch's time is the last row of raw.github_fetch_runs, or, for a database written before that table
    existed, the newest _loaded_at of the PRs.
    """
    (last_run,) = con.execute("select max(loaded_at) from raw.github_fetch_runs").fetchone()
    if last_run is None:
        (last_run,) = con.execute("select max(_loaded_at) from raw.github_pull_requests").fetchone()
    return None if last_run is None else last_run - lookback


def pr_rows(raw_prs):
    return pd.DataFrame([{
        "number": pr["number"],
        "title": pr["title"],
        "state": pr["state"],
        "merged": pr.get("merged_at") is not None,
        "author": pr["user"]["login"] if pr.get("user") else None,
        "created_at": pr["created_at"],
        "updated_at": pr["updated_at"],
        "merged_at": pr.get("merged_at"),
        "closed_at": pr.get("closed_at"),
    } for pr in raw_prs], columns=PR_COLUMNS)


def review_rows(pr_number, reviews):
    return [{
        "pr_number": pr_number,
        "review_id": review["id"],
        "reviewer_login": review["user"]["login"] if review.get("user") else None,
        "review_state": review["state"],
        "submitted_at": review.get("submitted_at"),
    } for review in reviews]


def upsert(con, pr_df, reviews_df, loaded_at, mode):
    """Write one fetch in a single transaction: replace these PRs by number, replace ALL reviews of these PRs (so a
    review removed on GitHub disappears too), stamp the written rows with loaded_at, and record the run."""
    pr_df = add_loaded_at(pr_df.reindex(columns=PR_COLUMNS), loaded_at)
    reviews_df = add_loaded_at(reviews_df.reindex(columns=REVIEW_COLUMNS), loaded_at)
    ensure_raw_tables(con)
    con.register("incoming_prs", pr_df)
    con.register("incoming_reviews", reviews_df)
    con.execute("begin transaction")
    try:
        con.execute("delete from raw.github_pull_requests where number in (select number from incoming_prs)")
        con.execute("insert into raw.github_pull_requests by name select * from incoming_prs")
        con.execute("delete from raw.github_pr_reviews where pr_number in (select number from incoming_prs)")
        con.execute("insert into raw.github_pr_reviews by name select * from incoming_reviews")
        con.execute("insert into raw.github_fetch_runs values (?, ?, ?, ?)", [loaded_at, mode, len(pr_df), len(reviews_df)])
        con.execute("commit")
    except BaseException:
        con.execute("rollback")
        raise
    finally:
        con.unregister("incoming_prs")
        con.unregister("incoming_reviews")


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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("GH_TOKEN not set. Run: export GH_TOKEN='your_token'")
    headers = build_headers(token)

    con = duckdb.connect(DB_PATH)
    try:
        ensure_raw_tables(con)
        since = None if "--full" in argv else watermark(con)
        if since is None:
            mode = "full"
            print(f"Full fetch: pull requests for {OWNER}/{REPO} created on or after {START_DATE}...")
            raw_prs = fetch_prs_since(OWNER, REPO, START_DATE, headers)
        else:
            mode = "incremental"
            print(f"Incremental fetch: pull requests updated since {since:%Y-%m-%d %H:%M} UTC "
                  f"(the previous fetch minus {FETCH_LOOKBACK.days} days)...")
            raw_prs = fetch_prs_updated_since(OWNER, REPO, since, START_DATE, headers)
        print(f"Found {len(raw_prs)} pull requests to write.")

        print("Fetching reviews for each of them (one API call per PR)...")
        reviews = []
        for i, pr in enumerate(raw_prs):
            reviews += review_rows(pr["number"], fetch_reviews_for_pr(OWNER, REPO, pr["number"], headers))
            if (i + 1) % 50 == 0:
                print(f"  ...reviews fetched for {i + 1}/{len(raw_prs)} PRs")
            time.sleep(REQUEST_PAUSE_SECONDS)

        # One timestamp for the whole run, so the rows it wrote and the run record all say they were loaded together.
        loaded_at = utc_now_naive()
        upsert(con, pr_rows(raw_prs), pd.DataFrame(reviews, columns=REVIEW_COLUMNS), loaded_at, mode)
        total_prs = con.execute("select count(*) from raw.github_pull_requests").fetchone()[0]
        total_reviews = con.execute("select count(*) from raw.github_pr_reviews").fetchone()[0]
        print(f"{mode} fetch written to {DB_PATH}: {len(raw_prs)} PRs and {len(reviews)} reviews upserted; "
              f"raw now holds {total_prs} PRs and {total_reviews} reviews.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
