"""Readable console progress with optional JSON output and shared story context."""

import logging
import os
import re
import sys
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PRIVATE_QUERY = re.compile(r"token|key|password|secret|signature|credential|authorization", re.I)
_META = {
    "@timestamp",
    "log.level",
    "message",
    "service.name",
    "job.name",
    "trace.id",
    "stage",
    "story_index",
    "story_total",
}
_LABELS = {
    "story_id": "HN ID",
    "title": "title",
    "url": "URL",
    "path": "file",
    "filename": "file",
    "characters": "characters",
    "words": "words",
    "extracted_characters": "extracted characters",
    "kept_characters": "characters kept",
    "original_characters": "original characters",
    "sent_characters": "characters sent",
    "reserved_tokens": "reserved token budget",
    "reported_tokens": "reported total tokens",
    "prompt_tokens": "input tokens",
    "completion_tokens": "generated tokens",
    "thinking_tokens": "of which thinking tokens",
    "error_type": "error",
    "content_type": "content type",
    "requested": "requested stories",
    "fetched": "fetched",
    "extracted": "readable sources",
    "summarized": "summaries",
    "failed_sources": "unreadable sources",
    "failed_summaries": "failed summaries",
    "requests_sent": "LLM attempts",
    "rate_limits_seen": "rate limits",
    "wait_seconds": "time waiting",
    "duration_seconds": "elapsed",
    "seconds": "wait",
    "size_bytes": "size",
    "max_output_tokens": "maximum output tokens",
    "rpm_limit": "requests/minute limit",
    "tpm_limit": "tokens/minute limit",
    "char_limit": "source character limit",
    "download_limit_bytes": "download limit",
    "timeout_seconds": "timeout",
    "email_status": "email",
    "max_attempts": "maximum attempts",
}


def safe_url(url: str) -> str:
    """Keep public source links useful while hiding URL credentials and secret query values."""
    try:
        parsed = urlsplit(url)
        host = parsed.netloc.rsplit("@", 1)[-1]
        query = urlencode(
            [
                (key, "[REDACTED]" if _PRIVATE_QUERY.search(key) else value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except ValueError:
        return "[invalid URL]"


def _one_line(value: object) -> str:
    return _CONTROL.sub(" ", str(value)).strip()


def _display(key: str, value: object) -> str:
    if value is None:
        return "not reported"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key == "mode":
        return {
            "scraping_preview": "source preview (no AI/email)",
            "dry_run": "AI preview (no email)",
        }.get(value, _one_line(value))
    if isinstance(value, (int, float)) and key in {
        "duration_seconds",
        "wait_seconds",
        "seconds",
        "timeout_seconds",
    }:
        return f"{value:,.2f}s"
    if isinstance(value, int):
        return f"{value:,}" + (" bytes" if key.endswith("_bytes") else "")
    if isinstance(value, float):
        return f"{value:,.2f}"
    return _one_line(value)


def render_text(logger, method_name: str, event: dict) -> str:
    timestamp = str(event.get("@timestamp", ""))[11:19]
    level = str(event.get("log.level", method_name)).upper()
    stage = _one_line(event.get("stage", "App"))
    story = ""
    if event.get("story_index") is not None:
        story = f" [Story {event['story_index']}/{event['story_total']}]"
    details = []
    for key, value in event.items():
        if key in _META or (key == "story_id" and story):
            continue
        details.append(f"{_LABELS.get(key, key.replace('_', ' '))}: {_display(key, value)}")
    suffix = " | " + " | ".join(details) if details else ""
    scope = story.strip() if stage == "Story" and story else f"[{stage}]{story}"
    return f"{timestamp} {level:<7} {scope} {_one_line(event.get('message', ''))}{suffix}"


def configure_logging() -> None:
    log_format = os.environ.get("LOG_FORMAT", "text").strip().lower() or "text"
    if log_format not in {"text", "json"}:
        raise ValueError("LOG_FORMAT must be text or json")
    app_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    library_level = getattr(
        logging, os.environ.get("ROOT_LOG_LEVEL", "WARNING").upper(), logging.WARNING
    )
    secrets = tuple(
        value
        for name, value in os.environ.items()
        if value
        and (
            re.search(r"(?:API_KEY|PASSWORD|SECRET|ACCESS_TOKEN)$", name)
            or name in {"SMTP_USER", "SMTP_SENDER_EMAIL", "PUBLISH_EMAIL"}
        )
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        **{
            "service.name": "hackernews-epub-digest",
            "job.name": "daily-digest",
            "trace.id": uuid.uuid4().hex,
        }
    )

    def fields(logger, method_name, event):
        event["message"] = event.pop("event", "")
        event["log.level"] = event.pop("level", method_name)
        return event

    def redact(logger, method_name, event):
        def clean(value):
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[REDACTED]")
                return value
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            return value

        return {key: clean(value) for key, value in event.items()}

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", key="@timestamp"),
        fields,
    ]
    handler = logging.StreamHandler(sys.stdout)
    # Some libraries set their own INFO level, so filtering only the root logger is insufficient.
    handler.addFilter(
        lambda record: (
            record.name.startswith("hn_digest.")
            or record.name == "hn_digest"
            or record.levelno >= library_level
        )
    )
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                redact,
                structlog.processors.JSONRenderer() if log_format == "json" else render_text,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(library_level)
    logging.getLogger("hn_digest").setLevel(app_level)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
