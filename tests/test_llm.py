from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hn_digest.config import DEFAULT_SYSTEM_PROMPT, Config
from hn_digest.llm import ModelError, article_messages, prepare_request, summarize_article


def test_default_prompt_is_a_separate_system_message():
    messages = article_messages(
        "Untrusted article text.", "Article title", "https://example.org", Config()
    )
    assert messages == [
        {
            "role": "system",
            "content": DEFAULT_SYSTEM_PROMPT.replace("{target_language}", "Italian"),
        },
        {
            "role": "user",
            "content": (
                "Title: Article title\nSource: https://example.org\nTarget language: Italian\n"
                "<article>\nUntrusted article text.\n</article>"
            ),
        },
    ]
    assert "Untrusted article text." not in messages[0]["content"]


def test_custom_prompt_expands_language_and_preserves_other_braces():
    prompt = 'Write in {target_language}.\nReturn {"summary": "..."}; preserve {other}.'
    messages = article_messages(
        "Article",
        "Title",
        "https://example.org",
        Config(llm_system_prompt=prompt, target_language="German", language_code="de"),
    )
    assert messages[0] == {
        "role": "system",
        "content": 'Write in German.\nReturn {"summary": "..."}; preserve {other}.',
    }


def test_custom_prompt_is_forwarded_to_completion(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    completion = MagicMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Summary"), finish_reason="stop")
            ]
        )
    )
    monkeypatch.setattr(litellm, "completion", completion)
    config = Config(
        llm_system_prompt="Write in {target_language} using five paragraphs.", llm_tpm_limit=0
    )
    assert summarize_article("Article", "Title", "https://example.org", config) == "Summary"
    assert completion.call_args.kwargs["messages"][0] == {
        "role": "system",
        "content": "Write in Italian using five paragraphs.",
    }
    assert completion.call_args.kwargs["max_tokens"] == 10000


def test_token_budget_includes_custom_system_prompt(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    counter = MagicMock(
        side_effect=lambda **kwargs: sum(len(message["content"]) for message in kwargs["messages"])
    )
    monkeypatch.setattr(litellm, "token_counter", counter)
    config = Config(
        llm_system_prompt="Instructions. " * 500, llm_tpm_limit=3000, llm_max_output_tokens=1800
    )
    with pytest.raises(ModelError, match="LLM_TPM_LIMIT is too small"):
        prepare_request("Source. " * 100, "Title", "https://example.org", config)
    assert counter.call_args_list[0].kwargs["messages"][0]["content"] == config.llm_system_prompt
