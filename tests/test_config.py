import pytest

from hn_digest.config import Config


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
        Config().validate(dry_run=True, no_llm=False)


def test_delivery_checks_all_credentials():
    with pytest.raises(ValueError, match="SMTP_USER, SMTP_PASSWORD, PUBLISH_EMAIL"):
        Config(llm_model="gemini/test").validate(dry_run=False, no_llm=False)


def test_secrets_excluded_from_config_repr():
    assert "secret-token" not in repr(Config(smtp_password="secret-token"))
