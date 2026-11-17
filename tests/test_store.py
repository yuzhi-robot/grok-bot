"""Tests for the SQLite conversation store."""

from __future__ import annotations

from pathlib import Path

from app.store import Store


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.init()
    return store


def test_create_and_read_conversation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create_conversation("grok")
    assert created["title"] == "新对话"

    store.append_message(created["id"], "user", "你好")
    store.append_message(created["id"], "assistant", "在。", provider="demo")

    conversation = store.get_conversation(created["id"])
    assert conversation is not None
    assert conversation["persona"] == "grok"
    assert conversation["message_count"] == 2
    assert [m["role"] for m in conversation["messages"]] == ["user", "assistant"]


def test_missing_conversation_returns_none(tmp_path: Path) -> None:
    assert _store(tmp_path).get_conversation("deadbeef") is None


def test_auto_title_uses_first_message(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conversation = store.create_conversation("grok")
    title = store.auto_title(conversation["id"], "帮我写一个 Python 快速排序")
    assert title == "帮我写一个 Python 快速排序"
    assert store.get_conversation(conversation["id"])["title"] == title


def test_auto_title_is_stable_after_rename(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conversation = store.create_conversation("grok")
    store.auto_title(conversation["id"], "第一条消息")
    store.rename_conversation(conversation["id"], "改过的标题")
    assert store.auto_title(conversation["id"], "第二条消息") == "改过的标题"


def test_messages_for_model_filters_system_roles(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conversation = store.create_conversation("grok")
    store.append_message(conversation["id"], "user", "问题")
    store.append_message(conversation["id"], "assistant", "答案")
    history = store.messages_for_model(conversation["id"])
    assert history == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "答案"},
    ]


def test_list_orders_by_recent_activity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.create_conversation("grok")
    second = store.create_conversation("coder")
    store.append_message(first["id"], "user", "让第一个浮上来")
    ids = [row["id"] for row in store.list_conversations()]
    assert ids[0] == first["id"]
    assert second["id"] in ids


def test_delete_removes_conversation_and_messages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conversation = store.create_conversation("grok")
    store.append_message(conversation["id"], "user", "会被删掉")
    assert store.delete_conversation(conversation["id"]) is True
    assert store.get_conversation(conversation["id"]) is None
    assert store.delete_conversation(conversation["id"]) is False
