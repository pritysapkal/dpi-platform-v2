"""Step 1 check: DB opens read-only and one Gemini call works.

Run from dpi_project/:  python -m agent.smoke_test
"""

from google import genai

from agent.config import DB_PATH, GEMINI_MODEL, require_api_key
from agent.db import get_connection


def check_db() -> None:
    con = get_connection()
    rows = con.execute("select count(*) from main.mart_dora_metrics_daily").fetchone()[0]
    con.close()
    print(f"[ok] DuckDB read-only: {DB_PATH.name}, mart_dora_metrics_daily has {rows} rows")


def check_gemini() -> None:
    client = genai.Client(api_key=require_api_key())
    response = client.models.generate_content(
        model=GEMINI_MODEL, contents="Reply with exactly: pong"
    )
    print(f"[ok] Gemini {GEMINI_MODEL} replied: {response.text.strip()!r}")


if __name__ == "__main__":
    check_db()
    check_gemini()
