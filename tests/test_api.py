"""End-to-end tests for the HTTP API, pinned to the offline demo backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_settings(db_path: Path) -> Settings:
    return Settings(
        provider="demo",
        api_base="https://api.openai.com/v1",
        api_key="",
        model="gpt-4o-mini",
        ollama_base="http://127.0.0.1:11434",
        ollama_model="qwen2.5:7b",
        host="127.0.0.1",
        port=8000,
        temperature=0.7,
        max_tokens=512,
        timeout=30.0,
        history_limit=20,
        db_path=db_path,
    )


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(make_settings(tmp_path / "api.db"))
    with TestClient(app) as test_client:
        yield test_client


def test_health_and_config_report_demo_mode(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["provider"] == "demo"

    config = client.get("/api/config").json()
    assert config["demo_mode"] is True
    assert config["default_persona"] == "grok"
    assert {p["id"] for p in config["personas"]} == {"grok", "analyst", "coder"}


def test_index_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "GrokBot" in response.text


def collect_sse(body: str) -> dict:
    """Fold an SSE response into {event: [payloads]} plus a joined token stream."""
    events: dict = {}
    for frame in body.split("\n\n"):
        event = "message"
        data_lines = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        events.setdefault(event, []).append(json.loads("\n".join(data_lines)))
    return events


def test_chat_streams_and_persists_conversation(client: TestClient) -> None:
    response = client.post("/api/chat", json={"message": "(128 + 72) * 3 等于多少"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = collect_sse(response.text)
    assert "meta" in events
    assert events["delta"], "expected streamed deltas"
    assert "done" in events

    joined = "".join(piece["text"] for piece in events["delta"])
    assert "600" in joined

    conversation_id = events["meta"][0]["conversation_id"]
    assert conversation_id

    conversation = client.get(f"/api/conversations/{conversation_id}").json()
    assert conversation["message_count"] == 2
    assert conversation["messages"][0]["role"] == "user"
    assert conversation["messages"][1]["role"] == "assistant"
    assert conversation["title"].startswith("(128")


def test_chat_rejects_blank_message(client: TestClient) -> None:
    response = client.post("/api/chat", json={"message": "   "})
    assert response.status_code in {400, 422}


def test_chat_on_missing_conversation_returns_404(client: TestClient) -> None:
    response = client.post("/api/chat", json={"message": "你好", "conversation_id": "nope"})
    assert response.status_code == 404


def test_conversation_lifecycle(client: TestClient) -> None:
    created = client.post("/api/conversations", json={"persona": "coder"}).json()
    conversation_id = created["id"]
    assert created["persona"] == "coder"

    renamed = client.patch(f"/api/conversations/{conversation_id}", json={"title": "新标题"})
    assert renamed.status_code == 200
    assert client.get(f"/api/conversations/{conversation_id}").json()["title"] == "新标题"

    listed = client.get("/api/conversations").json()["conversations"]
    assert any(row["id"] == conversation_id for row in listed)

    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 200
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404


def test_invalid_persona_falls_back_to_default(client: TestClient) -> None:
    response = client.post("/api/chat", json={"message": "你好", "persona": "unknown-persona"})
    assert response.status_code == 200
    assert "event: done" in response.text
