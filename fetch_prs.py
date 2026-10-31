import os
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


def build_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


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
        response = httpx.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break  # ran out of PRs before hitting the limit
        prs.extend(batch)
        page += 1

    return prs[:limit]


def fetch_reviews_for_pr(owner, repo, pr_number, headers):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    response = httpx.get(url, headers=headers, params={"per_page": 100}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


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
