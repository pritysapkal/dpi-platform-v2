"""Database access. The connection is locked down by DuckDB itself, independent of our SQL checks."""

import datetime
import math
import threading
from decimal import Decimal

import duckdb

from agent.config import DB_PATH

QUERY_TIMEOUT_SECONDS = 10


class QueryTimeout(Exception):
    """Raised when a query runs longer than the allowed time."""


def get_connection() -> duckdb.DuckDBPyConnection:
    """Open dev.duckdb so that the database itself refuses anything but reads.

    read_only=True                -> DuckDB rejects INSERT/UPDATE/DELETE/CREATE/DROP.
    enable_external_access=False  -> DuckDB rejects file access (read_csv, glob, ATTACH)
                                     and extension INSTALL/LOAD. It cannot be switched
                                     back on with SET while the database is open.
    So even if a query slips past guardrails.py, it still cannot write or read files.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} not found. Build it first: python fetch_prs.py && dbt build"
        )
    return duckdb.connect(
        str(DB_PATH),
        read_only=True,
        config={"enable_external_access": False},
    )


def to_json_safe(value):
    """Convert DuckDB result values into plain JSON types the model can read."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def run_query(
    sql: str,
    params: list | None = None,
    max_rows: int | None = None,
    timeout_seconds: float = QUERY_TIMEOUT_SECONDS,
) -> tuple[list[str], list[list], bool]:
    """Run one read query with a timeout. Returns (columns, rows, truncated).

    DuckDB has no per-statement timeout setting, so a timer thread calls
    con.interrupt(), which makes the running query raise InterruptException.
    """
    con = get_connection()
    timer = threading.Timer(timeout_seconds, con.interrupt)
    timer.start()
    try:
        cursor = con.execute(sql, params or [])
        columns = [d[0] for d in cursor.description]
        if max_rows is None:
            raw_rows = cursor.fetchall()
            truncated = False
        else:
            # Fetch one extra row so we can tell the model the result was cut off.
            raw_rows = cursor.fetchmany(max_rows + 1)
            truncated = len(raw_rows) > max_rows
            raw_rows = raw_rows[:max_rows]
    except duckdb.InterruptException as exc:
        raise QueryTimeout(f"Query exceeded the {timeout_seconds}s time limit.") from exc
    finally:
        timer.cancel()
        con.close()

    rows = [[to_json_safe(v) for v in row] for row in raw_rows]
    return columns, rows, truncated


def existing_object_names() -> set[str]:
    """Every table and view name in the database, including DuckDB's built-in views.

    Used by the guardrails to stop a CTE from shadowing a real table (see guardrails.py).
    """
    _, rows, _ = run_query(
        "select table_name from duckdb_tables() union select view_name from duckdb_views()"
    )
    return {row[0].lower() for row in rows}
