"""Rate limits for a single sequential digest, with a rolling 60-second window."""

import datetime as dt
import math
import re
import time
from collections import deque
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import structlog

from hn_digest.config import Config

log = structlog.get_logger(__name__)
WINDOW_SECONDS = 60.0


class ThrottleError(RuntimeError):
    """A request cannot fit the configured budget or wait bound."""


@dataclass
class Reservation:
    sent_at: float
    tokens: int


class RateLimiter:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.reservations: deque[Reservation] = deque()
        self.cooldown_until = 0.0
        self.requests_sent = 0
        self.rate_limits_seen = 0
        self.wait_seconds = 0.0
        self.reported_tokens = 0

    def defer(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds > self.config.llm_max_wait_seconds:
            raise ThrottleError("Provider cooldown exceeds LLM_MAX_WAIT_SECONDS")
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + max(0, seconds))

    def acquire(self, tokens: int) -> Reservation:
        if self.config.llm_tpm_limit and tokens > self.config.llm_tpm_limit:
            raise ThrottleError("A single request exceeds LLM_TPM_LIMIT")
        while True:
            now = time.monotonic()
            while self.reservations and self.reservations[0].sent_at + WINDOW_SECONDS <= now:
                self.reservations.popleft()
            request_limit = self.config.llm_rpm_limit
            token_limit = self.config.llm_tpm_limit
            full = (request_limit and len(self.reservations) >= request_limit) or (
                token_limit and sum(r.tokens for r in self.reservations) + tokens > token_limit
            )
            wait = max(0.0, self.cooldown_until - now)
            if full:
                wait = max(wait, self.reservations[0].sent_at + WINDOW_SECONDS - now)
            if wait <= 0:
                reservation = Reservation(now, tokens)
                self.reservations.append(reservation)
                self.requests_sent += 1
                return reservation
            log.info("Waiting for LLM rate budget", seconds=round(wait, 2))
            # Keep individual sleeps short and emit progress during long provider cooldowns.
            pause = min(wait, 30.0)
            time.sleep(pause)
            self.wait_seconds += pause

    def record_usage(self, reservation: Reservation, total_tokens: object) -> None:
        if (
            isinstance(total_tokens, int)
            and not isinstance(total_tokens, bool)
            and total_tokens > 0
        ):
            self.reported_tokens += total_tokens
            # Some providers charge the reserved output allowance to their rate limit.
            # Keep that reservation even when actual output was shorter.
            reservation.tokens = max(reservation.tokens, total_tokens)


def duration(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value.strip())
        if not parts or "".join(number + unit for number, unit in parts) != value.strip():
            return None
        seconds = sum(
            float(number) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
            for number, unit in parts
        )
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def error_body(error: Exception) -> dict:
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        try:
            body = error.response.json()
        except (AttributeError, ValueError):
            return {}
    if not isinstance(body, dict):
        return {}
    return body.get("error", body) if isinstance(body.get("error", body), dict) else {}


def permanent_quota_error(error: Exception) -> bool:
    body = error_body(error)
    if getattr(error, "rate_limit_type", None) == "budget":
        return True
    if body.get("code") in ("insufficient_quota", "billing_hard_limit_reached"):
        return True
    # Google quota failures can identify a daily limit separately from RetryInfo.
    for detail in body.get("details", []) if isinstance(body.get("details"), list) else []:
        if not isinstance(detail, dict):
            continue
        for violation in (
            detail.get("violations", []) if isinstance(detail.get("violations"), list) else []
        ):
            if isinstance(violation, dict):
                quota = str(violation.get("quotaId", "")).lower().replace("_", "")
                if "perday" in quota:
                    return True
    return False


def retry_delay(error: Exception, attempt: int) -> float:
    """Use provider hints when preserved by LiteLLM, otherwise exponential minute backoff."""
    response = getattr(error, "response", None)
    headers = dict(getattr(response, "headers", {}) or {})
    headers.update(getattr(error, "headers", {}) or {})
    headers = {str(key).lower(): str(value) for key, value in headers.items()}
    hints = []
    for key, scale in (("retry-after", 1), ("retry-after-ms", 0.001)):
        value = headers.get(key, "")
        seconds = duration(value)
        if seconds is None and key == "retry-after":
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is not None:
                    seconds = max(0, date.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
        if seconds is not None:
            hints.append(seconds * scale)
    if not hints:
        for key in (
            "x-ratelimit-reset-requests",
            "x-ratelimit-reset-tokens",
            "anthropic-ratelimit-requests-reset",
            "anthropic-ratelimit-tokens-reset",
        ):
            value = headers.get(key, "")
            seconds = duration(value)
            if seconds is not None and seconds >= 1_000_000_000:
                seconds = max(0, seconds - time.time())
            if seconds is None:
                try:
                    date = dt.datetime.fromisoformat(value)
                    if date.tzinfo is not None:
                        seconds = max(0, date.timestamp() - time.time())
                except ValueError:
                    pass
            if seconds is not None:
                hints.append(seconds)
        body = error_body(error)
        for detail in body.get("details", []) if isinstance(body.get("details"), list) else []:
            if isinstance(detail, dict) and str(detail.get("@type", "")).endswith(
                "google.rpc.RetryInfo"
            ):
                seconds = duration(detail.get("retryDelay"))
                if seconds is not None:
                    hints.append(seconds)
    return max(1.0, max(hints)) if hints else 60.0 * 2**attempt
