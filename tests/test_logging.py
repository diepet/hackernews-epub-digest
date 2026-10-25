import json
import logging
import smtplib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import structlog
from urllib3.response import HTTPResponse

from hn_digest import fetching, pipeline
from hn_digest.cli import main
from hn_digest.llm import usage_counts
from hn_digest.logging_config import configure_logging, safe_url


@pytest.fixture(autouse=True)
def restore_logging(monkeypatch):
    root = logging.getLogger()
    app = logging.getLogger("hn_digest")
    handlers, root_level, app_level = root.handlers[:], root.level, app.level
    configuration = structlog.get_config().copy()
    context = structlog.contextvars.get_contextvars().copy()
    monkeypatch.setenv("LOG_FORMAT", "text")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("ROOT_LOG_LEVEL", "WARNING")
    yield
    root.handlers, root.level, app.level = handlers, root_level, app_level
    structlog.configure(**configuration)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**context)


def test_text_logging_has_clear_stage_and_story_without_repeated_metadata(capsys):
    configure_logging()
    with structlog.contextvars.bound_contextvars(story_index=2, story_total=30, story_id=123):
        structlog.get_logger("hn_digest.example").info(
            "Readable source ready", stage="Source", characters=12345, duration_seconds=1.25
        )
    line = capsys.readouterr().out.strip()
    assert "INFO" in line
    assert "[Source] [Story 2/30] Readable source ready" in line
    assert "characters: 12,345" in line
    assert "elapsed: 1.25s" in line
    assert "trace.id" not in line
    assert "service.name" not in line
    assert not line.startswith("{")


def test_story_context_does_not_leak_into_final_summary(capsys):
    configure_logging()
    logger = structlog.get_logger("hn_digest.example")
    with structlog.contextvars.bound_contextvars(story_index=1, story_total=2):
        logger.info("Processing story", stage="Story")
    logger.info("Digest completed successfully", stage="Run")
    lines = capsys.readouterr().out.splitlines()
    assert "[Story 1/2]" in lines[0]
    assert "[Story" not in lines[1]


def test_json_logging_remains_available(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging()
    structlog.get_logger("hn_digest.example").info("Source ready", stage="Source", characters=123)
    event = json.loads(capsys.readouterr().out)
    assert event["message"] == "Source ready"
    assert event["log.level"] == "info"
    assert event["service.name"] == "hackernews-epub-digest"
    assert event["trace.id"]
    assert event["characters"] == 123


def test_stdlib_library_logs_use_the_same_format_and_verbosity(capsys):
    configure_logging()
    logger = logging.getLogger("digest_test_library")
    previous = logger.level
    try:
        logger.setLevel(logging.DEBUG)
        logger.info("Library chatter")
        logger.warning("Library warning")
    finally:
        logger.setLevel(previous)
    output = capsys.readouterr().out
    assert "Library chatter" not in output
    assert "WARNING" in output and "Library warning" in output
    assert "trace.id" not in output


@pytest.mark.parametrize("log_format", ["text", "json"])
def test_credentials_are_redacted_from_messages_and_details(monkeypatch, capsys, log_format):
    monkeypatch.setenv("LOG_FORMAT", log_format)
    monkeypatch.setenv("GEMINI_API_KEY", "private-api-key")
    monkeypatch.setenv("SMTP_PASSWORD", "private-password")
    monkeypatch.setenv("PUBLISH_EMAIL", "private-reader@example.org")
    configure_logging()
    structlog.get_logger("hn_digest.example").warning(
        "Failure with private-api-key",
        reason="private-password",
        recipient="private-reader@example.org",
    )
    output = capsys.readouterr().out
    assert "private-api-key" not in output
    assert "private-password" not in output
    assert "private-reader@example.org" not in output
    assert "[REDACTED]" in output


def test_source_urls_hide_credentials_and_sensitive_query_parameters():
    result = safe_url(
        "https://name:password@example.org/article?id=123&api_key=secret-value&token=private#fragment"
    )
    assert "name" not in result and "password" not in result
    assert "secret-value" not in result and "private" not in result
    assert "id=123" in result and "example.org/article" in result


def test_untrusted_text_cannot_add_console_lines_or_ansi_commands(capsys):
    configure_logging()
    structlog.get_logger("hn_digest.example").info(
        "Processing story", stage="Story", title="Title\n::error::injected\x1b[31m"
    )
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert "\x1b" not in output


def test_debug_previews_are_opt_in(monkeypatch, capsys):
    configure_logging()
    logger = structlog.get_logger("hn_digest.example")
    logger.debug("Source text preview", preview="source preview")
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging()
    logger.debug("Source text preview", preview="source preview")
    assert "source preview" in capsys.readouterr().out


def test_invalid_format_has_a_clear_configuration_error(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "unknown")
    assert main(["--no-llm"]) == 1
    assert "LOG_FORMAT must be text or json" in capsys.readouterr().out


def test_actual_usage_includes_thinking_as_a_subset_without_inventing_missing_counts():
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=200,
            total_tokens=300,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=50),
        )
    )
    assert usage_counts(response) == {
        "prompt_tokens": 100,
        "completion_tokens": 200,
        "reported_tokens": 300,
        "thinking_tokens": 50,
    }
    assert all(value is None for value in usage_counts(SimpleNamespace()).values())


def test_http_retries_are_visible_and_hide_sensitive_urls(capsys):
    configure_logging()
    retry = fetching.LoggedRetry(total=2, backoff_factor=1)
    result = retry.increment(
        method="GET",
        url="/article?token=private",
        response=HTTPResponse(status=429, headers={"Retry-After": "3"}),
        _pool=SimpleNamespace(scheme="https", host="example.org"),
    )
    output = capsys.readouterr().out
    assert "HTTP request failed temporarily; retrying" in output
    assert "HTTP 429" in output and "wait: 3.00s" in output
    assert "private" not in output
    assert "https://example.org/article" in output
    assert result.total == 1


def test_complete_dry_run_logs_story_progress_usage_files_and_outcome(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("STORY_LIMIT", "2")
    import litellm

    stories = [
        fetching.Story(i, f"Article {i}", f"https://example.org/article/{i}") for i in (1, 2)
    ]
    monkeypatch.setattr(pipeline, "get_top_stories", lambda *args: stories)
    article = (
        "A database engine reduces latency by batching disk writes and preserving transactions. "
        * 12
    )
    monkeypatch.setattr(
        fetching,
        "download",
        lambda session, url, config: (
            f"<html><title>Database</title><article><p>{article}</p></article></html>".encode(),
            "text/html",
            url,
        ),
    )
    monkeypatch.setattr(litellm, "token_counter", MagicMock(return_value=1000))
    monkeypatch.setattr(
        litellm,
        "completion",
        MagicMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="A useful technical summary."),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=100,
                    completion_tokens=200,
                    total_tokens=300,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=50),
                ),
            )
        ),
    )
    email = MagicMock(side_effect=AssertionError("Preview must not send email"))
    monkeypatch.setattr(pipeline, "send_email", email)
    assert main(["--dry-run", "--output-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    for expected in (
        "Starting Hacker News digest",
        "[Story 1/2]",
        "[Story 2/2]",
        "Article 1",
        "https://example.org/article/1",
        "HTML article extracted",
        "Readable source ready",
        "Sending summary request",
        "input tokens: 100",
        "generated tokens: 200",
        "of which thinking tokens: 50",
        "reported total tokens: 300",
        "Summary ready",
        "EPUB saved",
        "Email skipped",
        "Digest completed successfully",
        "Run report saved",
    ):
        assert expected in output, expected
    report = json.loads((tmp_path / "run-report.json").read_text())
    assert len(report["stories"]) == 2
    assert report["stories"][0]["status"] == "summary"
    assert report["llm"]["reported_tokens"] == 600
    assert report["llm"]["prompt_tokens"] == 200
    assert report["llm"]["completion_tokens"] == 400
    assert report["llm"]["thinking_tokens"] == 100
    email.assert_not_called()


def test_failed_mail_delivery_is_explicit_and_keeps_credentials_out_of_logs(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("SMTP_USER", "private-user@example.org")
    monkeypatch.setenv("SMTP_PASSWORD", "private-password")
    monkeypatch.setenv("PUBLISH_EMAIL", "private-reader@example.org")
    monkeypatch.setenv("STORY_LIMIT", "1")
    monkeypatch.setattr(
        pipeline,
        "get_top_stories",
        lambda *args: [fetching.Story(1, "Title", "https://example.org")],
    )
    monkeypatch.setattr(pipeline, "fetch_article_text", lambda *args: "Source text. " * 50)
    monkeypatch.setattr(pipeline, "summarize_article", lambda *args: "Summary")
    monkeypatch.setattr(
        pipeline,
        "send_email",
        MagicMock(side_effect=smtplib.SMTPAuthenticationError(535, b"private-password")),
    )
    assert main(["--output-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "[Email] Stopping after a failed step" in output
    assert "SMTP authentication rejected" in output
    assert "Digest FAILED" in output and "email: not sent" in output
    assert "private-password" not in output
    assert "private-user@example.org" not in output
    assert "private-reader@example.org" not in output
