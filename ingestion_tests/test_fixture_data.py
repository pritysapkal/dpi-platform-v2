"""The test fixture (fixtures/): its cases, its CSV files, its loader and its documentation.

Needs no network and no existing database; every test builds what it needs in a temporary folder.
"""

import datetime
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
sys.path.insert(0, str(FIXTURES))

import build_fixture as fx  # noqa: E402
import load_fixture  # noqa: E402

PULLED = fx.PULLED_AT
STATES = {"COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED", "PENDING"}


def when(text):
    return fx._t(text)


# ---------- the CSV files are exactly what the script writes ----------

def test_the_committed_csv_files_are_exactly_what_the_script_generates():
    def committed(path):  # Windows may have turned line endings into CRLF on checkout; the content is what matters
        return path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")

    assert committed(fx.PRS_CSV) == fx.render(fx.PR_COLUMNS, fx.pr_rows())
    assert committed(fx.REVIEWS_CSV) == fx.render(fx.REVIEW_COLUMNS, fx.review_rows())


def test_generating_twice_gives_identical_output():  # no randomness anywhere
    assert fx.render(fx.PR_COLUMNS, fx.pr_rows()) == fx.render(fx.PR_COLUMNS, fx.pr_rows())


def test_the_csv_columns_are_the_raw_table_columns_in_order():
    header = lambda path: path.read_text(encoding="utf-8").splitlines()[0].split(",")  # noqa: E731
    assert header(fx.PRS_CSV) == ["number", "title", "state", "merged", "author", "created_at", "updated_at", "merged_at", "closed_at", "_loaded_at"]
    assert header(fx.REVIEWS_CSV) == ["pr_number", "review_id", "reviewer_login", "review_state", "submitted_at", "_loaded_at"]


# ---------- the case table is internally consistent ----------

def test_numbers_and_review_ids_are_unique_and_every_review_belongs_to_a_pr():
    numbers = [p.number for p in fx.CASES]
    review_ids = [x[0] for p in fx.CASES for x in p.reviews]
    assert len(numbers) == len(set(numbers)) == 50
    assert len(review_ids) == len(set(review_ids))


@pytest.mark.parametrize("pr", fx.CASES, ids=lambda p: str(p.number))
def test_each_case_is_possible_and_tagged(pr):
    created = when(pr.created)
    assert pr.tags, "every case must say what it is for"
    assert created < PULLED
    assert not (pr.merged and pr.closed), "a PR is merged or closed without merging, never both"
    for end in (pr.merged, pr.closed):
        if end:
            assert created < when(end) < PULLED
    for review_id, reviewer, state, at in pr.reviews:
        assert state in STATES and reviewer
        assert created < when(at) < PULLED
        if pr.closed:
            assert when(at) < when(pr.closed), "no review on a PR after it was closed unmerged"
    assert pr.updated_at >= max([pr.created, pr.merged or pr.created, pr.closed or pr.created] + [x[3] for x in pr.reviews])


def test_the_pull_happens_after_everything():
    latest = max(max([p.created, p.merged or "", p.closed or ""] + [x[3] for x in p.reviews]) for p in fx.CASES)
    assert when(latest) < PULLED


# ---------- every situation we promised is really there ----------

def test_every_required_case_exists():
    present = {tag for p in fx.CASES for tag in p.tags}
    assert fx.REQUIRED_TAGS <= present, f"missing cases: {sorted(fx.REQUIRED_TAGS - present)}"
    assert set(fx.TAG_MEANING) == fx.REQUIRED_TAGS  # every required case is explained in the README, and vice versa


def test_the_tags_say_what_is_true():
    by_number = {p.number: p for p in fx.CASES}
    # a few tags are claims about the data, so check the claims rather than trusting the labels
    for p in fx.CASES:
        if "only_post_merge_review" in p.tags:
            assert min(when(x[3]) for x in p.reviews) > when(p.merged)
        if "review_at_merge_instant" in p.tags:
            assert min(x[3] for x in p.reviews) == p.merged
        if "bot_merged_no_review" in p.tags:
            assert fx.is_bot(p.author) and p.merged and not p.reviews
        if "human_merged_no_review" in p.tags:
            assert not fx.is_bot(p.author) and p.merged and not p.reviews
        if "lookalike_human_login" in p.tags:
            assert "bot" in p.author and not fx.is_bot(p.author)
        if "self_comment" in p.tags:
            assert any(x[1] == p.author for x in p.reviews)
        if "out_of_order_review_ids" in p.tags:
            ids_by_time = [x[0] for x in sorted(p.reviews, key=lambda x: x[3])]
            assert ids_by_time != sorted(ids_by_time)
        if "year_boundary" in p.tags:
            assert p.created[:4] != p.merged[:4]
        if "leap_day" in p.tags:
            assert p.created.startswith("2024-02-29") and p.merged.startswith("2024-02-29")
        if p.state == "open" and "open_ancient_never_reviewed" in p.tags:
            assert (PULLED - when(p.created)).days > 2 * 365 and not p.reviews
        if "many_reviews_one_reviewer" in p.tags:
            counts = {who: sum(1 for x in p.reviews if x[1] == who) for who in {x[1] for x in p.reviews}}
            assert max(counts.values()) == 5  # five reviews by one person on this one PR
        if "open_bot_pr" in p.tags:
            assert fx.is_bot(p.author) and p.state == "open" and not p.reviews
        if "bot_reviewer" in p.tags:
            assert any(fx.is_bot(x[1]) for x in p.reviews)
    assert by_number[1009].reviews[0][3] == by_number[1009].merged


def activity_days():
    """Every day on which anything happened: a PR was opened, merged or closed, or a review was submitted."""
    days = set()
    for p in fx.CASES:
        for stamp in [p.created, p.merged, p.closed] + [x[3] for x in p.reviews]:
            if stamp:
                days.add(when(stamp).date())
    return days


def test_the_calendar_has_the_gaps_the_readme_describes():
    days = activity_days()
    merges = lambda y, m: [p for p in fx.CASES if p.merged and p.merged.startswith(f"{y}-{m:02d}")]  # noqa: E731
    assert not [d for d in days if (d.year, d.month) in {(2023, 11), (2025, 12)}]  # nothing at all
    assert merges(2024, 9) == [] and merges(2024, 12) == []  # no merges, though there is activity
    assert [d for d in days if (d.year, d.month) == (2024, 9)] and [d for d in days if (d.year, d.month) == (2024, 12)]
    assert merges(2024, 8) and all(fx.is_bot(p.author) for p in merges(2024, 8))  # only bots merge in August 2024
    same_day = [p.merged[:10] for p in fx.CASES if p.merged]
    assert same_day.count("2024-11-18") == 2


def test_people_and_roles():
    authors = {p.author for p in fx.CASES if not fx.is_bot(p.author)}
    reviewers = {x[1] for p in fx.CASES for x in p.reviews if not fx.is_bot(x[1])}
    assert len(authors) == 11 and "grace" in reviewers and "grace" not in authors  # one reviewer who never authors
    assert len(authors) > 10  # so a "top 10" list has something to cut off


# ---------- the expected answers are self-consistent ----------

def test_facts_add_up():
    f = fx.facts()
    assert f["merged"] + f["closed_without_merge"] + f["open"] == f["prs"]
    assert f["bot_merged"] + f["human_merged"] == f["merged"]
    assert f["human_merged_reviewed_before_merge"] + f["human_merged_not_reviewed_before_merge"] == f["human_merged"]
    assert sum(f["merged_by_year"].values()) == f["merged"]
    assert f["p90_lead_time_hours"] >= f["median_lead_time_hours"] and f["mean_lead_time_hours"] > f["median_lead_time_hours"]


def test_the_readme_tables_are_exactly_the_generated_ones():
    text = fx.README.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert fx.block_text(text, "catalogue") == fx.catalogue_markdown()
    assert fx.block_text(text, "facts") == fx.facts_markdown()


# ---------- the loader ----------

EXPECTED_TYPES = {
    "github_pull_requests": [("number", "BIGINT"), ("title", "VARCHAR"), ("state", "VARCHAR"), ("merged", "BOOLEAN"), ("author", "VARCHAR"),
                             ("created_at", "VARCHAR"), ("updated_at", "VARCHAR"), ("merged_at", "VARCHAR"), ("closed_at", "VARCHAR"),
                             ("_loaded_at", "TIMESTAMP")],
    "github_pr_reviews": [("pr_number", "BIGINT"), ("review_id", "BIGINT"), ("reviewer_login", "VARCHAR"), ("review_state", "VARCHAR"),
                          ("submitted_at", "VARCHAR"), ("_loaded_at", "TIMESTAMP")],
}


def types_of(con, table):
    return con.execute("select column_name, data_type from information_schema.columns where table_schema='raw' and table_name=? order by ordinal_position", [table]).fetchall()


def test_the_loader_creates_the_raw_tables_with_the_production_types(tmp_path):
    db = tmp_path / "dev.duckdb"
    assert load_fixture.load(db) == {"pull_requests": 50, "reviews": 40}
    con = duckdb.connect(str(db), read_only=True)
    for table, expected in EXPECTED_TYPES.items():
        assert [tuple(r) for r in types_of(con, table)] == expected  # dates stay text, as fetch_prs.py leaves them
    assert con.execute("select count(*) from raw.github_pull_requests where merged_at = '' or closed_at = ''").fetchone()[0] == 0  # blanks became NULL
    assert con.execute("select count(*) from raw.github_pull_requests where state = 'open' and closed_at is null").fetchone()[0] == 6
    assert con.execute("select max(_loaded_at) from raw.github_pull_requests").fetchone()[0] == datetime.datetime(2026, 1, 15, 12, 0, 0)
    assert con.execute("select count(*) from raw.github_pull_requests where title like '%\"unclosed\" client, again'").fetchone()[0] == 1  # quotes and commas survive


def test_the_loaded_rows_match_the_case_table(tmp_path):
    db = tmp_path / "dev.duckdb"
    load_fixture.load(db)
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("select count(*) filter (where merged), count(*) filter (where author like '%[bot]') from raw.github_pull_requests").fetchone() == (39, 14)
    assert con.execute("select count(distinct review_id) from raw.github_pr_reviews").fetchone()[0] == 40


def test_loading_twice_is_fine(tmp_path):
    db = tmp_path / "dev.duckdb"
    load_fixture.load(db)
    load_fixture.load(db)
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("select count(*) from raw.github_pull_requests").fetchone()[0] == 50  # replaced, not doubled


def test_the_loader_refuses_a_database_with_real_data_and_leaves_it_alone(tmp_path):
    db = tmp_path / "real.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute("create table raw.github_pull_requests as select 1 as number, 'real' as title, timestamp '2026-11-01 18:34:18' as _loaded_at")
    con.close()
    with pytest.raises(load_fixture.RefusingToOverwrite, match="not this dataset"):
        load_fixture.load(db)
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("select title from raw.github_pull_requests").fetchall() == [("real",)]  # untouched


def test_the_loader_refuses_rows_with_no_load_time_too(tmp_path):
    db = tmp_path / "odd.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute("create table raw.github_pr_reviews as select 1 as pr_number, cast(null as timestamp) as _loaded_at")
    con.close()
    with pytest.raises(load_fixture.RefusingToOverwrite):
        load_fixture.load(db)


def test_the_loader_command_line_needs_an_explicit_path():
    result = subprocess.run([sys.executable, "-B", str(FIXTURES / "load_fixture.py")], capture_output=True, text=True)
    assert result.returncode != 0 and "no default, on purpose" in (result.stdout + result.stderr)


def test_the_command_line_loads_into_a_new_file(tmp_path):
    db = tmp_path / "cli.duckdb"
    result = subprocess.run([sys.executable, "-B", str(FIXTURES / "load_fixture.py"), str(db)], capture_output=True, text=True)
    assert result.returncode == 0 and "loaded 50 PRs and 40 reviews" in result.stdout


# ---------- period_facts(): the answer key for date windows ----------

def test_period_facts_over_everything_equals_facts():
    f, p = fx.facts(), fx.period_facts("2000-01-01", "2100-01-01")
    assert (p["merged"], p["median"], p["p90"], p["mean"]) == (f["merged"], f["median_lead_time_hours"], f["p90_lead_time_hours"], f["mean_lead_time_hours"])
    h = fx.period_facts("2000-01-01", "2100-01-01", humans_only=True)
    assert (h["merged"], h["median"], h["p90"]) == (f["human_merged"], f["human_median_lead_time_hours"], f["human_p90_lead_time_hours"])
    assert p["bot_merged"] == h["bot_merged"] == f["bot_merged"]


def test_period_facts_years_add_up_and_edges_are_inclusive():
    f = fx.facts()
    assert [fx.period_facts(f"{y}-01-01", f"{y}-12-31")["merged"] for y in (2023, 2024, 2025)] == [f["merged_by_year"][y] for y in (2023, 2024, 2025)]
    assert fx.period_facts("2024-11-18", "2024-11-18")["merged"] == 2  # two merges on that one day: both ends of a range count
    assert fx.period_facts("2024-09-01", "2024-09-30")["merged"] == 0 and fx.period_facts("2024-09-01", "2024-09-30")["median"] is None
    assert fx.period_facts("2024-08-01", "2024-08-31", humans_only=True)["merged"] == 0 < fx.period_facts("2024-08-01", "2024-08-31")["merged"]  # bot-only month
