"""Provider selection and real-call opt-in rules."""

from __future__ import annotations

import pytest

from paperrelay.config import ConfigError, load_settings, read_groq_api_key


def test_default_is_offline_double():
    s = load_settings({})
    assert s.provider == "offline" and s.source == "offline-double"
    assert s.real_call_ceiling is None


def test_groq_requires_explicit_ceiling_and_run_id():
    with pytest.raises(ConfigError):
        load_settings({"PAPERRELAY_PROVIDER": "groq", "PAPERRELAY_RUN_ID": "r"})
    with pytest.raises(ConfigError):
        load_settings({"PAPERRELAY_PROVIDER": "groq", "PAPERRELAY_REAL_CALL_CEILING": "0",
                       "PAPERRELAY_RUN_ID": "r"})
    with pytest.raises(ConfigError):
        load_settings({"PAPERRELAY_PROVIDER": "groq", "PAPERRELAY_REAL_CALL_CEILING": "8"})
    s = load_settings({"PAPERRELAY_PROVIDER": "groq", "PAPERRELAY_REAL_CALL_CEILING": "8",
                       "PAPERRELAY_RUN_ID": "smoke-01"})
    assert s.source == "real" and s.real_call_ceiling == 8


@pytest.mark.parametrize("bad", ["../x", "a/b", "c:\\d", ".hidden"])
def test_run_id_cannot_escape_evidence_dir(bad):
    with pytest.raises(ConfigError):
        load_settings({"PAPERRELAY_RUN_ID": bad})


def test_unknown_provider_rejected():
    with pytest.raises(ConfigError):
        load_settings({"PAPERRELAY_PROVIDER": "openai"})


def test_api_key_read_from_dotenv_without_env(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("OTHER=1\nGROQ_API_KEY=\"abc123\"\n", encoding="utf-8")
    assert read_groq_api_key({}, dotenv) == "abc123"
    assert read_groq_api_key({"GROQ_API_KEY": "fromenv"}, dotenv) == "fromenv"
    assert read_groq_api_key({}, tmp_path / "missing.env") is None
    dotenv.write_text("GROQ_API_KEY=\n", encoding="utf-8")
    assert read_groq_api_key({}, dotenv) is None
