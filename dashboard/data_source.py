"""Where the dashboard's data comes from: the local database, or the published snapshot.

The order, decided each time a query runs:
  1. DPIP_DATA_SOURCE=local or =snapshot forces one of the two (no fallback between them).
  2. Otherwise the local database file (dev.duckdb, or DPIP_DB_PATH) if it exists: a developer's machine, and CI.
  3. Otherwise the published snapshot: Parquet files plus manifest.json on the `data` branch, written by the daily
     refresh (.github/workflows/refresh.yml). This is what the public deployment reads, since it has no database.

The snapshot is downloaded, every file's SHA-256 is checked against the manifest, and the verified copy is kept in a
cache folder and reused for up to an hour. If a new download fails (network, a half-published branch, a wrong
checksum), the last verified copy is used and the page says so; if there is no verified copy at all, SnapshotUnavailable
is raised and the page shows an error instead of numbers.

The snapshot is opened as an in-memory DuckDB database with one view per table, named exactly like the warehouse
tables (main.fct_pull_requests, ...), so every query in queries.py runs unchanged on either source.

Needs only the standard library and duckdb, both already in dashboard/requirements.txt.
"""

import datetime
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

# The contract between the exporter (ops/export_snapshot.py) and this reader. Bump SCHEMA_VERSION when a file is added,
# removed or changes meaning; a reader never opens a snapshot with a version it does not know.
SCHEMA_VERSION = 1
# file stem in the snapshot -> the warehouse relation it is exported from, which is also the view name it is read as
TABLES = {
    "fct_pull_requests": "main.fct_pull_requests",
    "dim_contributors": "main.dim_contributors",
    "dim_date": "main.dim_date",
    "pr_reviews": "main.stg_github__pr_reviews",
}

DEFAULT_SNAPSHOT_URL = "https://raw.githubusercontent.com/pritysapkal/dpi-platform-v2/data"
CACHE_SECONDS = 3600  # reuse a verified download for up to an hour
RETRY_AFTER_FAILURE_SECONDS = 300  # after a failed download, keep using the cached copy for 5 minutes before trying again
STALE_AFTER = datetime.timedelta(hours=48)
DOWNLOAD_TIMEOUT_SECONDS = 30

LOCAL_LABEL = "local database"
SNAPSHOT_LABEL = "published snapshot"


class SnapshotUnavailable(RuntimeError):
    """No verified snapshot could be downloaded and none is cached: there is nothing safe to show."""


class SnapshotInvalid(ValueError):
    """A downloaded snapshot does not match its manifest (checksum, missing file, unknown schema version)."""


@dataclass
class Source:
    kind: str  # "local" or "snapshot"
    label: str  # shown in the page caption
    db_path: Path | None = None
    snapshot_dir: Path | None = None
    manifest: dict = field(default_factory=dict)
    notice: str | None = None  # shown as a banner, e.g. "using the cached copy because ..."
    connection: duckdb.DuckDBPyConnection | None = None

    def connect(self):
        """A connection to run one query on. The caller closes it."""
        if self.kind == "local":
            return duckdb.connect(str(self.db_path), read_only=True)  # read-only: never competes with dbt for the lock
        return self.connection.cursor()  # a cursor is its own connection to the shared in-memory database


# ---------- choosing the source ----------

def choose(db_path: Path) -> str:
    """'local' or 'snapshot', from DPIP_DATA_SOURCE and whether the database file exists."""
    forced = (os.environ.get("DPIP_DATA_SOURCE") or "").strip().lower()
    if forced in ("local", "snapshot"):
        return forced
    if forced:
        raise ValueError(f"DPIP_DATA_SOURCE must be 'local' or 'snapshot', got {forced!r}")
    return "local" if db_path.exists() else "snapshot"


def snapshot_url() -> str:
    return (os.environ.get("DPIP_SNAPSHOT_URL") or DEFAULT_SNAPSHOT_URL).rstrip("/")


def cache_root() -> Path:
    return Path(os.environ.get("DPIP_SNAPSHOT_CACHE") or Path(tempfile.gettempdir()) / "dpip-snapshot-cache")


_lock = threading.Lock()
_snapshots: dict[tuple[str, str], "_SnapshotState"] = {}


def active(db_path: Path) -> Source:
    """The source the next query should run on."""
    if choose(db_path) == "local":
        if not db_path.exists():
            raise FileNotFoundError(f"DPIP_DATA_SOURCE=local but {db_path} does not exist")
        return Source(kind="local", label=LOCAL_LABEL, db_path=db_path)
    key = (snapshot_url(), str(cache_root()))
    with _lock:
        state = _snapshots.setdefault(key, _SnapshotState(*key))
        return state.current()


def is_stale(data_as_of: datetime.datetime, now: datetime.datetime | None = None) -> bool:
    """More than 48 hours between the last pull from GitHub (naive UTC) and now."""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now - data_as_of > STALE_AFTER


# ---------- the manifest and its checks ----------

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(folder: Path, manifest: dict) -> None:
    """Raise SnapshotInvalid unless every table in TABLES is present with the size and checksum the manifest states."""
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotInvalid(f"schema version {manifest.get('schema_version')!r}, this dashboard reads {SCHEMA_VERSION}")
    listed = manifest.get("tables") or {}
    if set(listed) != set(TABLES):
        raise SnapshotInvalid(f"the manifest lists {sorted(listed)}, expected {sorted(TABLES)}")
    for name, entry in listed.items():
        path = folder / entry["file"]
        if Path(entry["file"]).name != entry["file"] or not path.is_file():
            raise SnapshotInvalid(f"{entry['file']} is missing")
        actual = sha256_of(path)
        if actual != entry["sha256"]:
            raise SnapshotInvalid(f"{entry['file']}: checksum {actual[:12]}… does not match the manifest's {entry['sha256'][:12]}…")


def open_snapshot(folder: Path, manifest: dict) -> duckdb.DuckDBPyConnection:
    """An in-memory database with one view per table, named like the warehouse table it came from."""
    con = duckdb.connect(":memory:")
    for name, entry in manifest["tables"].items():
        schema, view = TABLES[name].split(".")
        con.execute(f"create schema if not exists {schema}")
        parquet = str((folder / entry["file"]).resolve()).replace("'", "''")
        con.execute(f"create view {schema}.{view} as select * from read_parquet('{parquet}')")
        (rows,) = con.execute(f"select count(*) from {schema}.{view}").fetchone()
        if rows != entry["rows"]:
            con.close()
            raise SnapshotInvalid(f"{entry['file']} has {rows} rows, the manifest says {entry['rows']}")
    return con


# ---------- download and cache ----------

def _fetch(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, open(destination, "wb") as out:
        shutil.copyfileobj(response, out)


def download(base_url: str, into: Path) -> dict:
    """Download manifest.json and every file it lists into `into`, verify them, and return the manifest."""
    into.mkdir(parents=True, exist_ok=True)
    _fetch(f"{base_url}/manifest.json", into / "manifest.json")
    manifest = json.loads((into / "manifest.json").read_text(encoding="utf-8"))
    for entry in (manifest.get("tables") or {}).values():
        name = Path(entry["file"]).name  # never a path from the manifest: only a plain file name inside `into`
        _fetch(f"{base_url}/{name}", into / name)
    verify(into, manifest)
    return manifest


class _SnapshotState:
    """One snapshot URL + cache folder: the verified copy in use, and when to look for a newer one.

    Each verified download goes into its own folder named after the manifest's checksum, and `current.json` in the cache
    root names the folder in use, so a new download never overwrites files an open view still reads.
    """

    def __init__(self, base_url: str, root: str):
        self.base_url = base_url
        self.root = Path(root)
        self.source: Source | None = None
        self.next_check = 0.0

    def _pointer(self) -> Path:
        return self.root / "current.json"

    def _load_cached(self) -> tuple[Path, dict, float] | None:
        """The last verified copy on disk (folder, manifest, when it was downloaded), checked again before use."""
        try:
            pointer = json.loads(self._pointer().read_text(encoding="utf-8"))
            folder = self.root / pointer["folder"]
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            verify(folder, manifest)
            return folder, manifest, float(pointer["downloaded_at"])
        except (OSError, ValueError, KeyError):
            return None

    def _use(self, folder: Path, manifest: dict, notice: str | None) -> Source:
        if self.source is None or self.source.snapshot_dir != folder:
            old = self.source
            self.source = Source(kind="snapshot", label=SNAPSHOT_LABEL, snapshot_dir=folder, manifest=manifest,
                                 connection=open_snapshot(folder, manifest))
            if old is not None and old.connection is not None:
                old.connection.close()
        self.source.notice = notice
        return self.source

    def _download_new(self) -> tuple[Path, dict]:
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="incoming-", dir=self.root))
        try:
            manifest = download(self.base_url, staging)
            folder = self.root / ("snapshot-" + sha256_of(staging / "manifest.json")[:16])
            if folder.exists():
                shutil.rmtree(staging)
            else:
                staging.rename(folder)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        pointer = {"folder": folder.name, "downloaded_at": time.time()}
        tmp = self._pointer().with_suffix(".tmp")
        tmp.write_text(json.dumps(pointer), encoding="utf-8")
        os.replace(tmp, self._pointer())
        self._prune(keep={folder.name, self.source.snapshot_dir.name if self.source else ""})
        return folder, manifest

    def _prune(self, keep: set[str]) -> None:
        """Delete older snapshot folders, keeping the new one and the one open views may still read."""
        for old in self.root.glob("snapshot-*"):
            if old.name not in keep:
                shutil.rmtree(old, ignore_errors=True)

    def current(self) -> Source:
        now = time.time()
        if self.source is not None and now < self.next_check:
            return self.source
        cached = self._load_cached()
        if cached and now - cached[2] < CACHE_SECONDS:  # a fresh enough verified copy: no download
            self.next_check = cached[2] + CACHE_SECONDS
            return self._use(cached[0], cached[1], None)
        try:
            folder, manifest = self._download_new()
        except Exception as error:  # noqa: BLE001 - any failure falls back to the cached copy or a clear error
            reason = f"{type(error).__name__}: {error}"
            if not cached:
                raise SnapshotUnavailable(f"could not download the published snapshot from {self.base_url} ({reason})") from error
            self.next_check = now + RETRY_AFTER_FAILURE_SECONDS
            when = datetime.datetime.fromtimestamp(cached[2], datetime.timezone.utc)
            return self._use(cached[0], cached[1],
                             f"The newest published snapshot could not be downloaded ({reason}), so this page shows the "
                             f"copy downloaded at {when:%Y-%m-%d %H:%M} UTC.")
        self.next_check = now + CACHE_SECONDS
        return self._use(folder, manifest, None)


def reset() -> None:
    """Forget every open snapshot (tests use this to start clean)."""
    with _lock:
        for state in _snapshots.values():
            if state.source is not None and state.source.connection is not None:
                state.source.connection.close()
        _snapshots.clear()
