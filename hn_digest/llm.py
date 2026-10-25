"""Model-aware input estimates and bounded provider-independent retries."""

import math
import os
import random

import structlog

from hn_digest.config import Config
from hn_digest.throttling import (
    RateLimiter,
    ThrottleError,
    permanent_quota_error,
    retry_delay,
)

log = structlog.get_logger(__name__)


class SummaryError(RuntimeError):
    """A transient or content-specific generation failure."""


class ModelError(RuntimeError):
    """A provider/model configuration failure that should stop the run."""


def article_messages(text: str, title: str, url: str, config: Config) -> list[dict[str, str]]:
    # Replace only the documented placeholder; leave JSON/code braces in custom prompts intact.
    system_prompt = config.llm_system_prompt.replace("{target_language}", config.target_language)
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Title: {title}\nSource: {url}\nTarget language: {config.target_language}\n"
                f"<article>\n{text}\n</article>"
            ),
        },
    ]


def estimate_tokens(messages: list[dict[str, str]], config: Config) -> int:
    from litellm import token_counter

    try:
        tokens = token_counter(model=config.llm_model, messages=messages)
        if not isinstance(tokens, int) or tokens <= 0:
            raise ValueError("Invalid token estimate")
    except Exception:
        # Counting must not prevent generation for custom model/deployment names.
        # UTF-8 byte length is deliberately conservative for ordinary text tokenizers.
        log.warning("Model tokenizer unavailable; estimating from UTF-8 bytes")
        tokens = sum(len(message["content"].encode("utf-8")) + 16 for message in messages)
    return math.ceil(tokens * 1.15)


def prepare_request(
    text: str, title: str, url: str, config: Config
) -> tuple[list[dict[str, str]], int]:
    messages = article_messages(text, title, url, config)
    if not config.llm_tpm_limit:
        return messages, 0
    reserved = estimate_tokens(messages, config) + config.llm_max_output_tokens
    if reserved <= config.llm_tpm_limit:
        return messages, reserved
    # Waiting cannot fix a single request larger than the entire minute budget.
    # Find a source excerpt that fits, including prompt overhead and output allowance.
    low, high = 0, len(text)
    best = None
    while low <= high:
        middle = (low + high) // 2
        candidate = article_messages(text[:middle], title, url, config)
        tokens = estimate_tokens(candidate, config) + config.llm_max_output_tokens
        if tokens <= config.llm_tpm_limit:
            best = candidate, tokens, middle
            low = middle + 1
        else:
            high = middle - 1
    if best is None or best[2] < min(200, len(text)):
        raise ModelError("LLM_TPM_LIMIT is too small: increase it or lower LLM_MAX_OUTPUT_TOKENS")
    log.info(
        "Trimmed source to fit LLM_TPM_LIMIT",
        original_characters=len(text),
        sent_characters=best[2],
        reserved_tokens=best[1],
    )
    return best[0], best[1]


def summarize_article(
    text: str, title: str, url: str, config: Config, limiter: RateLimiter | None = None
) -> str:
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from litellm import (
        APIConnectionError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
        ServiceUnavailableError,
        Timeout,
        completion,
    )

    limiter = limiter or RateLimiter(config)
    messages, reserved = prepare_request(text, title, url, config)
    for attempt in range(config.max_retries + 1):
        try:
            reservation = limiter.acquire(reserved)
            response = completion(
                model=config.llm_model,
                messages=messages,
                timeout=config.llm_timeout,
                num_retries=0,
                max_retries=0,
                max_tokens=config.llm_max_output_tokens,
            )
            usage = getattr(response, "usage", None)
            total_tokens = getattr(usage, "total_tokens", None)
            limiter.record_usage(reservation, total_tokens)
            log.info(
                "LLM response received", reserved_tokens=reserved, reported_tokens=total_tokens
            )
            choice = response.choices[0]
            result = choice.message.content
            if not isinstance(result, str) or not result.strip():
                raise SummaryError("Provider returned an empty summary")
            if choice.finish_reason != "stop":
                raise SummaryError("Provider returned an incomplete or blocked summary")
            return result.strip()
        except (AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError) as exc:
            raise ModelError("Check LLM_MODEL, the provider API key, and model access") from exc
        except RateLimitError as exc:
            limiter.rate_limits_seen += 1
            if permanent_quota_error(exc):
                raise ModelError("Provider daily quota or billing budget exhausted") from exc
            delay = retry_delay(exc, attempt)
            try:
                limiter.defer(delay)
            except ThrottleError as wait_error:
                raise ModelError(str(wait_error)) from exc
            if attempt == config.max_retries:
                raise SummaryError("Provider rate limit persisted after bounded retries") from exc
            log.warning(
                "Provider rate limit; retrying after cooldown",
                seconds=round(delay, 2),
                retry=attempt + 1,
            )
        except (APIConnectionError, InternalServerError, ServiceUnavailableError, Timeout) as exc:
            if attempt == config.max_retries:
                raise SummaryError(f"LLM request failed ({type(exc).__name__})") from exc
            limiter.defer(2**attempt + random.uniform(0, 1))
            log.warning(
                "Transient LLM failure; retrying", error_type=type(exc).__name__, retry=attempt + 1
            )
        except ThrottleError as exc:
            raise ModelError(str(exc)) from exc
        except SummaryError:
            raise
        except Exception as exc:
            raise SummaryError(f"LLM request failed ({type(exc).__name__})") from exc
    raise AssertionError("Unreachable retry state")
