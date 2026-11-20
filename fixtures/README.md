# The test fixture: 50 made-up pull requests that never change

The live GitHub data changes every day, so a test that says "there were 288 merged PRs" is only true on one particular
day. This folder holds a small dataset that is **always the same**, built so that every situation the pipeline has to
get right appears at least once. The tests run the real pipeline on it (`dbt build`, then the agent and the dashboard
code) and compare the answers with figures worked out independently in plain Python.

| File | What it is |
|---|---|
| `build_fixture.py` | The list of cases, written by hand, one comment per case. Writes the two CSV files. No randomness. |
| `raw_pull_requests.csv`, `raw_pr_reviews.csv` | The data. Their columns match the raw tables that `fetch_prs.py` writes, one for one. |
| `load_fixture.py` | Loads the CSVs into the `raw` schema of a DuckDB file, so `dbt build` can run on top. |

```
python fixtures/build_fixture.py            # rewrite the CSVs from the case table (a test checks they are identical)
python fixtures/load_fixture.py dev.duckdb  # load them; then run: dbt build
python fixtures/build_fixture.py --facts    # print the expected answers
```

The loader has no default path and **refuses to touch a database that holds real data**: it replaces the two raw tables,
so it only works on a new file or on a file that already contains just this fixture.

## Why CSV files written by a script, and not dbt seeds

dbt seeds load into the project's own schema and guess each column's type from the file, so the dates would become
timestamps. The real raw tables keep dates as text, and the staging models convert them, so a seed would stop testing that
conversion. Using seeds for the `raw` schema also needs a schema-naming override that could change production behaviour.
Writing the raw tables directly keeps the fixture exactly as awkward as the real data.

## The cases

Each case is a pull request on purpose. The tag is also in the case table, and a test fails if a tag below disappears.

<!-- catalogue:start -->
| Case (tag) | PRs | What it is for |
|---|---|---|
| `window_start_day` | 1001 | created on 2023-01-01, the first day of the data window |
| `normal_reviewed_merge` | 1001, 1005, 1015, 1018, 1021, 1031, 1036, 1040, 1044, 1047 | an ordinary PR: reviewed, approved, merged |
| `multi_review` | 1003, 1014, 1028, 1035 | several reviews from different people on one PR |
| `slow_merge_week` | 1007, 1025, 1028, 1042 | took a week to merge, so the lead-time tail is not empty |
| `bot_merged_no_review` | 1002, 1006, 1017, 1019, 1022, 1026, 1030, 1032, 1033, 1037, 1041, 1048 | a dependabot PR merged within hours, never reviewed |
| `bot_closed_unmerged` | 1013, 1046 | a bot PR closed without merging |
| `bot_reviewer` | 1035 | the first review comes from a bot (`copilot-pull-request-reviewer[bot]`) |
| `bot_only_month` | 1032, 1033 | a month (August 2024) in which only bots merge anything |
| `lookalike_human_login` | 1015 | `robot-dev` contains "bot" but is a person, so `is_bot` must be false |
| `human_merged_no_review` | 1008, 1029, 1045 | a person merged their own PR with no review at all |
| `only_post_merge_review` | 1010, 1011 | the only review arrived after the PR was already merged |
| `review_at_merge_instant` | 1009 | first review in the same second as the merge: counts as before the merge |
| `review_one_second_after_merge` | 1010 | first review one second after the merge: does not count as before |
| `changes_requested_then_approved` | 1014 | CHANGES_REQUESTED, then a comment, then APPROVED |
| `out_of_order_review_ids` | 1014 | review ids are not in time order, so the first review is not the lowest id |
| `self_comment` | 1016 | the author comments on their own PR (not a review of someone else's work) |
| `human_closed_unmerged_reviewed` | 1004, 1034, 1039 | a person's PR closed without merging, after a review |
| `human_closed_unmerged_unreviewed` | 1027 | a person's PR closed without merging, never reviewed |
| `open_ancient_never_reviewed` | 1012 | still open, created in 2023, never reviewed (over two years old when pulled) |
| `open_reviewed_not_approved` | 1024, 1043 | still open, has comments but no approval |
| `open_recent_approved` | 1049 | still open, created days before the pull, already approved |
| `open_recent_never_reviewed` | 1050 | still open, created days before the pull, no review yet |
| `year_boundary` | 1020 | opened in one year, merged in the next |
| `leap_day` | 1023 | opened and merged on 29 February 2024 |
| `two_merges_same_day` | 1037, 1038 | two PRs merged on 2024-11-18 (checks date ranges are inclusive) |
| `merge_free_month_with_activity` | 1039 | December 2024: a PR is opened and reviewed, but nothing is merged |
<!-- catalogue:end -->

Calendar shape, also deliberate: the data runs from 2023-01-01 to a pretend fetch on 2026-01-15 12:00 UTC; **November 2023
and December 2025 are completely empty** (a stretch with no activity at all); September and December 2024 have no merges;
there are 11 people who author PRs and one who only reviews (`grace`), so a "top 10" list has something to cut off.

## The expected answers

Worked out by `facts()` from the case table, in plain Python, without SQL or dbt. The tests check that the pipeline
reproduces every one of them, and that this table matches.

<!-- facts:start -->
| Fact | Value |
|---|---|
| PRs | 50 |
| reviews | 36 |
| merged | 39 |
| closed without merge | 6 |
| still open | 5 |
| bot-authored PRs | 14 |
| merged by bots | 12 |
| merged by people | 27 |
| people's merges reviewed before the merge | 22 |
| people's merges not reviewed before the merge | 5 |
| open PRs never reviewed | 2 |
| people who authored a PR | 11 |
| people who reviewed | 8 |
| distinct logins (people and bots) | 14 |
| merged in 2023 | 16 |
| merged in 2024 | 16 |
| merged in 2025 | 7 |
| median lead time, all merged (hours) | 12.00 |
| p90 lead time, all merged (hours) | 129.60 |
| mean lead time, all merged (hours) | 34.79 |
| median lead time, people only (hours) | 24.00 |
| p90 lead time, people only (hours) | 168.00 |
<!-- facts:end -->

## Adding or changing a case

1. Edit the case table in `build_fixture.py` (give the new case a tag and, if it is a new kind of situation, add the tag to
   `TAG_MEANING` and `REQUIRED_TAGS`).
2. Run `python fixtures/build_fixture.py` and then `python fixtures/build_fixture.py --update-readme`.
3. Run the tests. Anything that depended on the old numbers will say so, which is the point.
