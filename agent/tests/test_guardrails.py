"""Guardrail unit tests. No database file and no Gemini API needed.

Run from dpi_project/:  python -m pytest agent/tests -v
"""

import pytest

from agent.guardrails import (
    GuardrailError,
    validate_date,
    validate_date_range,
    validate_repo,
    validate_sql,
    validate_table_name,
)

# A realistic catalog: the whitelisted tables plus non-whitelisted ones a CTE could shadow.
EXISTING_OBJECTS = {
    "mart_dora_metrics_daily",
    "int_pr_lifecycle",
    "stg_github__pull_requests",
    "stg_github__pr_reviews",
    "github_pull_requests",
    "metricflow_time_spine",
    "duckdb_tables",
    "tables",
}


def assert_blocked(query: str, match: str | None = None):
    with pytest.raises(GuardrailError, match=match):
        validate_sql(query, EXISTING_OBJECTS)


# ---------- malicious / write SQL is blocked ----------

@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO main.mart_dora_metrics_daily VALUES (now(), 1, 1)",
        "UPDATE main.int_pr_lifecycle SET pr_title = 'x'",
        "DELETE FROM main.mart_dora_metrics_daily",
        "DROP TABLE main.mart_dora_metrics_daily",
        "CREATE TABLE t AS SELECT * FROM main.int_pr_lifecycle",
        "ALTER TABLE main.int_pr_lifecycle ADD COLUMN x INT",
        "ATTACH 'other.db' AS other",
        "COPY main.int_pr_lifecycle TO 'out.csv'",
        "PRAGMA database_list",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET enable_external_access = true",
        "EXPORT DATABASE 'dump'",
    ],
)
def test_write_and_admin_statements_blocked(query):
    assert_blocked(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1; SELECT 2",
        "SELECT * FROM main.mart_dora_metrics_daily; DROP TABLE main.mart_dora_metrics_daily",
    ],
)
def test_multiple_statements_blocked(query):
    assert_blocked(query)


@pytest.mark.parametrize(
    "query",
    [
        # the DROP is real SQL, only the text before it is hidden in a comment
        "SELECT 1 /* harmless report */; DROP TABLE main.mart_dora_metrics_daily",
        "SELECT 1 -- harmless report\n; DROP TABLE main.mart_dora_metrics_daily",
    ],
)
def test_comment_hidden_drop_blocked(query):
    assert_blocked(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM read_csv('C:/secrets.csv')",
        "SELECT * FROM read_parquet('data.parquet')",
        "SELECT * FROM glob('*')",
        "SELECT * FROM duckdb_tables()",
        "SELECT * FROM query('SELECT * FROM raw.github_pull_requests')",
    ],
)
def test_table_functions_blocked(query):
    assert_blocked(query, match="Table functions are not allowed")


def test_read_csv_blocked():
    assert_blocked("SELECT * FROM read_csv('/etc/passwd')", match="read_csv")


@pytest.mark.parametrize(
    "query",
    [
        "FROM 'data.csv'",  # DuckDB "replacement scan" reads a file by name
        "SHOW TABLES",  # parsed as a SELECT by DuckDB, caught by the text layer
        "DESCRIBE raw.github_pull_requests",
        "SUMMARIZE main.mart_dora_metrics_daily",
    ],
)
def test_non_select_forms_blocked(query):
    assert_blocked(query)


# ---------- table whitelist ----------

@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM raw.github_pull_requests",
        "SELECT * FROM stg_github__pull_requests",
        "SELECT * FROM main.metricflow_time_spine",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM other_db.main.mart_dora_metrics_daily",
        "SELECT * FROM main.int_pr_lifecycle i JOIN raw.github_pr_reviews r ON i.pr_number = r.pr_number",
        "SELECT pr_number FROM main.int_pr_lifecycle UNION ALL SELECT number FROM raw.github_pull_requests",
    ],
)
def test_non_whitelisted_table_blocked(query):
    assert_blocked(query, match="is not allowed")


@pytest.mark.parametrize(
    "query",
    [
        # subquery in WHERE
        "SELECT * FROM main.int_pr_lifecycle WHERE pr_number IN (SELECT number FROM raw.github_pull_requests)",
        # scalar subquery in the SELECT list
        "SELECT (SELECT count(*) FROM stg_github__pr_reviews) AS n FROM main.mart_dora_metrics_daily",
        # subquery inside a CTE
        "WITH x AS (SELECT * FROM (SELECT * FROM raw.github_pr_reviews)) SELECT * FROM x",
    ],
)
def test_subquery_with_non_whitelisted_table_blocked(query):
    assert_blocked(query, match="is not allowed")


@pytest.mark.parametrize(
    "query",
    [
        # Verified against DuckDB: inside its own body, this CTE name resolves to the
        # REAL stg view, so allowing CTE names blindly would bypass the whitelist.
        "WITH stg_github__pull_requests AS (SELECT * FROM stg_github__pull_requests) "
        "SELECT * FROM stg_github__pull_requests",
        # forward reference: inside a's body, b is not yet defined and would hit the real table
        "WITH a AS (SELECT * FROM github_pull_requests), github_pull_requests AS (SELECT 1) SELECT * FROM a",
        "WITH tables AS (SELECT * FROM tables) SELECT * FROM tables",
    ],
)
def test_cte_shadowing_real_table_blocked(query):
    assert_blocked(query, match="clash with real tables")


# ---------- valid queries pass ----------

@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM main.mart_dora_metrics_daily",
        "select sum(deployment_frequency) from mart_dora_metrics_daily",
        "SELECT * FROM dev.main.int_pr_lifecycle LIMIT 5",
        "SELECT author_login, count(*) FROM main.int_pr_lifecycle GROUP BY 1 ORDER BY 2 DESC;",
        # keywords inside string literals and as part of column names are fine
        "SELECT 'delete me' AS note, updated_count FROM (SELECT 1 AS updated_count)",
        "SELECT pr_number FROM main.int_pr_lifecycle WHERE pr_title ILIKE '%drop%'",
        "SELECT m.metric_date, i.pr_number FROM main.mart_dora_metrics_daily m "
        "JOIN main.int_pr_lifecycle i ON cast(i.merged_at AS date) = cast(m.metric_date AS date)",
        "SELECT median(lead_time_hours) OVER (ORDER BY merged_at) FROM main.int_pr_lifecycle",
    ],
)
def test_valid_select_passes(query):
    validate_sql(query, EXISTING_OBJECTS)  # must not raise


def test_cte_over_allowed_table_passes():
    query = """
        WITH monthly AS (
            SELECT date_trunc('month', metric_date) AS month,
                   sum(deployment_frequency) AS merges
            FROM main.mart_dora_metrics_daily
            GROUP BY 1
        ),
        ranked AS (
            SELECT *, rank() OVER (ORDER BY merges DESC) AS r FROM monthly
        )
        SELECT * FROM ranked WHERE r <= 3
    """
    validate_sql(query, EXISTING_OBJECTS)


def test_recursive_cte_passes():
    validate_sql(
        "WITH RECURSIVE n AS (SELECT 1 AS i UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT * FROM n",
        EXISTING_OBJECTS,
    )


def test_empty_query_blocked():
    assert_blocked("   ", match="empty")


# ---------- dates, repo, table names ----------

@pytest.mark.parametrize(
    "value",
    ["2024-1-5", "2024/01/05", "05-01-2024", "2024-02-30", "2024-13-01", "last quarter", "", None, 20240105],
)
def test_bad_dates_rejected(value):
    with pytest.raises(GuardrailError):
        validate_date(value, "start_date")


def test_valid_date_accepted():
    assert validate_date("2024-02-29", "start_date").isoformat() == "2024-02-29"


def test_start_after_end_rejected():
    with pytest.raises(GuardrailError, match="is after"):
        validate_date_range("2024-06-01", "2024-01-01")


def test_same_day_range_accepted():
    validate_date_range("2024-06-01", "2024-06-01")


@pytest.mark.parametrize("repo", [None, "encode/httpx", "Encode/HTTPX"])
def test_known_repo_accepted(repo):
    assert validate_repo(repo) == "encode/httpx"


def test_unknown_repo_rejected():
    with pytest.raises(GuardrailError, match="only ingests one repository"):
        validate_repo("facebook/react")


@pytest.mark.parametrize("name", ["mart_dora_metrics_daily", "main.int_pr_lifecycle", " INT_PR_LIFECYCLE "])
def test_allowed_table_name_accepted(name):
    validate_table_name(name)


@pytest.mark.parametrize("name", ["raw.github_pull_requests", "stg_github__pull_requests", "", "x; drop"])
def test_disallowed_table_name_rejected(name):
    with pytest.raises(GuardrailError):
        validate_table_name(name)
