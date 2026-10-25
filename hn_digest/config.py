"""Parse configuration when a run starts, without import-time side effects."""

import math
import os
from dataclasses import dataclass, field

import langcodes


def number(name: str, default: int | float, minimum: float, maximum: float) -> int | float:
    raw = os.environ.get(name, "").strip()
    try:
        value = type(default)(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


@dataclass(frozen=True)
class Config:
    llm_model: str = ""
    target_language: str = "English"
    language_code: str = "en"
    story_limit: int = 10
    scrape_char_limit: int = 60000
    llm_rpm_limit: int = 0
    llm_tpm_limit: int = 0
    llm_max_output_tokens: int = 1800
    llm_max_wait_seconds: float = 300.0
    http_timeout: float = 20.0
    llm_timeout: float = 90.0
    smtp_timeout: float = 30.0
    max_retries: int = 2
    max_download_bytes: int = 10_000_000
    pdf_page_limit: int = 30
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_user: str = field(default="", repr=False)
    smtp_password: str = field(default="", repr=False)
    smtp_sender_email: str = field(default="", repr=False)
    publish_email: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "Config":
        language = os.environ.get("TARGET_LANGUAGE", "English").strip()
        try:
            code = (
                langcodes.get(language)
                if langcodes.tag_is_valid(language)
                else langcodes.find(language)
            ).to_tag()
        except (LookupError, ValueError) as exc:
            raise ValueError("TARGET_LANGUAGE must be a language name or language tag") from exc
        if code == "und":
            raise ValueError("TARGET_LANGUAGE must identify a language")
        return cls(
            llm_model=os.environ.get("LLM_MODEL", "").strip(),
            target_language=language,
            language_code=code,
            story_limit=number("STORY_LIMIT", 10, 1, 50),
            scrape_char_limit=number("SCRAPE_CHAR_LIMIT", 60000, 300, 500000),
            llm_rpm_limit=number("LLM_RPM_LIMIT", 0, 0, 1_000_000),
            llm_tpm_limit=number("LLM_TPM_LIMIT", 0, 0, 1_000_000_000),
            llm_max_output_tokens=number("LLM_MAX_OUTPUT_TOKENS", 1800, 100, 32000),
            llm_max_wait_seconds=number("LLM_MAX_WAIT_SECONDS", 300.0, 60, 600),
            http_timeout=number("HTTP_TIMEOUT", 20.0, 1, 120),
            llm_timeout=number("LLM_TIMEOUT", 90.0, 1, 300),
            smtp_timeout=number("SMTP_TIMEOUT", 30.0, 1, 120),
            max_retries=number("MAX_RETRIES", 2, 0, 5),
            max_download_bytes=number("MAX_DOWNLOAD_BYTES", 10_000_000, 1024, 50_000_000),
            pdf_page_limit=number("PDF_PAGE_LIMIT", 30, 1, 100),
            smtp_host=os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com",
            smtp_port=number("SMTP_PORT", 465, 1, 65535),
            smtp_user=os.environ.get("SMTP_USER", "").strip(),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_sender_email=os.environ.get("SMTP_SENDER_EMAIL", "").strip(),
            publish_email=os.environ.get("PUBLISH_EMAIL", "").strip(),
        )

    def validate(self, *, dry_run: bool, no_llm: bool) -> None:
        required = []
        if not no_llm and not self.llm_model:
            required.append("LLM_MODEL")
        if not dry_run and not no_llm:
            required.extend(
                name
                for name, value in (
                    ("SMTP_USER", self.smtp_user),
                    ("SMTP_PASSWORD", self.smtp_password),
                    ("PUBLISH_EMAIL", self.publish_email),
                )
                if not value
            )
        if required:
            raise ValueError("Missing configuration: " + ", ".join(required))
