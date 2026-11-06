from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import Settings
from app.services.ai import AIService, AIUnavailable, relevant_context, split_text
from tests.test_processing import study_notes


async def test_notes_process_every_chunk_and_merge(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.ai_chunk_chars = 1000
    service = AIService(settings)
    completion = AsyncMock(return_value=study_notes().model_dump_json())
    monkeypatch.setattr(service, "_completion", completion)
    text = "A" * 1000 + "B" * 1000 + "TAIL"
    await service.generate_notes(text)
    assert completion.await_count == 4  # Three source chunks plus the final merge.
    assert [c.args[1] for c in completion.await_args_list[:3]] == ["A" * 1000, "B" * 1000, "TAIL"]


@pytest.mark.parametrize("output", ["not json", "{}", '{"structured_markdown":"only text"}'])
async def test_invalid_json_is_rejected(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    service = AIService(settings)
    monkeypatch.setattr(service, "_completion", AsyncMock(return_value=output))
    with pytest.raises(AIUnavailable):
        await service.generate_notes("Lecture")


async def test_limits_fail_explicitly(settings: Settings) -> None:
    settings.ai_max_chunks = 1
    with pytest.raises(AIUnavailable):
        await AIService(settings).generate_notes("x" * (settings.ai_chunk_chars + 1))


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_sdk_payload_and_client_closure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    settings.ai_provider = provider
    service = AIService(settings)
    client = Mock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="Answer", refusal=None)
                )
            ]
        )
    )
    manager = AsyncMock()
    manager.__aenter__.return_value = client
    monkeypatch.setattr(service, "_client", lambda: manager)
    assert await service.answer_question("Lecture", "Question?") == "Answer"
    payload = client.chat.completions.create.call_args.kwargs
    assert payload["model"] == "test-model"
    assert payload["response_format"] == {"type": "text"}
    token_arg = "max_completion_tokens" if provider == "openai" else "max_tokens"
    assert payload[token_arg] == settings.ai_max_output_tokens
    manager.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("content,finish", [("", "stop"), ("  ", "stop"), ("partial", "length")])
async def test_empty_and_truncated_results_fail(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, content: str, finish: str
) -> None:
    service = AIService(settings)
    client = Mock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish, message=SimpleNamespace(content=content, refusal=None)
                )
            ]
        )
    )
    manager = AsyncMock()
    manager.__aenter__.return_value = client
    monkeypatch.setattr(service, "_client", lambda: manager)
    with pytest.raises(AIUnavailable):
        await service.answer_question("Lecture", "Question?")


def test_retrieval_searches_the_end_of_a_long_lecture() -> None:
    text = "a" * 6000 * 7 + "Photosynthesis converts light energy."
    context = relevant_context(text, "Explain photosynthesis")
    assert "Photosynthesis" in context
    assert len(context) < len(text)
    assert "".join(split_text(text, 1000)) == text


def test_missing_ai_configuration_fails(settings: Settings) -> None:
    settings.ai_model = ""
    with pytest.raises(AIUnavailable):
        AIService(settings)._client()
