"""Guardrails enforced in code, so safety never depends on the model obeying its prompt.

Defence in depth, three layers for free-form SQL:
  1. DuckDB's own parser (json_serialize_sql): exactly one SELECT statement, and every
     table it touches must be on the whitelist. This is the primary check.
  2. A regex check on the raw text, as a second, independent opinion.
  3. The database connection itself is read-only with external access disabled (db.py),
     so even a query that fooled both checks could not write data or read files.
"""

import datetime
import json
import re

import duckdb

# Only the dbt outputs meant for consumption. raw.*, staging and the time spine stay hidden.
ALLOWED_TABLES = frozenset({"mart_dora_metrics_daily", "int_pr_lifecycle"})
ALLOWED_SCHEMAS = frozenset({"", "main"})
ALLOWED_CATALOGS = frozenset({"", "dev"})  # "dev" is the database name of dev.duckdb

MAX_RESULT_ROWS = 200
MAX_QUERY_CHARS = 5000

# Statements that change data or settings, or reach outside the database.
BLOCKED_KEYWORDS = (
    "insert", "update", "delete", "merge", "truncate", "drop", "create", "alter",
    "attach", "detach", "copy", "export", "import", "pragma", "set", "reset",
    "install", "load", "call", "vacuum", "checkpoint", "use",
)
_BLOCKED_KEYWORD_RE = re.compile(r"\b(" + "|".join(BLOCKED_KEYWORDS) + r")\b", re.IGNORECASE)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ALLOWED_REPO = "encode/httpx"


class GuardrailError(ValueError):
    """Raised when input breaks a guardrail. The message is shown to the model so it can recover."""


# ---------- free-form SQL ----------

def validate_sql(query: str, existing_objects: set[str]) -> None:
    """Raise GuardrailError unless `query` is a single read-only SELECT on whitelisted tables.

    existing_objects: every real table/view name in the database (lowercase). Needed to
    stop a CTE from shadowing a real table, see _check_tables.
    """
    if not query or not query.strip():
        raise GuardrailError("Query is empty.")
    if len(query) > MAX_QUERY_CHARS:
        raise GuardrailError(f"Query is longer than {MAX_QUERY_CHARS} characters.")

    tree = _parse(query)  # layer 1
    _check_tables(tree, existing_objects)
    _check_text(query)  # layer 2


def _parse(query: str) -> dict:
    """Parse with DuckDB's real SQL parser. It only serializes SELECT statements, so any
    INSERT/DROP/PRAGMA/etc., anywhere in the text, makes it return an error."""
    # An in-memory connection: parsing needs no data, and tests can run without dev.duckdb.
    con = duckdb.connect(":memory:", config={"enable_external_access": False})
    try:
        result = con.execute("select json_serialize_sql(?)", [query]).fetchone()[0]
    finally:
        con.close()
    tree = json.loads(result)

    if tree.get("error"):
        raise GuardrailError(
            "Only a single read-only SELECT (or WITH ... SELECT) statement is allowed. "
            f"Parser said: {tree.get('error_message', 'invalid SQL')}"
        )
    if len(tree["statements"]) != 1:
        raise GuardrailError("Only one statement is allowed; remove the extra ';' statements.")
    return tree


def _walk(node, table_refs: list, functions: list, cte_names: set) -> None:
    """Recursively collect table references, table functions and CTE names from the parse tree.

    Walking every dict/list (instead of only FROM clauses) means subqueries in WHERE,
    SELECT, JOIN, UNION and nested CTEs are all covered without special cases.
    """
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type == "BASE_TABLE":
            table_refs.append(node)
        elif node_type == "TABLE_FUNCTION":
            functions.append(node.get("function", {}).get("function_name", "unknown"))
        for entry in node.get("cte_map", {}).get("map", []):
            cte_names.add(entry["key"].lower())
        for value in node.values():
            _walk(value, table_refs, functions, cte_names)
    elif isinstance(node, list):
        for item in node:
            _walk(item, table_refs, functions, cte_names)


def _check_tables(tree: dict, existing_objects: set[str]) -> None:
    table_refs, functions, cte_names = [], [], set()
    _walk(tree, table_refs, functions, cte_names)

    # Table functions (read_csv, glob, query, duckdb_tables, ...) can read files or
    # metadata. None are needed for analytics on two tables, so all are rejected.
    if functions:
        raise GuardrailError(f"Table functions are not allowed: {', '.join(sorted(set(functions)))}.")

    # A CTE named like a real table is a bypass: in DuckDB,
    #   WITH stg_x AS (SELECT * FROM stg_x) ...
    # reads the REAL stg_x inside the CTE body. Forbidding such names means an
    # unqualified CTE name can only ever resolve to the CTE itself.
    shadowing = cte_names & existing_objects
    if shadowing:
        raise GuardrailError(
            f"CTE name(s) {sorted(shadowing)} clash with real tables; pick different CTE names."
        )

    for ref in table_refs:
        name = ref.get("table_name", "").lower()
        schema = ref.get("schema_name", "").lower()
        catalog = ref.get("catalog_name", "").lower()

        if not schema and not catalog and name in cte_names:
            continue  # reference to a CTE defined in this query
        if name in ALLOWED_TABLES and schema in ALLOWED_SCHEMAS and catalog in ALLOWED_CATALOGS:
            continue
        full_name = ".".join(p for p in (catalog, schema, name) if p)
        raise GuardrailError(
            f"Table '{full_name}' is not allowed. Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}."
        )


def _strip_comments_and_strings(query: str) -> str:
    """Remove comments and quoted text so keywords hidden in them can't fool layer 2,
    and words inside string literals (e.g. 'delete') don't cause false alarms."""
    query = re.sub(r"--[^\n]*", " ", query)
    query = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    query = re.sub(r"'(?:[^']|'')*'", "''", query)
    query = re.sub(r'"(?:[^"]|"")*"', '""', query)
    return query


def _check_text(query: str) -> None:
    text = _strip_comments_and_strings(query).strip().rstrip(";").strip()

    if ";" in text:
        raise GuardrailError("Only one statement is allowed; remove the extra ';' statements.")
    # Also rejects SHOW / DESCRIBE / SUMMARIZE / FROM-first queries, which DuckDB's
    # parser treats as SELECTs but which can list tables outside the whitelist.
    first_word = text.split(None, 1)[0].lower() if text else ""
    if first_word not in ("select", "with"):
        raise GuardrailError("Query must start with SELECT or WITH.")
    blocked = _BLOCKED_KEYWORD_RE.search(text)
    if blocked:
        raise GuardrailError(f"Keyword '{blocked.group(1).upper()}' is not allowed in a read-only query.")


# ---------- structured tool inputs ----------

def validate_date(value: str, field: str) -> datetime.date:
    """Parse a strict YYYY-MM-DD date, or raise with a message the model can act on."""
    # strptime alone accepts '2024-1-5', so the regex enforces the exact format first.
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise GuardrailError(f"{field} must be a date in YYYY-MM-DD format, got {value!r}.")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise GuardrailError(f"{field} is not a real calendar date: {value!r}.") from None


def validate_date_range(start: str, end: str, prefix: str = "") -> tuple[datetime.date, datetime.date]:
    start_date = validate_date(start, f"{prefix}start_date")
    end_date = validate_date(end, f"{prefix}end_date")
    if start_date > end_date:
        raise GuardrailError(f"{prefix}start_date ({start}) is after {prefix}end_date ({end}).")
    return start_date, end_date


def validate_repo(repo: str | None) -> str:
    """The pipeline ingests exactly one repo; reject anything else instead of guessing."""
    if repo is None or repo.strip().lower() == ALLOWED_REPO:
        return ALLOWED_REPO
    raise GuardrailError(
        f"No data for repo {repo!r}. DPIP only ingests one repository: {ALLOWED_REPO}."
    )


def validate_table_name(table_name: str) -> str:
    """Accept 'x' or 'main.x' for a whitelisted table; return the bare name."""
    name = (table_name or "").strip().lower()
    if name.startswith("main."):
        name = name[len("main."):]
    if name not in ALLOWED_TABLES:
        raise GuardrailError(
            f"Table {table_name!r} is not available. Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}."
        )
    return name
