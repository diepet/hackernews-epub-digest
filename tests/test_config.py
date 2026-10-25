from pathlib import Path

import pytest
from dotenv import dotenv_values

from hn_digest.config import DEFAULT_SYSTEM_PROMPT, Config


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STORY_LIMIT", "0"),
        ("STORY_LIMIT", "-1"),
        ("STORY_LIMIT", "abc"),
        ("LLM_RPM_LIMIT", "-1"),
        ("LLM_TPM_LIMIT", "nan"),
        ("LLM_MAX_OUTPUT_TOKENS", "0"),
        ("LLM_MAX_WAIT_SECONDS", "inf"),
        ("LLM_TIMEOUT", "inf"),
        ("SMTP_PORT", "70000"),
        ("SCRAPE_CHAR_LIMIT", "-1"),
        ("MAX_RETRIES", "-1"),
    ],
)
def test_invalid_settings_fail_early(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Config.from_env()


def test_blank_smtp_port_uses_default(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "")
    assert Config.from_env().smtp_port == 465


@pytest.mark.parametrize(
    ("language", "tag"), [("Italian", "it"), ("German", "de"), ("en-US", "en-US")]
)
def test_language_metadata(monkeypatch, language, tag):
    monkeypatch.setenv("TARGET_LANGUAGE", language)
    assert Config.from_env().language_code == tag


def test_preview_needs_no_credentials():
    Config().validate(dry_run=False, no_llm=True)


def test_ai_preview_needs_model():
    with pytest.raises(ValueError, match="LLM_MODEL"):
        Config(llm_model="").validate(dry_run=True, no_llm=False)


def test_delivery_checks_all_credentials():
    with pytest.raises(ValueError, match="SMTP_USER, SMTP_PASSWORD, PUBLISH_EMAIL"):
        Config(llm_model="gemini/test").validate(dry_run=False, no_llm=False)


def test_secrets_excluded_from_config_repr():
    assert "secret-token" not in repr(Config(smtp_password="secret-token"))


def test_non_secret_defaults_match_env_example(monkeypatch):
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example", interpolate=False)
    fields = (
        "LLM_MODEL",
        "LLM_SYSTEM_PROMPT",
        "LLM_RPM_LIMIT",
        "LLM_TPM_LIMIT",
        "LLM_MAX_OUTPUT_TOKENS",
        "LLM_MAX_WAIT_SECONDS",
        "MAX_RETRIES",
        "TARGET_LANGUAGE",
        "STORY_LIMIT",
        "SCRAPE_CHAR_LIMIT",
        "HTTP_TIMEOUT",
        "LLM_TIMEOUT",
        "SMTP_TIMEOUT",
        "SMTP_HOST",
        "SMTP_PORT",
    )
    for name in fields:
        monkeypatch.delenv(name, raising=False)
    direct = Config()
    parsed = Config.from_env()
    for name in fields:
        actual = getattr(parsed, name.lower())
        assert actual == getattr(direct, name.lower())
        assert actual == type(actual)(values[name]), name
    assert parsed.language_code == "it"


@pytest.mark.parametrize("prompt", [None, "", "   "])
def test_unset_or_blank_prompt_uses_default(monkeypatch, prompt):
    if prompt is None:
        monkeypatch.delenv("LLM_SYSTEM_PROMPT", raising=False)
    else:
        monkeypatch.setenv("LLM_SYSTEM_PROMPT", prompt)
    assert Config.from_env().llm_system_prompt == DEFAULT_SYSTEM_PROMPT


def test_custom_multiline_prompt_preserved(monkeypatch):
    prompt = 'Summarize in {target_language}.\nUse JSON like {"summary": "..."}.'
    monkeypatch.setenv("LLM_SYSTEM_PROMPT", prompt)
    assert Config.from_env().llm_system_prompt == prompt


def test_env_can_override_defaults_with_disabled_limits(monkeypatch):
    monkeypatch.setenv("LLM_RPM_LIMIT", "0")
    monkeypatch.setenv("LLM_TPM_LIMIT", "0")
    monkeypatch.setenv("LLM_MODEL", "another/model")
    config = Config.from_env()
    assert config.llm_rpm_limit == config.llm_tpm_limit == 0
    assert config.llm_model == "another/model"


def test_env_example_credentials_are_not_runtime_defaults(monkeypatch):
    for name in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_SENDER_EMAIL", "PUBLISH_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="SMTP_USER, SMTP_PASSWORD, PUBLISH_EMAIL"):
        Config.from_env().validate(dry_run=False, no_llm=False)
