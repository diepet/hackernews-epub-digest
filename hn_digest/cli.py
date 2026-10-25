"""Command line entry point and structured logging."""

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path

import structlog

from hn_digest.config import Config
from hn_digest.llm import ModelError
from hn_digest.pipeline import run_digest


def configure_logging() -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        **{
            "service.name": "hackernews-epub-digest",
            "job.name": "daily-digest",
            "trace.id": uuid.uuid4().hex,
        }
    )

    def fields(logger, method_name, event_dict):
        event_dict["message"] = event_dict.pop("event", "")
        event_dict["log.level"] = event_dict.pop("level", method_name)
        return event_dict

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", key="@timestamp"),
        fields,
    ]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(
        getattr(logging, os.environ.get("ROOT_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    )
    logging.getLogger("hn_digest").setLevel(
        getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and email a Hacker News EPUB digest.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Use AI and write an EPUB without email"
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="Preview source excerpts without AI or email"
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    args = parser.parse_args(argv)
    configure_logging()
    log = structlog.get_logger(__name__)
    if os.environ.get("LLM_REQUEST_DELAY", "").strip() not in {"", "0"}:
        log.warning(
            "LLM_REQUEST_DELAY has been replaced; configure LLM_RPM_LIMIT and LLM_TPM_LIMIT"
        )
    try:
        config = Config.from_env()
        config.validate(dry_run=args.dry_run, no_llm=args.no_llm)
    except ValueError as exc:
        log.error("Invalid configuration", reason=str(exc))
        return 1
    try:
        return run_digest(config, args.output_dir, dry_run=args.dry_run, no_llm=args.no_llm)
    except ModelError as exc:
        log.error("LLM configuration or quota failure", reason=str(exc))
        return 1
    except Exception as exc:
        log.error(
            "Digest failed",
            error_type=type(exc).__name__,
            action="Check configuration, provider access, SMTP settings, and run-report.json",
        )
        return 1
