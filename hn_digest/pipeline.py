"""Run the digest and retain a report even if retrieval or delivery fails."""

import datetime as dt
import json
from pathlib import Path

import structlog

from hn_digest.config import Config
from hn_digest.fetching import FetchError, fetch_article_text, get_top_stories, make_session
from hn_digest.llm import ModelError, SummaryError, summarize_article
from hn_digest.publishing import Article, create_epub, send_email
from hn_digest.throttling import RateLimiter

log = structlog.get_logger(__name__)


def run_digest(config: Config, output_dir: Path, *, dry_run: bool, no_llm: bool) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "mode": "scraping_preview" if no_llm else "dry_run" if dry_run else "delivery",
        "requested": config.story_limit,
        "fetched": 0,
        "extracted": 0,
        "summarized": 0,
        "email_sent": False,
        "status": "failed",
        "errors": [],
    }
    limiter = None if no_llm else RateLimiter(config)
    stage = "hn_api"
    try:
        articles = []
        with make_session(config) as session:
            stories = get_top_stories(session, config)
            report["fetched"] = len(stories)
            log.info("Fetched HN stories", count=len(stories), requested=config.story_limit)
            for story in stories:
                stage = "scrape"
                log.info("Processing story", story_id=story.id, title=story.title)
                try:
                    text = fetch_article_text(session, story, config)
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, FetchError) else "Source extraction failed"
                    report["errors"].append(
                        {
                            "story_id": story.id,
                            "stage": "scrape",
                            "error_type": type(exc).__name__,
                            "reason": reason,
                        }
                    )
                    log.warning(
                        "Source unavailable",
                        story_id=story.id,
                        error_type=type(exc).__name__,
                        reason=reason,
                    )
                    articles.append(
                        Article(
                            story,
                            "Could not retrieve readable source content. "
                            "Use the source link to read it online.",
                            "unavailable",
                        )
                    )
                    continue
                report["extracted"] += 1
                log.info("Extracted source", story_id=story.id, characters=len(text))
                if no_llm:
                    articles.append(Article(story, text, "excerpt"))
                    continue
                stage = "summary"
                try:
                    summary = summarize_article(text, story.title, story.url, config, limiter)
                except SummaryError as exc:
                    report["errors"].append(
                        {"story_id": story.id, "stage": "summary", "error_type": type(exc).__name__}
                    )
                    log.warning("Summary unavailable", story_id=story.id, reason=str(exc))
                    articles.append(
                        Article(
                            story,
                            "AI summary unavailable for this story. "
                            "Use the source link to read it online.",
                            "unavailable",
                        )
                    )
                    continue
                report["summarized"] += 1
                articles.append(Article(story, summary, "summary"))
        if not (report["extracted"] if no_llm else report["summarized"]):
            raise RuntimeError("No usable stories: check the source and provider errors")
        stage = "epub"
        path = create_epub(articles, output_dir, config, preview=no_llm)
        report["epub"] = path.name
        if not (dry_run or no_llm):
            stage = "smtp"
            send_email(path, config)
            report["email_sent"] = True
        summary_failed = any(error["stage"] == "summary" for error in report["errors"])
        report["status"] = (
            "partial" if report["errors"] or len(stories) < config.story_limit else "success"
        )
        return 1 if summary_failed else 0
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        report["failure_stage"] = stage
        if isinstance(exc, ModelError):
            report["failure_reason"] = str(exc)
        raise
    finally:
        if limiter is not None:
            report["llm"] = {
                "requests_sent": limiter.requests_sent,
                "rate_limits_seen": limiter.rate_limits_seen,
                "wait_seconds": round(limiter.wait_seconds, 2),
                "reported_tokens": limiter.reported_tokens,
            }
        report["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
        (output_dir / "run-report.json").write_text(json.dumps(report, indent=2) + "\n")
        log.info(
            "Run report saved",
            status=report["status"],
            extracted=report["extracted"],
            summarized=report["summarized"],
            email_sent=report["email_sent"],
        )
