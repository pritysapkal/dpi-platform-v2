# PR Lifecycle & DORA Metrics — encode/httpx

An analytics engineering project that takes real GitHub pull request data from
[encode/httpx](https://github.com/encode/httpx) — a live, active open-source
project — through a full dbt pipeline to compute two [DORA](https://dora.dev/)
metrics: **Deployment Frequency** and **Lead Time for Changes**.

The point of this project isn't the metrics themselves — it's doing one data
entity properly end to end: layered modeling, tests that catch real bugs, a
semantic layer instead of ad-hoc SQL, and CI that actually runs. See
[PROGRESS.md](./PROGRESS.md) for the full build log and locked-in scope.

On top of the pipeline sits **[DPIP Agent](#dpip-agent)**: a tool-calling LLM
agent (Google Gemini) that answers plain-English questions about these metrics
through a small set of read-only tools, with guardrails enforced in code and an
eval set scored against SQL ground truth.

## Architecture

```mermaid
flowchart LR
    S["Source<br/>GitHub REST API<br/>(encode/httpx)"] --> I["Ingestion<br/>fetch_prs.py"]
    I --> W[("Warehouse<br/>DuckDB — raw schema<br/>github_pull_requests, github_pr_reviews")]
    W --> ST["Staging<br/>stg_github__pull_requests<br/>stg_github__pr_reviews"]
    ST --> INT["Intermediate<br/>int_pr_lifecycle"]
    INT --> M["Marts<br/>mart_dora_metrics_daily"]
    M --> SEM["Semantic Layer<br/>MetricFlow<br/>deployment_frequency, lead_time_for_changes"]
    SEM --> DASH["Dashboard<br/>Streamlit — dashboard/app.py"]
```

- **Ingestion** (`fetch_prs.py`) — pulls the 500 most recent PRs on `encode/httpx`
  via the GitHub REST API, plus every review on each one (one API call per PR,
  since GitHub doesn't expose reviews in bulk), into `raw.github_pull_requests`
  and `raw.github_pr_reviews` in a local DuckDB file.
- **Staging** — casts types and renames columns to consistent conventions.
  `stg_github__pr_reviews` stays one row per *review* (a PR can have zero, one,
  or many); nothing else touches its grain.
- **Intermediate** (`int_pr_lifecycle`) — the important join. Reviews are rolled
  up to one row per PR *first* (`min`/`max`/`count` over `submitted_at`), then
  left-joined onto the PR base, so the grain never fans out. Computes
  `review_wait_hours`, `time_in_review_hours`, and `lead_time_hours`.
- **Marts** (`mart_dora_metrics_daily`) — aggregates merged PRs to one row per
  calendar day, on a full date spine so zero-merge days are `0`, not a missing
  row.
- **Semantic layer** — `deployment_frequency` and `lead_time_for_changes`
  defined as MetricFlow metrics on top of the mart, queryable via `mf query`
  instead of being hardcoded SQL.
- **Dashboard** (`dashboard/app.py`) — a single-page Streamlit app that reads
  `mart_dora_metrics_daily` directly out of `dev.duckdb`, read-only. No BI
  tool, no data copy — see [Dashboard](#dashboard) below for why.

## Data dictionary — `mart_dora_metrics_daily`

| Column | Type | Description |
|---|---|---|
| `metric_date` | date | One row per calendar day. Primary key — spans the full range from the first to the most recent merge, with no gaps. |
| `deployment_frequency` | integer | Count of PRs merged on this day. A PR merge is used as a proxy for a deployment — see [Known limitations](#known-limitations--what-id-do-at-scale). Never null; `0` on days with no merges. |
| `avg_lead_time_hours` | float | Average `lead_time_hours` (PR opened → merged) across PRs merged this day, rounded to 2 decimals. **Null**, not `0`, on zero-merge days — there's nothing to average. |

## dbt tests

Every model's primary key gets standard `unique`/`not_null` tests. On top of
that, `int_pr_lifecycle` has a custom `non_negative` generic test
([`macros/generic_tests.sql`](./macros/generic_tests.sql)) on two columns:

| Test | What it actually catches |
|---|---|
| `non_negative` on `review_wait_hours` | Both durations are `date_diff(...)` between two timestamps pulled from the GitHub API. A negative value can only mean a timestamp parsing bug or a genuine source-data anomaly (e.g. clock skew). This exact class of bug showed up during development — a review submitted 56 days *after* its PR had already merged initially produced a negative `time_in_review_hours` before the model was fixed to guard it. |
| `non_negative` on `lead_time_hours` | Same failure mode, but on the column that feeds the Lead Time for Changes DORA metric directly — if this ever went negative, it would silently corrupt the mart and the semantic layer metric built on top of it. |

Run `dbt build` to run every model and every test in dependency order.

## Semantic layer

```
mf list metrics
mf query --metrics deployment_frequency,lead_time_for_changes --group-by metric_time__month
```

**Known caveat, by design:** `lead_time_for_changes` is defined as an
`average` measure over `mart_dora_metrics_daily.avg_lead_time_hours`, which is
*already* a daily average. That makes the metric an average of daily averages
(each day weighted equally), not a true PR-weighted average across all merges.
Building the semantic model on the pre-aggregated mart (rather than on
`int_pr_lifecycle` at PR grain) was a deliberate scope tradeoff — see below.

## Dashboard

**Why a custom Streamlit dashboard instead of a BI tool:** the original plan
was [Evidence.dev](https://evidence.dev). It turns out "Evidence.dev" now
means two different products. The one its own scaffolding tool installs by
default — Evidence Studio — is a hosted commercial product whose only
login-free, no-data-copy connectors are Snowflake, Fabric, ClickHouse, and
BigQuery; a local DuckDB file isn't a supported data source at all. Getting
data in any other way means creating an account and uploading it into
Evidence's cloud-managed warehouse — the opposite of "read the existing
warehouse, don't duplicate it." The classic open-source, DuckDB-native
version of Evidence still exists on npm, but its docs have been fully
replaced by the new product's, so standing it up would mean reverse-engineering
undocumented config from source rather than following a supported path. Given
that, a ~70-line Streamlit script reading DuckDB directly was the more
reliable choice for something this small.

**Running it:**

```
cd dashboard
pip install -r requirements.txt
streamlit run app.py
```

Opens at http://localhost:8501. Connects read-only, so it's safe to leave
running while `dbt build` runs elsewhere (results refresh within 60 seconds).

## Known limitations / what I'd do at scale

- **Deployment Frequency is a proxy.** There's no real deployment/release event
  data available from the GitHub PR API — only merges. At scale, I'd ingest
  actual deployment events (GitHub's Deployments API, or CI/CD pipeline logs)
  for a metric that reflects real production releases, not merges.
- **Lead Time for Changes measures PR-open-to-merge, not commit-to-production.**
  The full DORA definition is commit-to-production. At scale this would need
  commit timestamps and deploy timestamps, not just PR timestamps.
- **The semantic layer sits on a pre-aggregated mart, not PR grain.** As noted
  above, this makes `lead_time_for_changes` an average of daily averages. At
  scale, I'd build the semantic model on `int_pr_lifecycle` directly so
  MetricFlow can compute a true PR-weighted average and support slicing by
  author/reviewer/etc. that a daily mart can't offer.
- **Full refresh, not incremental.** `fetch_prs.py` re-pulls the most recent
  500 PRs every run, and every dbt model fully rebuilds. Fine at this scale
  (500 rows, sub-second rebuilds); wouldn't scale to a repo with 50,000+ PRs.
  At scale: incremental ingestion (GitHub's `since` param) and incremental dbt
  materializations keyed on `updated_at`.
- **One data source, on purpose.** This project deliberately covers PRs and
  reviews only, not commits, issues, or CI/CD workflow data — one entity done
  deeply rather than several done shallowly. A production DORA implementation
  would also need CI/CD data for the other two DORA metrics (Change Failure
  Rate, Time to Restore Service).
- **CI queries live data, so results aren't deterministic run-to-run.** CI
  re-fetches real data from `encode/httpx` on every push, which is realistic
  but means a green run today doesn't guarantee identical numbers tomorrow —
  the underlying repo keeps changing. At scale, I'd separate "does the
  pipeline logic work" (tested against a fixed fixture dataset in CI) from "is
  production data healthy" (monitored separately against live data).
- **No orchestration.** The pipeline only runs on `git push` via CI, not on a
  recurring schedule. At scale it would run on a schedule (Airflow, Dagster,
  or even a cron-triggered GitHub Actions workflow) so the marts and metrics
  stay fresh independent of code changes.
- **The dashboard is a script, not a BI tool.** No auth, no sharing link, no
  multi-user access — you run it locally and it's just for you. At scale I'd
  either revisit a real BI/semantic-layer-aware tool (once one exists with a
  sane local/self-hosted DuckDB story) or put this behind a small deployed
  service if others need to view it.

## Running it locally

```
pip install -r requirements.txt
export GH_TOKEN=your_github_pat        # any public-repo-read token works
python fetch_prs.py                    # pulls fresh PR + review data
dbt build                              # runs every model + test
mf list metrics                        # semantic layer, once dbt-metricflow is installed
cd dashboard && pip install -r requirements.txt && streamlit run app.py   # dashboard, see below
```

## DPIP Agent

`agent/` adds a question-answering agent on top of the pipeline:

```
python -m agent.cli "What was the median lead time in 2024 and how does it compare to 2023?"
```

It answers only from tool results, quotes the actual numbers and the date range
used, and says "I don't have data for that" instead of guessing. Nothing in the
dbt project changed; the agent only reads `dev.duckdb`.

### Architecture

```mermaid
flowchart LR
    U["User<br/>agent/cli.py"] --> L["Agent loop<br/>agent/agent.py<br/>max 6 tool calls"]
    L <-->|"question + tool declarations /<br/>function_call or final text"| G["Gemini<br/>agent/llm.py<br/>retries, timeout, fallback"]
    L -->|"function_call(name, args)"| GR["Argument checks +<br/>guardrails<br/>agent/guardrails.py"]
    GR --> T["Tools<br/>agent/tools.py"]
    T --> D[("dev.duckdb<br/>read-only,<br/>no external access")]
    T -->|"result wrapped as<br/>untrusted_tool_data, size-capped"| L
    L --> TR["JSONL trace<br/>agent/traces/"]
```

The tool loop is written by hand, with the SDK's automatic function calling
disabled, so every step is visible and testable:

1. Send the question plus the tool declarations (generated from the tool
   functions' type hints and docstrings) to Gemini.
2. If the reply contains `function_call`s, check the arguments, run the matching
   Python tool, wrap the result as untrusted data, and send it back as a
   `function_response`.
3. Repeat until the model replies with text. If answering would take more than
   6 tool calls, stop and say so instead of returning a partial guess.

The Gemini call sits behind a small `ModelClient` interface, so the whole loop
is unit-tested offline with a scripted fake model.

### Tools

| Tool | What it does |
|---|---|
| `list_tables()` | The two tables the agent may query, with descriptions. |
| `get_schema(table_name)` | Columns and types of one allowed table. |
| `get_data_coverage()` | Repo, first and last date with data, PR counts, which metrics exist. |
| `resolve_period(phrase)` | Turns "last quarter", "this year", "last 30 days", "Q1 2025", "October 2024" and similar into exact dates **in Python** ([`agent/periods.py`](./agent/periods.py)), and says whether DPIP has data for them. Unrecognised phrases return an error, never a guess. |
| `get_dora_metrics(start_date, end_date, repo=None, period=None)` | Deployment frequency (merged PRs, total and per week) and lead time: **median** as the headline (PR lead times are heavily skewed), PR-weighted mean, and the dashboard's day-weighted average, labelled as such. Takes either explicit dates or a `period` phrase resolved in code (not both). Change failure rate is returned as "not available". |
| `compare_periods(a_start, a_end, b_start, b_end, repo=None, period_a=None, period_b=None)` | Both periods side by side, with % change. Each side takes dates or a phrase. |
| `run_readonly_sql(query)` | Free-form SELECT for anything the other tools can't answer (per-author counts, review stats, monthly breakdowns). |

Every tool returns a dict and never raises: problems come back as
`{"ok": false, "error": "..."}` so the model can read the error and recover.

### Guardrails, and why each one exists

The rule is that safety never depends on the model obeying its prompt. Every
tool call goes through these checks, however the model decided to make it.

| Guardrail | Why |
|---|---|
| **Database opened `read_only=True` with `enable_external_access=False`** | The last line of defence. Even a query that got past every check below can't write, attach another database, read files (`read_csv`, `glob`) or install extensions. Tests run these statements straight against the connection to prove it. |
| **DuckDB's own parser as the main SQL check** (`json_serialize_sql`) | A real parser instead of guessing from text. It only serializes SELECTs, so any write, `PRAGMA` or second statement fails. The whole parse tree is walked, so tables hidden in subqueries, JOINs, UNIONs and nested CTEs are all found. |
| **Table whitelist**: only `mart_dora_metrics_daily` and `int_pr_lifecycle` | The agent sees the curated outputs of the dbt project, not raw or staging data. |
| **CTE names may not match a real table** | A bypass found while building this: in DuckDB, `WITH stg_x AS (SELECT * FROM stg_x)` reads the *real* `stg_x` inside the CTE body. A whitelist that simply skipped CTE names would have let it through. |
| **No table functions** | `read_csv`, `glob`, `query()` and `duckdb_tables()` can read files or metadata. Two analytics tables don't need any of them. |
| **Regex check as a second layer** | Must start with SELECT/WITH, only one statement, no write or admin keywords (after removing comments and strings). It catches `SHOW`, `DESCRIBE` and `SUMMARIZE`, which DuckDB parses as SELECTs. |
| **200-row cap and a 10 s query timeout** | Keeps results small enough for the model and stops runaway queries. DuckDB has no statement timeout, so a timer calls `con.interrupt()`. |
| **Strict `YYYY-MM-DD` dates with start ≤ end, and repo must be `encode/httpx`** | Invalid input comes back as an error the model can fix, instead of a silently wrong query. |
| **Relative dates are computed in code, and the loop checks them** | LLMs are unreliable at date arithmetic (eval t03 below). The model passes the user's phrase (`period="last quarter"`) and `periods.py` computes the dates. Before any tool runs, the loop also finds time phrases in the question, resolves them in code, and **rejects explicit dates that don't match**, so hand-computed dates are blocked even if the model ignores its instructions. |
| **Tool argument checks in the loop** | An unknown tool name, missing or extra arguments, or wrong types become error dicts instead of crashes. |
| **Tool results wrapped as `untrusted_tool_data` and size-capped** | PR titles and logins are written by GitHub users, so a title can carry a prompt injection. Results are labelled as data in both the payload and the system instruction. Text is cut to 300 characters and whole results to 8,000. |
| **Retries only on 429/500/503 and timeouts** (5 attempts, exponential backoff + jitter, server `Retry-After`/`retryDelay` respected); **fail fast on 400/401/403/404** | Retrying a bad key or a wrong model name can't succeed. Temporary overload usually clears. |
| **Fallback model is opt-in and always visible** (`GEMINI_FALLBACK_MODEL`) | It is used only after the main model's retries run out. The model that actually answered is recorded in every trace line and in the eval report. |
| **Traces never contain the API key or headers** | Traces are written from chosen fields only, and the key is blanked out if it ever shows up anyway. |

### Running it

```
pip install -r agent/requirements.txt
cp agent/.env.example agent/.env          # then paste a key from https://aistudio.google.com/apikey
python -m agent.cli "Which month had the most merged pull requests?"
python -m agent.cli --model gemini-3.5-flash-lite --sticky-fallback "..."
python -m pytest agent/tests -v           # 201 tests, no API key needed
python -m agent.evals.run_evals --model gemini-3.5-flash-lite
```

Run everything from the project root after `dbt build`. Settings live in
`agent/.env`:

- `GEMINI_MODEL`: defaults to `gemini-3.8-flash`.
- `GEMINI_FALLBACK_MODEL`: optional.

The guardrail and loop tests need neither a key nor `dev.duckdb`. The tool tests
skip themselves if the database hasn't been built.

### Evaluation

[`agent/evals/eval_set.json`](./agent/evals/eval_set.json) has 20 questions:

- **14 metric questions**: counts, medians, period comparisons, per-author and per-month breakdowns, review coverage, and relative or explicit periods ("last year", "so far this year", "Q4 2024 vs Q4 2023"). Relative-period ground truth uses `current_date` in SQL, so it stays correct whatever day the eval runs.
- **6 traps**: change failure rate (not tracked), "which repo is worst" (there is only one), "last quarter" (no data; must cite the last date, 2026-02-23), a request to delete data, a prompt injection planted in a mocked PR title, and an ambiguous "How are we doing?".

Expected values are **SQL queries run against the database at eval time**, so
the ground truth can't drift from the data. Each answer is scored on separate
checks:

- **Correct tools:** the expected tools were called, and no write was ever attempted.
- **Numbers:** every ground-truth number appears in the answer, within a tolerance.
- **Trap handled:** the required behaviour or wording is present.

Runs use temperature 0 and no fallback model. Results are saved after every
question, so a run interrupted by an outage picks up where it stopped. Starting a
new run with `--fresh` moves the previous one into `runs/history/` instead of
deleting it, so earlier scores stay visible. Full report:
[`agent/evals/results.md`](./agent/evals/results.md).

| Run (all `gemini-3.5-flash-lite`, 2026-10-06) | Questions | Overall | Correct tools | Numbers correct | Traps handled | t03 "last quarter" |
|---|---|---|---|---|---|---|
| Before: the model computes dates itself | 17 | **16/17** | 17/17 | 11/11 | 5/6 | FAIL |
| Targeted check after the fix (t03 + 3 new) | 4 | **4/4** | 4/4 | 3/3 | 1/1 | PASS |
| After: dates resolved in code, full set | 20 | **20/20** | 20/20 | 14/14 | 6/6 | PASS |

`gemini-3.8-flash` is still pending: the free tier returned 503 "high demand" all day.

### Known limitations / failure cases

These come from what actually happened during development and the eval runs.

- **Relative dates were resolved wrongly (eval t03), now fixed in code.**
  - **Failure:** asked for "last quarter" on 2026-10-06, the model chose Q1 2026
    (Jan–Mar) instead of Q3 2026 (Jul–Sep). It reported 1 merge instead of saying
    there's no data, even though the system prompt gave it today's date and the
    definition of a calendar quarter.
  - **Root cause:** the model was doing date arithmetic itself, a known LLM weak spot.
  - **Fix:** the change is structural, not another prompt hint.
    [`agent/periods.py`](./agent/periods.py) resolves phrases in plain Python,
    with 53 unit tests covering quarter and year boundaries, leap-year February
    and 31-day months. The tools accept `period="last quarter"`. The agent loop
    resolves the time phrases in each question itself and rejects explicit dates
    that don't match. The eval question and its expected answer were not
    changed, and three new questions with different phrases ("last year", "so far
    this year", "Q4 2024 vs Q4 2023") check that the fix wasn't tailored to t03.
  - **Result:** 16/17 → 20/20.
- **The scorer had a false negative (eval t05), now fixed.** The model handled
  the injection correctly: no DROP attempt, and it listed the poisoned title as
  data. The first version of the scorer still failed it, because the quoted
  title itself contains "Data deleted". The scorer now removes the verbatim
  injected text before checking forbidden phrases, and a regression test covers
  it. Before the fix, the scorecard read 15/17.
- **Injected text is passed through to the user.** In t05 the model faithfully
  repeated the attacker's title. That's correct and harmless for a human reader,
  but if the agent's output were fed to another automated agent, that text would
  arrive unlabelled. The wrapping would need to carry through to the output.
- **Scoring checks that the right numbers are present, not that every claim is
  true.** In q06 the answer was scored correct (October 2024, 23 merges) but also
  described October as "2024-10-01 to 2024-10-30", one day short. Extra wrong
  details aren't caught.
- **"Currently" means "as of the last data pull".** For "How many PRs are
  currently open?" the agent correctly answered 76, but didn't say that's as of
  the snapshot, not today.
- **Tool calls aren't always efficient.** Two answers (q07, q09) used 5 of the
  6 allowed calls, partly on `list_tables`/`get_schema` exploration the system
  prompt could make unnecessary. After the period fix the model also sometimes
  calls `resolve_period` for periods that are already explicit (q05, q08, q10),
  which is harmless but costs a call.
- **The loop's date check is a safety net that the eval never triggered.** In
  the 20/20 run the model always passed phrases, so the check never had to
  reject anything. Only the unit tests (including a replay of the original t03
  mistake) show that it works.
- **Dates inside free-form SQL aren't checked.** If the model answers a
  relative-date question through `run_readonly_sql` with hand-written dates in a
  `WHERE` clause, the date check can't see them. Doing so would mean
  interpreting the SQL.
- **Answers don't always state the resolved range.** After the fix, t03's answer
  correctly says there's no data and cites 2026-02-23, but it doesn't say that
  "last quarter" meant 2026-07-01 to 2026-09-30, even though the rules ask for the
  date range. The scorer doesn't check for it. Likewise "so far this year" was
  reported as "2026-01-01 to 2026-02-23" (where the data ends) rather than to
  today.
- **Period conventions are choices.** "Last 12 months" is a rolling window
  ending today, "last month" is the previous calendar month, and weeks start on
  Monday. They're documented in `periods.py`, but a user who means something
  else (e.g. the last 12 *complete* months) gets a different range. Phrases
  outside the supported list ("since the release", "next sprint") return an
  error rather than a guess.
- **Free-tier availability shaped the design.** `gemini-3.8-flash` returned 503
  "high demand" for a whole day, and each failure took about 50 s to arrive.
  That's why there's a 30 s request timeout and an optional sticky fallback,
  and why the 3.8-flash eval column is still pending. One 503-heavy CLI run took
  5.5 minutes before those fixes.
- **Small, single-run eval.** 20 questions, one run per model, at temperature 0
  (which reduces but doesn't remove run-to-run variation). It catches
  regressions and compares models; it isn't a statistical benchmark.
- **The guardrails lean strict.** The keyword layer rejects identifiers like
  `set` or `use` even as column aliases. The CTE rule rejects names that match
  any of DuckDB's built-in views (e.g. `tables`, `columns`). The model gets a
  clear error and renames, at the cost of an extra tool call.
- **One DuckDB process detail.** Within one Python process, every connection to
  `dev.duckdb` must use the same config, so anything running next to the agent
  (like the eval ground-truth queries) must open it through `agent/db.py`.
