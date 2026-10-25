"""Command line entry point for digest generation and delivery."""

import argparse
import os
from pathlib import Path

import structlog

from hn_digest.config import Config
from hn_digest.llm import ModelError
from hn_digest.logging_config import configure_logging
from hn_digest.pipeline import run_digest


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
    try:
        configure_logging()
    except ValueError as exc:
        print(f"ERROR [Config] {exc}", flush=True)
        return 1
    log = structlog.get_logger(__name__)
    if os.environ.get("LLM_REQUEST_DELAY", "").strip() not in {"", "0"}:
        log.warning(
            "LLM_REQUEST_DELAY has been replaced; configure LLM_RPM_LIMIT and LLM_TPM_LIMIT"
        )
    try:
        config = Config.from_env()
        config.validate(dry_run=args.dry_run, no_llm=args.no_llm)
    except ValueError as exc:
        log.error("Invalid configuration", stage="Config", reason=str(exc))
        return 1
    try:
        return run_digest(config, args.output_dir, dry_run=args.dry_run, no_llm=args.no_llm)
    except ModelError as exc:
        log.error("LLM configuration or quota failure", stage="LLM", reason=str(exc))
        return 1
    except Exception as exc:
        log.error(
            "Digest failed",
            stage="Run",
            error_type=type(exc).__name__,
            action="Check configuration, provider access, SMTP settings, and run-report.json",
        )
        return 1
