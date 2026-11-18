# DPIP Agent — eval results

Report generated 2026-11-18. 1 questions: 1 metric questions and 0 traps. Ground truth is computed by SQL against `dev.duckdb` at eval time (see `eval_set.json`). Runs use no fallback model, so every column is exactly one model.

## Scorecard

| Model | Status | Run date | Temperature | Overall | Correct tools | Numbers correct | Traps handled | API errors |
|---|---|---|---|---|---|---|---|---|
| `gemini-3.8-flash` | complete | 2026-11-18 | 0 | **1/1** | 1/1 | 1/1 | – | 0 |
| `gemini-3.5-flash-lite` | complete | 2026-11-18 | 0 | **1/1** | 1/1 | 1/1 | – | 0 |

## Run history (before / after)

Every run is kept. Archived runs live in `runs/history/`. Scores use the current scorer.

| Started | Model | Questions run | Passed | Correct tools | Numbers correct | Traps handled | t03 (last quarter) |
|---|---|---|---|---|---|---|---|
| 2026-11-18 19:00:00 | `gemini-3.5-flash-lite` | 1 | **1/1** | 1/1 | 1/1 | – | – |
| 2026-11-18 19:00:00 | `gemini-3.8-flash` | 1 | **1/1** | 1/1 | 1/1 | – | – |

## Per-question results

Each cell: correct tools · numbers correct · trap handled → result (– = not applicable).

| ID | Type | Question | `gemini-3.8-flash` | `gemini-3.5-flash-lite` |
|---|---|---|---|---|
| q01 | metric | How many PRs? | ✅ · ✅ · – → **PASS** | ✅ · ✅ · – → **PASS** |

## Failures in detail

None.
