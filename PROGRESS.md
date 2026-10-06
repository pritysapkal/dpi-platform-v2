## Completed
- Python 3.12 venv set up, dbt-core 1.11.15 + dbt-duckdb 1.11.0 (matched versions)
- GitHub PAT configured (GH_TOKEN)
- fetch_prs.py switched from octocat/Hello-World to encode/httpx (real, active repo) — pulls most recent 500 PRs via GitHub REST API into dev.duckdb (raw.github_pull_requests)
- fetch_prs.py extended to also pull PR reviews (one API call per PR to /pulls/{number}/reviews) into raw.github_pr_reviews (778 reviews across 312 of the 500 PRs; 188 PRs have zero reviews)
- sources.yml + stg_github__pull_requests.sql created and verified working (table: main.stg_github__pull_requests)
- stg_github__pr_reviews.sql created and verified working (table: main.stg_github__pr_reviews) — one row per review, cast submitted_at to timestamp
- int_pr_lifecycle.sql (intermediate layer) built and verified — one row per PR (grain confirmed: 500 rows, 500 distinct pr_number), left-joins a review_rollup CTE (reviews aggregated per PR to avoid fan-out) onto the PR base. Computes review_wait_hours (opened → first review), time_in_review_hours (first review → merge, null-guarded so post-merge reviews don't produce negative durations), lead_time_hours (opened → merged, feeds the future DORA mart), and pr_lifecycle_stage (merged / closed_without_merge / open). Verified no negative durations anywhere.
- Removed leftover models/example/ dbt-init scaffold (dummy models + generic tests) that was polluting dbt build/test runs; set per-layer materialization in dbt_project.yml (staging/intermediate = view, marts = table)
- mart_dora_metrics_daily.sql built and verified — one row per calendar day (1,114 days, full date spine so zero-merge days show 0 instead of a missing row) with deployment_frequency (merges that day) and avg_lead_time_hours (rounded avg of lead_time_hours for that day's merges, null on zero-merge days — not 0). Verified SUM(deployment_frequency) = 269 matches total merged PRs exactly; no negative avg_lead_time_hours.
- dbt tests added across all 4 models (schema.yml per layer) — unique/not_null on every model's primary key, plus a custom non_negative generic test macro (macros/generic_tests.sql) applied to review_wait_hours and lead_time_hours, documented with the real bug it'd catch (timestamp parsing / source anomaly corrupting the Lead Time DORA metric). `dbt build` passes 17/17 (4 models + 13 tests).
- Semantic layer built: models/marts/dora_metrics.yml defines the dora_metrics_daily semantic model (on mart_dora_metrics_daily) and the deployment_frequency + lead_time_for_changes metrics. Added models/metricflow_time_spine.sql (+ explicit time_spine YAML config, required by MetricFlow) and installed dbt-metricflow in the venv. Verified via `mf list metrics` (both show up) and `mf query --metrics deployment_frequency,lead_time_for_changes --group-by metric_time__month` (real monthly numbers matching the mart). Known caveat, documented in the YAML: lead_time_for_changes is an average of mart_dora_metrics_daily's daily averages (day-weighted), not a true PR-weighted average — a tradeoff of building the semantic layer on a pre-aggregated mart. Local note: `mf` CLI needs `PYTHONIOENCODING=utf-8` + explicit `DBT_PROFILES_DIR` to run on Windows (unrelated tooling bug in halo/colorama, not a project issue).

## Target Scope for 8-9/10 Analytics Engineering Portfolio (Locked In — Don't Expand Beyond This)

Focus: ONE data entity (Pull Requests) taken all the way through the full pipeline, done deeply and correctly — not multiple entities done shallowly.

1. **Multi-layer modeling**
   - [x] Switch from octocat/Hello-World to a real, active open-source repo (500+ PRs) — now encode/httpx
   - [x] Build int_pr_lifecycle.sql — PR journey: opened → reviewed → merged, using proper intermediate-layer logic
   - [x] Build mart_dora_metrics_daily.sql — at least 2 DORA metrics (Deployment Frequency + Lead Time for Changes)

2. **Meaningful dbt tests**
   - [x] Not just generic unique/not_null — include at least 1-2 business-rule tests (e.g., review_wait_hours >= 0)
   - [x] Document in README/comments what real issue each test would catch

3. **Semantic layer (MetricFlow)**
   - [x] Define the 2 DORA metrics as code using dbt's MetricFlow, not just raw SQL in the mart

4. **Working CI/CD**
   - [x] .github/workflows/ci.yml that actually runs dbt build + dbt test on push
   - [x] Must show a real passing (green) run on GitHub, not just exist unused — **confirmed green**: current main tip db72faa, conclusion "success" (verified via GitHub Actions API, 2026-09-20)

5. **Real documentation**
   - [x] README with: architecture diagram (Mermaid ok), data dictionary for the mart columns, "known limitations / what I'd do at scale" section
   - [x] Written in own words — not ghostwritten/templated (drafted this session — read it over and adjust tone/wording to sound like you before treating it as final)

## Explicitly Out of Scope (Don't Add These — They Dilute Analytics Engineering Positioning)
> **Scope update (2026-10-06):** the project is now also aimed at an AI-Native Software Engineering role, so an LLM agent over the finished pipeline was added deliberately — see "New Addition: DPIP Agent" below. It reads the existing marts and doesn't change the pipeline. The items below are still out of scope.
- AI-assisted PR classification
- Predictive ML modeling (e.g., predicting MTTR)
- Multiple additional data sources (commits, issues, workflows) — one entity done deeply > many done shallowly

- .github/workflows/ci.yml added — on every push and every PR to main, it checks out the repo, installs requirements.txt, writes a dbt profile pointing at a fresh dev.duckdb, re-runs fetch_prs.py to pull current PR+review data from GitHub (not a stale snapshot), then runs `dbt build` (models + tests together) as the pass/fail gate. requirements.txt added (dbt-core, dbt-duckdb, duckdb, httpx, pandas, all pinned to locally-verified versions).
  - **GH_TOKEN repo secret added on GitHub (2026-09-20).** The very first CI run (triggered by the push that added ci.yml) failed at the "Fetch latest PR + review data from GitHub" step with "GH_TOKEN not set", since it ran before the secret existed — expected, not a workflow bug. This commit re-triggers the workflow now that the secret is in place.
- README.md fully rewritten (was still dbt-init boilerplate) — architecture diagram (all 8 stages: source → ingestion → warehouse → staging → intermediate → marts → semantic layer → dashboard), data dictionary for mart_dora_metrics_daily, test-to-real-bug table, 7-point known-limitations/at-scale section, and a local run guide.

## Locked Scope Status
All 5 locked-in scope items are complete: built, verified locally (`dbt build` passes 20/20), pushed, and CI confirmed green on GitHub (run #2, commit f1cd57e). The locked-in 8-9/10 portfolio scope from this file is fully delivered.

## New Addition: Dashboard (beyond locked scope, added by request)
- [x] Attempted Evidence.dev first, as originally requested. Discovered its default scaffolding tool now installs "Evidence Studio," a hosted commercial product whose no-login/no-data-copy connectors are limited to Snowflake/Fabric/ClickHouse/BigQuery — no local DuckDB file support at all. The classic open-source, DuckDB-native Evidence still exists on npm but its docs have been fully replaced by the new product's, making setup an undocumented reverse-engineering exercise rather than a supported path. Flagged this to the user as a real blocker rather than silently working around it or picking a path that contradicted "no data duplication."
- [x] Built dashboard/app.py instead — a single-page Streamlit dashboard, read-only connection to dev.duckdb (won't lock the file against dbt), querying mart_dora_metrics_daily directly with no recomputation in Python. Three summary tiles (Total PRs Merged, Avg Lead Time hrs, Days Tracked) plus two line charts (Deployment Frequency, Lead Time for Changes) each with a plain-language caption on what a healthy trend looks like.
- [x] Verified with real data: `streamlit.testing.v1.AppTest` run against the live app confirmed no exceptions and correct values — 269 total merged PRs, 258.2 avg lead-time hours, 1,114 days tracked (all matching previously-validated mart numbers exactly). Also manually confirmed the running server responds (HTTP 200 on localhost:8501).
- [x] dashboard/requirements.txt (streamlit==1.64.0, duckdb==1.5.5) and dashboard/README.md (one-line run instructions) added.
- [x] Main README.md updated: architecture diagram now names the dashboard stage explicitly, a new "Dashboard" section explains the Evidence.dev pivot and how to run it, "Running it locally" includes the dashboard command, and a known-limitations bullet notes it's a script (no auth/sharing), not a full BI tool.

## New Addition: DPIP Agent (AI-native extension, 2026-10-06)
A tool-calling Gemini agent in `agent/` that answers plain-English questions about the DORA metrics. No dbt models were changed.
- [x] **Step 0 — repo review.** Found three gaps between the original brief and the data: there is only one repo (encode/httpx, no repo dimension), no change failure rate (no deployment/incident data), and data ends 2026-02-23 (so "last quarter" has none). Decisions: `repo` accepts only encode/httpx, CFR is reported as "not available" (no proxy metric), and a `get_data_coverage()` tool plus today's date in the system prompt.
- [x] **Step 1 — setup.** `agent/` package, pinned `agent/requirements.txt` (google-genai 2.28.0, duckdb 1.5.5, python-dotenv 1.2.3, pytest 9.1.1), `agent/.env` (gitignored) and `.env.example`. DuckDB is opened `read_only=True` with `enable_external_access=False`. Default model `gemini-3.8-flash` (`gemini-2.5-flash` is closed to new keys); `gemini-3.5-flash-lite` confirmed working on the free tier.
- [x] **Step 2 — tools and guardrails.** Six tools: list_tables, get_schema, get_data_coverage, get_dora_metrics, compare_periods, run_readonly_sql. Lead time uses the **median** as the headline, plus the PR-weighted mean and the dashboard's day-weighted figure. SQL guardrails in three layers: DuckDB's parser (`json_serialize_sql`) with a whitelist walk of the parse tree, then a regex check, then the locked-down connection. **Found and blocked a CTE-shadowing bypass** (`WITH stg_x AS (SELECT * FROM stg_x)` reads the real table). 200-row cap, 10 s timeout via `con.interrupt()`, strict date validation.
- [x] **Step 3 — agent loop.** Manual function-calling loop (automatic calling disabled), max 6 tool calls, then a fixed "could not complete" answer. Bad tool calls (unknown name, bad or missing args) go back to the model as error dicts. Tool results are wrapped as `untrusted_tool_data` and size-capped (prompt-injection defence). JSONL traces with the model recorded on every line and no API key. Retries only 429/500/503/timeouts: 5 attempts, backoff + jitter, honouring Retry-After and Gemini's retryDelay. 400/401/403/404 fail fast. 30 s request timeout. Optional visible `GEMINI_FALLBACK_MODEL` and a `--sticky-fallback` flag. The Gemini call sits behind a `ModelClient` interface so the loop is tested offline with a fake model.
- [x] **Step 4 — evals.** 17 questions (11 metric, 6 traps: CFR, single repo, last quarter, delete request, mocked prompt injection, ambiguous). Ground truth is SQL run at eval time. Each question is scored on correct tools, numbers within tolerance, and trap handling. Runs are resumable (saved per question), use temperature 0 and no fallback, and take a `--model` argument. **gemini-3.5-flash-lite: 16/17** (17/17 tools, 11/11 numbers, 5/6 traps). The one real failure: it resolved "last quarter" to Q1 2026 instead of Q3 2026. A scorer false negative on the injection trap was found and fixed (regression test added). gemini-3.8-flash is **pending**: it returned 503 "high demand" all day.
- [x] **Fix for t03: relative dates resolved in code (2026-10-06).**
  - **Failure:** "last quarter" on 2026-10-06 became Q1 2026 instead of Q3 2026.
  - **Root cause:** the LLM was doing date arithmetic itself, despite today's date being in the prompt.
  - **Fix (structural, not a prompt tweak):**
    - New `agent/periods.py` with `resolve_period(phrase, today)`: calendar quarters, Monday weeks, correct month ends and leap years, rolling "last N days/weeks/months", explicit quarters, halves, months and years. Unrecognised phrases return an error, never a guess.
    - `resolve_period` is exposed as a tool, and `get_dora_metrics` / `compare_periods` accept a `period` phrase (rejected if dates are passed too).
    - The system prompt now says never to compute dates.
    - **The enforcing mechanism:** the agent loop resolves time phrases found in the question and rejects any explicit date pair that doesn't match.
  - **Evals:** t03 left unchanged; 3 new questions (last year, this year, Q4 2024 vs Q4 2023) with `current_date`-based SQL ground truth.
  - **Scorer:**
    - It now removes dates before matching numbers, so "2026-01-01" can't supply a "1".
    - `--fresh` archives the previous run to `runs/history/` so results.md keeps a before/after table.
  - **Result on gemini-3.5-flash-lite:** before 16/17 (t03 FAIL); targeted after-check 4/4; full fresh run **20/20** (20/20 tools, 14/14 numbers, 6/6 traps).
  - **Caveat:** the loop's date check never fired in the eval (the model used phrases); it is proven by unit tests only.
- [x] **201 unit tests** pass in about 6 s with no API key: guardrails, period resolution, tools against the real DB, the agent loop with a fake model (including the date check), the retry/fallback client with a fake SDK, and the scorer.
- [ ] Step 5 (Streamlit chat page + MCP server) — deferred; optional.

## Next Immediate Step
- Re-run the eval on gemini-3.8-flash once it stops returning 503: `python -m agent.evals.run_evals --model gemini-3.8-flash` (resumable; fills in the pending column in agent/evals/results.md and the README table)
- Regenerate the Gemini API key before publishing the repo (the current one was pasted into a chat)
- Review README.md tone/wording to make sure it reads as your own voice, not mine (now including the DPIP Agent section)
