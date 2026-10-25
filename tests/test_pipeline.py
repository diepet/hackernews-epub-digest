import json
import smtplib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hn_digest import pipeline
from hn_digest.cli import main
from hn_digest.config import Config
from hn_digest.fetching import FetchError, Story


@pytest.fixture
def sources(monkeypatch):
    stories = [Story(i, f"Story {i}", f"https://example.org/{i}") for i in (1, 2)]
    monkeypatch.setattr(pipeline, "get_top_stories", lambda *args: stories)
    monkeypatch.setattr(
        pipeline, "fetch_article_text", lambda *args: "Readable source content. " * 30
    )
    return stories


def read_report(path):
    return json.loads((path / "run-report.json").read_text())


def test_preview_builds_book_without_ai_or_email(monkeypatch, tmp_path, sources):
    ai = MagicMock(side_effect=AssertionError("AI must not be called"))
    email = MagicMock(side_effect=AssertionError("Email must not be sent"))
    monkeypatch.setattr(pipeline, "summarize_article", ai)
    monkeypatch.setattr(pipeline, "send_email", email)
    assert pipeline.run_digest(Config(), tmp_path, dry_run=False, no_llm=True) == 0
    report = read_report(tmp_path)
    assert report["extracted"] == 2
    assert report["summarized"] == 0
    assert not report["email_sent"]
    assert (tmp_path / report["epub"]).exists()
    ai.assert_not_called()
    email.assert_not_called()


def test_all_sources_fail_with_report_and_no_email(monkeypatch, tmp_path, sources):
    monkeypatch.setattr(
        pipeline, "fetch_article_text", MagicMock(side_effect=FetchError("blocked"))
    )
    email = MagicMock()
    monkeypatch.setattr(pipeline, "send_email", email)
    with pytest.raises(RuntimeError, match="No usable stories"):
        pipeline.run_digest(Config(), tmp_path, dry_run=False, no_llm=True)
    assert read_report(tmp_path)["status"] == "failed"
    assert len(read_report(tmp_path)["errors"]) == 2
    email.assert_not_called()


def test_single_blocked_site_keeps_digest_useful(monkeypatch, tmp_path, sources):
    monkeypatch.setattr(
        pipeline,
        "fetch_article_text",
        MagicMock(side_effect=[FetchError("403"), "Source text" * 30]),
    )
    assert pipeline.run_digest(Config(), tmp_path, dry_run=True, no_llm=True) == 0
    assert read_report(tmp_path)["status"] == "partial"


def test_delivery_failure_is_failure_and_epub_is_retained(monkeypatch, tmp_path, sources):
    monkeypatch.setattr(pipeline, "summarize_article", lambda *args: "A useful summary.")
    monkeypatch.setattr(
        pipeline, "send_email", MagicMock(side_effect=smtplib.SMTPException("secret detail"))
    )
    with pytest.raises(smtplib.SMTPException):
        pipeline.run_digest(Config(llm_model="test"), tmp_path, dry_run=False, no_llm=False)
    report = read_report(tmp_path)
    assert not report["email_sent"]
    assert report["status"] == "failed"
    assert (tmp_path / report["epub"]).exists()
    assert "secret detail" not in json.dumps(report)


def test_partial_ai_failure_delivers_but_returns_failure(monkeypatch, tmp_path, sources):
    monkeypatch.setattr(
        pipeline,
        "summarize_article",
        MagicMock(side_effect=[pipeline.SummaryError("quota"), "Summary"]),
    )
    email = MagicMock()
    monkeypatch.setattr(pipeline, "send_email", email)
    assert pipeline.run_digest(Config(llm_model="test"), tmp_path, dry_run=False, no_llm=False) == 1
    report = read_report(tmp_path)
    assert report["status"] == "partial"
    assert report["email_sent"]
    email.assert_called_once()


def test_bad_model_stops_after_first_attempt(monkeypatch, tmp_path, sources):
    ai = MagicMock(side_effect=pipeline.ModelError("Check model"))
    monkeypatch.setattr(pipeline, "summarize_article", ai)
    with pytest.raises(pipeline.ModelError):
        pipeline.run_digest(Config(llm_model="test"), tmp_path, dry_run=True, no_llm=False)
    ai.assert_called_once()
    assert read_report(tmp_path)["failure_type"] == "ModelError"


def test_cli_failure_returns_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "test")
    monkeypatch.setattr("hn_digest.cli.run_digest", MagicMock(side_effect=RuntimeError("failure")))
    assert main(["--dry-run", "--output-dir", str(tmp_path)]) == 1


def test_cli_reports_actionable_rate_configuration_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LLM_MODEL", "test")
    monkeypatch.setattr(
        "hn_digest.cli.run_digest",
        MagicMock(
            side_effect=pipeline.ModelError(
                "LLM_TPM_LIMIT is too small: increase it or lower LLM_MAX_OUTPUT_TOKENS"
            )
        ),
    )
    assert main(["--dry-run", "--output-dir", str(tmp_path)]) == 1
    assert "LLM_TPM_LIMIT is too small" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "finish", "valid"),
    [("Summary", "stop", True), ("", "stop", False), ("Cut off", "length", False)],
)
def test_ai_response_validation_and_timeouts(monkeypatch, text, finish, valid):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)]
    )
    completion = MagicMock(return_value=response)
    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "token_counter", MagicMock(return_value=1000))
    if valid:
        assert (
            pipeline.summarize_article(
                "Source", "Title", "https://example.org", Config(llm_model="test")
            )
            == text
        )
    else:
        with pytest.raises(pipeline.SummaryError):
            pipeline.summarize_article(
                "Source", "Title", "https://example.org", Config(llm_model="test")
            )
    assert completion.call_args.kwargs["timeout"] == 90
    assert completion.call_args.kwargs["num_retries"] == 0
    assert completion.call_args.kwargs["max_retries"] == 0
