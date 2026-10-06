"""Central settings for the agent. Everything configurable lives here, read from env vars."""

import os
from pathlib import Path

from dotenv import load_dotenv

AGENT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = AGENT_DIR.parent

# .env sits next to this file so the agent is self-contained; it is gitignored.
load_dotenv(AGENT_DIR / ".env")

# Same database the dbt pipeline and the dashboard use.
DB_PATH = Path(os.environ.get("DPIP_DB_PATH", PROJECT_DIR / "dev.duckdb"))

# Not read at import time as a hard requirement, so tests can run without a key.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# Optional. Used only after retries on GEMINI_MODEL are exhausted; never silently.
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL") or None

TRACES_DIR = AGENT_DIR / "traces"


def require_api_key() -> str:
    """Return the API key, or fail with a message that says how to fix it."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy agent/.env.example to agent/.env "
            "and paste your key from https://aistudio.google.com/apikey"
        )
    return GEMINI_API_KEY
