"""Tests for the model backends -- parsing, math fallback and demo streaming."""

from __future__ import annotations

import asyncio

import pytest

from app.providers import (
    DemoProvider,
    ProviderError,
    model_is_available,
    parse_ollama_line,
    parse_openai_sse_line,
    safe_eval,
)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2+3", 5),
        ("(128 + 72) * 3", 600),
        ("100/4", 25),
        ("2^10", 1024),
        ("-8 + 3", -5),
        ("17 % 5", 2),
        ("1,000 * 2", 2000),
        ("3 × 4", 12),
        ("9 ÷ 3", 3),
    ],
)
def test_safe_eval_handles_arithmetic(expression: str, expected: float) -> None:
    assert safe_eval(expression) == expected


@pytest.mark.parametrize("expression", ["", "你好", "__import__('os')", "print(1)", "2+", "a+b"])
def test_safe_eval_rejects_non_arithmetic(expression: str) -> None:
    assert safe_eval(expression) is None


def test_parse_openai_sse_line_extracts_content() -> None:
    line = 'data: {"choices":[{"delta":{"content":"你好"}}]}'
    assert list(parse_openai_sse_line(line)) == ["你好"]


def test_parse_openai_sse_line_ignores_noise() -> None:
    assert list(parse_openai_sse_line("")) == []
    assert list(parse_openai_sse_line("event: ping")) == []
    assert list(parse_openai_sse_line("data: [DONE]")) == []
    assert list(parse_openai_sse_line('data: {"choices":[{"delta":{}}]}')) == []


def test_parse_openai_sse_line_surfaces_upstream_error() -> None:
    with pytest.raises(ProviderError):
        list(parse_openai_sse_line('data: {"error":{"message":"invalid api key"}}'))


def test_parse_ollama_line_extracts_content() -> None:
    line = '{"message":{"role":"assistant","content":"你好"},"done":false}'
    assert list(parse_ollama_line(line)) == ["你好"]
    assert list(parse_ollama_line('{"done":true}')) == []


def test_demo_provider_streams_and_answers_math() -> None:
    provider = DemoProvider()

    async def collect() -> str:
        chunks = []
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "(128 + 72) * 3 等于多少"}], 0.7, 512
        ):
            chunks.append(chunk)
        return "".join(chunks)

    answer = asyncio.run(collect())
    assert "600" in answer
    assert "演示" in answer


def test_demo_provider_describes_itself() -> None:
    provider = DemoProvider()
    assert provider.describe()["mode"] == "offline"
    assert provider.compose([{"role": "user", "content": "你是谁"}]).startswith("我是")


@pytest.mark.parametrize(
    ("configured", "available", "expected"),
    [
        ("qwen2.5:7b", ["qwen2.5:7b"], True),
        ("QWEN2.5:7B", ["qwen2.5:7b"], True),
        ("qwen2.5", ["qwen2.5:latest"], True),
        ("qwen2.5:latest", ["qwen2.5"], True),
        # Ollama resolves tags strictly: these must NOT be treated as a match.
        ("qwen2.5:7b", ["qwen2.5:7b-instruct-q4_K_M"], False),
        ("qwen2.5", ["qwen2.5:7b"], False),
        ("qwen2.5:7b", ["llama3:8b"], False),
        ("qwen2.5:7b", [], False),
        ("", ["qwen2.5:7b"], False),
    ],
)
def test_model_is_available_matches_ollama_tags(configured: str, available: list, expected: bool) -> None:
    assert model_is_available(configured, available) is expected
