import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.config import Settings
from app.services.ai import AIService, AITruncated, AIUnavailable, relevant_context, split_text
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


@pytest.mark.parametrize("first_response", [AITruncated(), '{"flashcards":[]}'])
async def test_incomplete_notes_retry_with_shorter_format(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, first_response: AITruncated | str
) -> None:
    service = AIService(settings)
    notes = study_notes()
    completion = AsyncMock(side_effect=[first_response, notes.model_dump_json()])
    monkeypatch.setattr(service, "_completion", completion)
    assert await service.generate_notes("Lecture") == notes
    first, second = (call.args for call in completion.await_args_list)
    assert first[1] == second[1] == "Lecture"
    assert "under 500 words" in second[0]


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


@pytest.mark.parametrize("provider", ["openai", "deepseek", "orcarouter", "openrouter"])
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
    assert payload.get("extra_body") is None
    manager.__aexit__.assert_awaited_once()


async def test_openrouter_notes_allow_reasoning_and_preserve_strict_schema(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.ai_provider = "openrouter"
    service = AIService(settings)
    expected = study_notes()

    def openrouter(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "reasoning" not in payload
        assert payload["provider"] == {"require_parameters": True}
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert (
            payload["response_format"]["json_schema"]["schema"]
            == type(expected).model_json_schema()
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": expected.model_dump_json()}}
                ]
            },
        )

    client = AsyncOpenAI(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(openrouter)),
    )
    monkeypatch.setattr(service, "_client", lambda: client)
    assert await service.generate_notes("A lecture on Euclidean geometry.") == expected


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


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"error": {"code": 502, "message": "private provider detail"}}),
        httpx.Response(200, json={"choices": [None]}),
        httpx.Response(200, text="private provider detail"),
        httpx.Response(200, text="{", headers={"Content-Type": "application/json"}),
    ],
)
async def test_provider_errors_in_success_responses_are_handled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    service = AIService(settings)
    client = AsyncOpenAI(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)),
    )
    monkeypatch.setattr(service, "_client", lambda: client)
    with pytest.raises(AIUnavailable) as exc:
        await service.answer_question("Lecture", "Question?")
    assert "private provider detail" not in str(exc.value)


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


@pytest.mark.parametrize(
    ("provider", "key_name", "base_url"),
    [
        ("orcarouter", "ORCA_KEY", "https://api.orcarouter.ai/v1/"),
        ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/"),
    ],
)
async def test_router_reads_its_key_from_environment(
    monkeypatch: pytest.MonkeyPatch, provider: str, key_name: str, base_url: str
) -> None:
    monkeypatch.setenv("AI_PROVIDER", provider)
    monkeypatch.setenv("AI_MODEL", "test-model")
    monkeypatch.setenv(key_name, "test-router-key")
    settings = Settings(_env_file=None)

    async with AIService(settings)._client() as client:
        assert str(client.base_url) == base_url
        assert client.api_key == "test-router-key"
