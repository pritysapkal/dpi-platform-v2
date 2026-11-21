"""Export the tables the dashboard reads to Parquet, with a manifest, for the public deployment.

    python ops/export_snapshot.py <database.duckdb> <output folder>

Writes one Parquet file per table in dashboard/data_source.py's TABLES, plus manifest.json:
    schema_version   the snapshot format (data_source.SCHEMA_VERSION); a dashboard refuses a version it does not know
    generated_at     when this export ran (UTC)
    data_as_of       when the data was pulled from GitHub: max(data_as_of) of fct_pull_requests (UTC)
    source_commit    the commit of this repository that built the data (GITHUB_SHA in Actions, else `git rev-parse`)
    tables           per table: file name, the warehouse relation it came from, row count, size and SHA-256

Before it finishes, the export reads its own files back exactly as the dashboard will (checksums, then row counts and
contents compared with the database), so a bad snapshot fails here and is never published.
The daily refresh (.github/workflows/refresh.yml) runs this after every check has passed and publishes the folder to the
`data` branch.
"""

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))

import data_source  # noqa: E402  (the reader and the writer share one definition of the format)

DATA_BRANCH_README = """# Published data snapshot (generated, do not edit)

This branch is written by the daily data refresh (.github/workflows/refresh.yml on `main`). Each successful run replaces
it with a single new commit (force-push), so it never grows. The public dashboard downloads these files when it has no
local database, checks each file's SHA-256 against manifest.json, and refuses a snapshot that does not match.

See manifest.json for when the data was pulled (data_as_of), which commit built it (source_commit) and the row counts.
"""


def source_commit() -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def utc_iso(value: datetime.datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


def export(db_path: Path, out_dir: Path, now: datetime.datetime | None = None, commit: str | None = None) -> dict:
    """Write the Parquet files and manifest.json into out_dir and return the manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        (data_as_of,) = con.execute("select max(data_as_of) from main.fct_pull_requests").fetchone()
        tables = {}
        for name, relation in data_source.TABLES.items():
            file = f"{name}.parquet"
            target = str((out_dir / file).resolve()).replace("'", "''")
            # ordered by every column, so the same data always gives the same rows in the same order
            con.execute(f"copy (select * from {relation} order by all) to '{target}' (format parquet, compression zstd)")
            (rows,) = con.execute(f"select count(*) from {relation}").fetchone()
            tables[name] = {
                "file": file,
                "relation": relation,
                "rows": rows,
                "bytes": (out_dir / file).stat().st_size,
                "sha256": data_source.sha256_of(out_dir / file),
            }
    finally:
        con.close()
    manifest = {
        "schema_version": data_source.SCHEMA_VERSION,
        "generated_at": utc_iso(now),
        "data_as_of": utc_iso(data_as_of),
        "source_commit": commit or source_commit(),
        "tables": tables,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out_dir / "README.md").write_text(DATA_BRANCH_README, encoding="utf-8")
    check(db_path, out_dir)
    return manifest


def check(db_path: Path, out_dir: Path) -> None:
    """Read the snapshot back the way the dashboard does and compare every table with the database, row for row."""
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    data_source.verify(out_dir, manifest)
    data_source.open_snapshot(out_dir, manifest).close()  # opens as the dashboard does: views build, row counts match
    # Compared inside the database itself (not attached under another name): dbt's views name their own catalog.
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for name, relation in data_source.TABLES.items():
            parquet = "read_parquet('" + str((out_dir / manifest["tables"][name]["file"]).resolve()).replace("'", "''") + "')"
            for left, right in ((parquet, relation), (relation, parquet)):
                (extra,) = con.execute(f"select count(*) from (select * from {left} except all select * from {right})").fetchone()
                if extra:
                    raise data_source.SnapshotInvalid(f"{name}: {extra} row(s) in {left} that are not in {right}")
            types = [con.execute(f"select column_name, column_type from (describe select * from {rel})").fetchall() for rel in (parquet, relation)]
            if types[0] != types[1]:
                raise data_source.SnapshotInvalid(f"{name}: columns differ: {types[0]} vs {types[1]}")
    finally:
        con.close()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python ops/export_snapshot.py <database.duckdb> <output folder>", file=sys.stderr)
        return 2
    manifest = export(Path(argv[1]), Path(argv[2]))
    counts = ", ".join(f"{name} {entry['rows']:,}" for name, entry in manifest["tables"].items())
    print(f"snapshot written to {argv[2]}: data as of {manifest['data_as_of']}, {counts}; read back and checked")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
