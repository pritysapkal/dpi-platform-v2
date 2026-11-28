# Published data snapshot (generated, do not edit)

This branch is written by the daily data refresh (.github/workflows/refresh.yml on `main`). Each successful run replaces
it with a single new commit (force-push), so it never grows. The public dashboard downloads these files when it has no
local database, checks each file's SHA-256 against manifest.json, and refuses a snapshot that does not match.

See manifest.json for when the data was pulled (data_as_of), which commit built it (source_commit) and the row counts.
