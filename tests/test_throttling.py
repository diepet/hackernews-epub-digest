import datetime as dt
import json
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hn_digest import llm, throttling
from hn_digest.config import Config
from hn_digest.throttling import RateLimiter, ThrottleError, retry_delay


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0
        sleeps = []

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(
        throttling,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.now, time=lambda: 1_700_000_000 + clock.now, sleep=clock.sleep
        ),
    )
    return clock


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    return litellm


def response(tokens=100):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="A useful summary."), finish_reason="stop"
            )
        ],
        usage=SimpleNamespace(total_tokens=tokens),
    )


def rate_error(sdk, *, headers=None, body=None):
    import httpx

    return sdk.RateLimitError(
        "rate limited",
        llm_provider="gemini",
        model="test",
        response=httpx.Response(429, headers=headers, json=body or {}),
    )


def test_no_local_limits_needs_no_pause(clock):
    limiter = RateLimiter(Config())
    for _ in range(10):
        limiter.acquire(100)
    assert clock.sleeps == []


def test_rpm_counts_requests_in_rolling_window(clock):
    limiter = RateLimiter(Config(llm_rpm_limit=2))
    limiter.acquire(0)
    limiter.acquire(0)
    limiter.acquire(0)
    assert clock.now == 60
    assert sum(clock.sleeps) == 60
    assert max(clock.sleeps) <= 30


def test_tpm_releases_only_expired_reservations(clock):
    limiter = RateLimiter(Config(llm_tpm_limit=500))
    limiter.acquire(200)
    clock.now = 10
    limiter.acquire(200)
    limiter.acquire(200)
    assert clock.now == 60
    assert sum(r.tokens for r in limiter.reservations) == 400


def test_actual_usage_can_increase_but_not_release_reserved_tokens(clock):
    limiter = RateLimiter(Config(llm_tpm_limit=1000))
    first = limiter.acquire(400)
    limiter.record_usage(first, 100)
    assert first.tokens == 400
    limiter.record_usage(first, 800)
    limiter.acquire(300)
    assert clock.now == 60


def test_impossible_request_fails_without_waiting(clock):
    with pytest.raises(ThrottleError, match="single request"):
        RateLimiter(Config(llm_tpm_limit=500)).acquire(501)
    assert not clock.sleeps


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "12"}, 12),
        ({"retry-after-ms": "2500"}, 2.5),
        ({"x-ratelimit-reset-tokens": "1m2s"}, 62),
        ({"retry-after": "nan"}, 60),
        ({"retry-after": "nonsense"}, 60),
    ],
)
def test_rate_limit_headers(sdk, clock, headers, expected):
    assert retry_delay(rate_error(sdk, headers=headers), 0) == expected


def test_retry_after_http_date(sdk, clock):
    date = dt.datetime.fromtimestamp(1_700_000_025, dt.UTC)
    error = rate_error(sdk, headers={"Retry-After": format_datetime(date, usegmt=True)})
    assert retry_delay(error, 0) == 25


def test_anthropic_reset_timestamp(sdk, clock):
    date = dt.datetime.fromtimestamp(1_700_000_042, dt.UTC)
    error = rate_error(sdk, headers={"anthropic-ratelimit-tokens-reset": date.isoformat()})
    assert retry_delay(error, 0) == 42


def test_google_retry_info(sdk, clock):
    error = rate_error(
        sdk,
        body={
            "error": {
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "18s"}
                ]
            }
        },
    )
    assert retry_delay(error, 0) == 18


def test_missing_retry_hint_uses_minute_backoff(sdk, clock):
    assert retry_delay(rate_error(sdk), 0) == 60
    assert retry_delay(rate_error(sdk), 1) == 120


def test_429_retries_without_hidden_sdk_retries(sdk, clock, monkeypatch):
    completion = MagicMock(side_effect=[rate_error(sdk, headers={"Retry-After": "12"}), response()])
    monkeypatch.setattr(sdk, "completion", completion)
    config = Config(llm_model="gemini/test")
    limiter = RateLimiter(config)
    assert (
        llm.summarize_article("Source", "Title", "https://example.org", config, limiter)
        == "A useful summary."
    )
    assert clock.now == 12
    assert limiter.requests_sent == 2
    assert limiter.rate_limits_seen == 1
    assert completion.call_args.kwargs["num_retries"] == 0
    assert completion.call_args.kwargs["max_retries"] == 0


def test_retry_is_also_subject_to_rpm(sdk, clock, monkeypatch):
    monkeypatch.setattr(
        sdk,
        "completion",
        MagicMock(side_effect=[rate_error(sdk, headers={"Retry-After": "1"}), response()]),
    )
    config = Config(llm_model="gemini/test", llm_rpm_limit=1)
    llm.summarize_article("Source", "Title", "https://example.org", config, RateLimiter(config))
    assert clock.now == 60


def test_retry_budget_exhaustion_preserves_cooldown_for_next_article(sdk, clock, monkeypatch):
    completion = MagicMock(
        side_effect=[
            rate_error(sdk, headers={"Retry-After": "15"}),
            rate_error(sdk, headers={"Retry-After": "15"}),
            response(),
        ]
    )
    monkeypatch.setattr(sdk, "completion", completion)
    config = Config(llm_model="gemini/test", max_retries=1)
    limiter = RateLimiter(config)
    with pytest.raises(llm.SummaryError, match="bounded retries"):
        llm.summarize_article("Source", "Title", "https://example.org", config, limiter)
    assert completion.call_count == 2
    llm.summarize_article("Other source", "Title", "https://example.org", config, limiter)
    assert clock.now == 30


def test_excessive_provider_wait_stops_the_run(sdk, clock, monkeypatch):
    completion = MagicMock(side_effect=rate_error(sdk, headers={"Retry-After": "86400"}))
    monkeypatch.setattr(sdk, "completion", completion)
    with pytest.raises(llm.ModelError, match="LLM_MAX_WAIT_SECONDS"):
        llm.summarize_article("Source", "Title", "https://example.org", Config(llm_model="test"))
    completion.assert_called_once()
    assert not clock.sleeps


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"code": "insufficient_quota"}},
        {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDay"}]}]}},
    ],
)
def test_daily_or_billing_quota_is_not_retried(sdk, clock, monkeypatch, body):
    completion = MagicMock(side_effect=rate_error(sdk, body=body))
    monkeypatch.setattr(sdk, "completion", completion)
    with pytest.raises(llm.ModelError, match="quota or billing"):
        llm.summarize_article("Source", "Title", "https://example.org", Config(llm_model="test"))
    completion.assert_called_once()
    assert not clock.sleeps


def test_authentication_failure_is_not_retried(sdk, clock, monkeypatch):
    completion = MagicMock(
        side_effect=sdk.AuthenticationError("bad key", llm_provider="gemini", model="test")
    )
    monkeypatch.setattr(sdk, "completion", completion)
    with pytest.raises(llm.ModelError):
        llm.summarize_article("Source", "Title", "https://example.org", Config(llm_model="test"))
    completion.assert_called_once()
    assert not clock.sleeps


def test_transient_failure_retries_through_the_same_limiter(sdk, clock, monkeypatch):
    completion = MagicMock(
        side_effect=[
            sdk.ServiceUnavailableError("busy", llm_provider="gemini", model="test"),
            response(),
        ]
    )
    monkeypatch.setattr(sdk, "completion", completion)
    llm.summarize_article("Source", "Title", "https://example.org", Config(llm_model="test"))
    assert completion.call_count == 2
    assert 1 <= clock.now <= 2


def test_model_tokenizer_and_output_allowance_are_included(sdk, monkeypatch):
    counter = MagicMock(return_value=1000)
    monkeypatch.setattr(sdk, "token_counter", counter)
    config = Config(llm_model="gemini/custom-model", llm_tpm_limit=5000)
    messages, reserved = llm.prepare_request("Source", "Title", "https://example.org", config)
    assert reserved == 1150 + 1800
    counter.assert_called_once_with(model=config.llm_model, messages=messages)


def test_oversized_article_is_trimmed_to_fit_tpm(sdk, monkeypatch):
    monkeypatch.setattr(
        sdk, "token_counter", lambda **kwargs: len(kwargs["messages"][0]["content"])
    )
    config = Config(llm_model="test", llm_tpm_limit=3000)
    messages, reserved = llm.prepare_request(
        "Source paragraph. " * 1000, "Title", "https://example.org", config
    )
    assert reserved <= 3000
    assert len(messages[0]["content"]) < 2000
    assert "Source paragraph." in messages[0]["content"]


def test_tpm_too_small_for_prompt_and_output_fails_before_generation(sdk, clock, monkeypatch):
    monkeypatch.setattr(sdk, "token_counter", MagicMock(return_value=1000))
    completion = MagicMock()
    monkeypatch.setattr(sdk, "completion", completion)
    with pytest.raises(llm.ModelError, match="LLM_TPM_LIMIT is too small"):
        llm.summarize_article(
            "Source", "Title", "https://example.org", Config(llm_model="test", llm_tpm_limit=1000)
        )
    completion.assert_not_called()
    assert not clock.sleeps


def test_tokenizer_failure_uses_conservative_fallback(sdk, monkeypatch):
    monkeypatch.setattr(
        sdk, "token_counter", MagicMock(side_effect=ValueError("unknown tokenizer"))
    )
    count = llm.estimate_tokens([{"role": "user", "content": "日本語"}], Config(llm_model="custom"))
    assert count >= len("日本語".encode())


def test_digest_shares_budget_across_articles_and_reports_wait(sdk, clock, monkeypatch, tmp_path):
    from hn_digest import pipeline
    from hn_digest.fetching import Story

    stories = [Story(i, f"Title {i}", f"https://example.org/{i}") for i in (1, 2)]
    monkeypatch.setattr(pipeline, "get_top_stories", lambda *args: stories)
    monkeypatch.setattr(pipeline, "fetch_article_text", lambda *args: "Source article. " * 100)
    monkeypatch.setattr(sdk, "completion", MagicMock(return_value=response(tokens=100)))
    config = Config(llm_model="gemini/test", llm_rpm_limit=1, story_limit=2)
    assert pipeline.run_digest(config, tmp_path, dry_run=True, no_llm=False) == 0
    report = json.loads((tmp_path / "run-report.json").read_text())
    assert report["status"] == "success"
    assert report["llm"]["requests_sent"] == 2
    assert report["llm"]["wait_seconds"] == 60
    assert report["llm"]["reported_tokens"] == 200
    assert clock.now == 60
