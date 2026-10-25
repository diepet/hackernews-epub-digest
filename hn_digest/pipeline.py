"""Run the digest with visible progress and a report retained on failure."""

import datetime as dt
import json
import smtplib
import time
from pathlib import Path

import requests
import structlog

from hn_digest.config import DEFAULT_SYSTEM_PROMPT, Config
from hn_digest.fetching import FetchError, fetch_article_text, get_top_stories, make_session
from hn_digest.llm import ModelError, SummaryError, summarize_article
from hn_digest.logging_config import safe_url
from hn_digest.publishing import Article, create_epub, send_email, summary_body_word_count
from hn_digest.throttling import RateLimiter

log = structlog.get_logger(__name__)
STAGES = {"hn_api": "HN", "scrape": "Source", "summary": "LLM", "epub": "EPUB", "smtp": "Email"}


def failure_reason(error: Exception) -> str:
    """Describe failures without exposing provider response bodies or mail credentials."""
    if isinstance(error, (FetchError, SummaryError, ModelError)):
        return str(error)
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "SMTP authentication rejected; check SMTP_USER and SMTP_PASSWORD/app password"
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "SMTP recipient rejected; check PUBLISH_EMAIL and the allowed sender settings"
    if isinstance(error, smtplib.SMTPException):
        return f"SMTP operation failed ({type(error).__name__}); check the mail provider settings"
    if isinstance(error, requests.HTTPError):
        status = error.response.status_code if error.response is not None else "unknown"
        return f"HTTP {status} from the source service"
    if isinstance(error, requests.RequestException):
        return (
            f"Network request failed ({type(error).__name__}); check connectivity and certificates"
        )
    if isinstance(error, OSError):
        return f"I/O operation failed ({type(error).__name__}); check connection and output permissions"
    return f"Operation failed ({type(error).__name__}); inspect the failed stage"


def run_digest(config: Config, output_dir: Path, *, dry_run: bool, no_llm: bool) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    mode = "scraping_preview" if no_llm else "dry_run" if dry_run else "delivery"
    report = {
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "mode": mode,
        "requested": config.story_limit,
        "fetched": 0,
        "extracted": 0,
        "summarized": 0,
        "email_sent": False,
        "status": "failed",
        "errors": [],
        "stories": [],
    }
    log.info(
        "Starting Hacker News digest",
        stage="Run",
        mode=mode,
        requested=config.story_limit,
        language=config.target_language,
    )
    log.info(
        "Source settings",
        stage="Config",
        char_limit=config.scrape_char_limit,
        pdf_page_limit=config.pdf_page_limit,
        download_limit_bytes=config.max_download_bytes,
        timeout_seconds=config.http_timeout,
        max_attempts=config.max_retries + 1,
        output_directory=str(output_dir),
    )
    if not no_llm:
        log.info(
            "AI settings",
            stage="Config",
            model=config.llm_model,
            rpm_limit=config.llm_rpm_limit or "disabled",
            tpm_limit=config.llm_tpm_limit or "disabled",
            max_output_tokens=config.llm_max_output_tokens,
            timeout_seconds=config.llm_timeout,
            prompt="built-in" if config.llm_system_prompt == DEFAULT_SYSTEM_PROMPT else "custom",
            prompt_characters=len(config.llm_system_prompt),
        )
    limiter = None if no_llm else RateLimiter(config)
    stage = "hn_api"
    try:
        articles = []
        with make_session(config) as session:
            stories = get_top_stories(session, config)
            report["fetched"] = len(stories)
            log.info(
                "Story selection complete",
                stage="HN",
                fetched=len(stories),
                requested=config.story_limit,
            )
            if len(stories) < config.story_limit:
                log.warning(
                    "Fewer usable stories than requested",
                    stage="HN",
                    missing=config.story_limit - len(stories),
                )
            for index, story in enumerate(stories, start=1):
                with structlog.contextvars.bound_contextvars(
                    story_index=index, story_total=len(stories), story_id=story.id
                ):
                    item = {
                        "story_id": story.id,
                        "title": story.title,
                        "url": safe_url(story.url),
                        "status": "pending",
                    }
                    report["stories"].append(item)
                    stage = "scrape"
                    log.info("Processing story", stage="Story", title=story.title)
                    log.info(
                        "Source link",
                        stage="Source",
                        url=safe_url(story.url),
                        source_type="HN self post" if story.is_self_post else "external article",
                    )
                    source_started = time.monotonic()
                    try:
                        text = fetch_article_text(session, story, config)
                    except Exception as exc:
                        reason = failure_reason(exc)
                        item.update(status="source_unavailable", reason=reason)
                        report["errors"].append(
                            {
                                "story_id": story.id,
                                "stage": "scrape",
                                "error_type": type(exc).__name__,
                                "reason": reason,
                            }
                        )
                        log.warning(
                            "Source unavailable; adding story to Other Articles",
                            stage="Source",
                            error_type=type(exc).__name__,
                            reason=reason,
                        )
                        articles.append(Article(story, "", "unavailable"))
                        continue
                    report["extracted"] += 1
                    item.update(
                        source_characters=len(text),
                        source_words=len(text.split()),
                        source_seconds=round(time.monotonic() - source_started, 2),
                    )
                    log.info(
                        "Readable source ready",
                        stage="Source",
                        characters=len(text),
                        words=item["source_words"],
                        duration_seconds=item["source_seconds"],
                    )
                    log.debug("Source text preview", stage="Source", preview=text[:400])
                    if no_llm:
                        item["status"] = "excerpt"
                        articles.append(Article(story, text, "excerpt"))
                        continue
                    stage = "summary"
                    summary_started = time.monotonic()
                    try:
                        summary = summarize_article(text, story.title, story.url, config, limiter)
                    except SummaryError as exc:
                        reason = str(exc)
                        item.update(status="summary_unavailable", reason=reason)
                        report["errors"].append(
                            {
                                "story_id": story.id,
                                "stage": "summary",
                                "error_type": type(exc).__name__,
                                "reason": reason,
                            }
                        )
                        log.warning(
                            "Summary unavailable; adding story to Other Articles",
                            stage="LLM",
                            reason=reason,
                        )
                        articles.append(Article(story, "", "unavailable"))
                        continue
                    except ModelError as exc:
                        item.update(status="summary_unavailable", reason=str(exc))
                        raise
                    report["summarized"] += 1
                    item.update(
                        status="summary",
                        summary_characters=len(summary),
                        summary_words=len(summary.split()),
                        summary_seconds=round(time.monotonic() - summary_started, 2),
                    )
                    if config.llm_system_prompt == DEFAULT_SYSTEM_PROMPT:
                        body_words = summary_body_word_count(summary)
                        warnings = []
                        if body_words > 250:
                            warnings.append("Summary exceeds the default prompt's 250-word maximum")
                        if len(text) < 500 and body_words > 120:
                            warnings.append(
                                "Very short source expanded beyond the prompt's short-source range; "
                                "review for unsupported detail"
                            )
                        item.update(summary_body_words=body_words, quality_warnings=warnings)
                        for warning in warnings:
                            log.warning(
                                "Summary needs editorial review",
                                stage="LLM",
                                reason=warning,
                                summary_body_words=body_words,
                                source_words=item["source_words"],
                            )
                    log.info(
                        "Summary ready",
                        stage="LLM",
                        characters=len(summary),
                        words=item["summary_words"],
                        duration_seconds=item["summary_seconds"],
                    )
                    log.debug("Summary text preview", stage="LLM", preview=summary[:600])
                    articles.append(Article(story, summary, "summary"))
        if not (report["extracted"] if no_llm else report["summarized"]):
            raise FetchError("No usable stories: check the source and provider errors")
        stage = "epub"
        path = create_epub(articles, output_dir, config, preview=no_llm)
        report["epub"] = path.name
        if not (dry_run or no_llm):
            stage = "smtp"
            send_email(path, config)
            report["email_sent"] = True
        else:
            log.info("Email skipped for this preview run", stage="Email", mode=mode)
        summary_failed = any(error["stage"] == "summary" for error in report["errors"])
        report["status"] = (
            "partial" if report["errors"] or len(stories) < config.story_limit else "success"
        )
        return 1 if summary_failed else 0
    except Exception as exc:
        report.update(
            failure_type=type(exc).__name__, failure_stage=stage, failure_reason=failure_reason(exc)
        )
        log.error(
            "Stopping after a failed step",
            stage=STAGES[stage],
            error_type=type(exc).__name__,
            reason=report["failure_reason"],
        )
        raise
    finally:
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        report["quality_warnings"] = sum(
            bool(item.get("quality_warnings")) for item in report["stories"]
        )
        if limiter is not None:
            report["llm"] = {
                "requests_sent": limiter.requests_sent,
                "rate_limits_seen": limiter.rate_limits_seen,
                "wait_seconds": round(limiter.wait_seconds, 2),
                **{
                    key: getattr(limiter, key) if limiter.usage_counts_reported[key] else None
                    for key in (
                        "reported_tokens",
                        "prompt_tokens",
                        "completion_tokens",
                        "thinking_tokens",
                    )
                },
            }
            log.info("LLM totals", stage="LLM", **report["llm"])
        report["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
        report_path = output_dir / "run-report.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        summary_fields = {
            "stage": "Run",
            "requested": report["requested"],
            "fetched": report["fetched"],
            "extracted": report["extracted"],
            "summarized": report["summarized"],
            "failed_sources": sum(error["stage"] == "scrape" for error in report["errors"]),
            "failed_summaries": sum(
                item["status"] == "summary_unavailable" for item in report["stories"]
            ),
            "quality_warnings": report["quality_warnings"],
            "duration_seconds": report["duration_seconds"],
            "email_status": "accepted by SMTP"
            if report["email_sent"]
            else "skipped (preview)"
            if dry_run or no_llm
            else "not sent",
        }
        if report["status"] == "failed":
            log.error("Digest FAILED", **summary_fields)
        elif report["status"] == "partial":
            log.warning("Digest completed with unavailable stories", **summary_fields)
        else:
            log.info("Digest completed successfully", **summary_fields)
        log.info("Run report saved", stage="Report", path=str(report_path))
