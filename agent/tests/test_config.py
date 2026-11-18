"""The default model is stated in three places (config.py, .env.example, README); keep them in step."""

import importlib
import re
from pathlib import Path

import pytest

from agent import config

PROJECT = config.PROJECT_DIR
EXPECTED_DEFAULT = "gemini-3.5-flash-lite"


@pytest.fixture
def clean_config(monkeypatch):
    """Reload config with no GEMINI_MODEL in the environment and without reading agent/.env,
    so the test sees the built-in default whatever the developer has set locally."""
    import dotenv

    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    yield importlib.reload(config)
    monkeypatch.undo()
    importlib.reload(config)  # put the real settings back for the other tests


def test_default_model_is_the_one_that_answers_on_the_free_tier(clean_config):
    assert clean_config.GEMINI_MODEL == EXPECTED_DEFAULT == clean_config.DEFAULT_MODEL


def test_gemini_model_can_still_be_overridden_from_the_environment(monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    try:
        assert importlib.reload(config).GEMINI_MODEL == "gemini-3.8-flash"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_env_example_documents_the_same_default_and_the_override():
    text = (PROJECT / "agent" / ".env.example").read_text(encoding="utf-8")
    assert f"Default is {EXPECTED_DEFAULT}" in text
    assert "# GEMINI_MODEL=gemini-3.8-flash" in text  # the override stays available, commented out
    assert "503/504" in text


def test_readme_states_the_same_default_and_why_3_8_is_not_evaluated():
    text = " ".join((PROJECT / "README.md").read_text(encoding="utf-8").split())
    match = re.search(r"`GEMINI_MODEL`: defaults to `([\w.\-]+)`", text)
    assert match and match.group(1) == EXPECTED_DEFAULT
    assert "`gemini-3.8-flash` can be chosen with this setting" in text
    assert "503 and 504" in text and "2026-11-18" in text
