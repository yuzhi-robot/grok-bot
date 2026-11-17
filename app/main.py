"""FastAPI application: streaming chat API plus the static single-page UI."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT, Settings, load_dotenv
from .prompts import DEFAULT_PERSONA, PERSONAS
from .providers import ProviderError, build_messages, resolve_provider
from .schemas import ChatRequest, ConfigResponse, ConversationCreate, ConversationRename
from .store import UNTITLED, Store

VERSION = "1.0.0"
STATIC_DIR = PROJECT_ROOT / "static"

PERSONA_LIST = [
    {"id": item["id"], "label": item["label"], "description": item["description"]}
    for item in PERSONAS.values()
]


def _sse(event: str, data: Dict[str, Any]) -> str:
    """Serialize one server-sent event frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _normalise_persona(persona: Optional[str]) -> str:
    return persona if persona in PERSONAS else DEFAULT_PERSONA


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    load_dotenv()
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.init()
        app.state.settings = settings
        app.state.store = store
        provider, note = await resolve_provider(settings)
        app.state.provider = provider
        app.state.provider_note = note
        yield

    app = FastAPI(
        title="GrokBot",
        version=VERSION,
        summary="Grok 风格的开源 AI 聊天机器人",
        lifespan=lifespan,
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- UI ---------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health(request: Request) -> Dict[str, Any]:
        provider = request.app.state.provider
        return {
            "status": "ok",
            "version": VERSION,
            "provider": provider.name,
            "model": provider.model,
        }

    @app.get("/api/config", response_model=ConfigResponse)
    async def config(request: Request) -> Dict[str, Any]:
        provider = request.app.state.provider
        demo = provider.name == "demo"
        note = getattr(request.app.state, "provider_note", "") or ""
        hint = note
        if demo and note:
            hint = note
        elif not note:
            hint = f"已连接 {provider.display_name}（{provider.model}）"
        payload = provider.describe()
        payload.update(
            {
                "demo_mode": demo,
                "personas": PERSONA_LIST,
                "default_persona": DEFAULT_PERSONA,
                "hint": hint,
            }
        )
        return payload

    # -- conversations ----------------------------------------------------
    @app.get("/api/conversations")
    async def list_conversations(request: Request) -> Dict[str, Any]:
        return {"conversations": request.app.state.store.list_conversations()}

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(payload: ConversationCreate, request: Request) -> Dict[str, Any]:
        persona = _normalise_persona(payload.persona)
        title = " ".join((payload.title or UNTITLED).split())[:40] or UNTITLED
        return request.app.state.store.create_conversation(persona, title)

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, request: Request) -> Dict[str, Any]:
        conversation = request.app.state.store.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return conversation

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(
        conversation_id: str, payload: ConversationRename, request: Request
    ) -> Dict[str, Any]:
        if not request.app.state.store.rename_conversation(conversation_id, payload.title):
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"ok": True, "title": " ".join(payload.title.split())[:40]}

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, request: Request) -> Dict[str, Any]:
        if not request.app.state.store.delete_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"ok": True}

    # -- chat -------------------------------------------------------------
    @app.post("/api/chat")
    async def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
        message = payload.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="消息不能为空")

        store: Store = request.app.state.store
        provider = request.app.state.provider

        if payload.conversation_id:
            existing = store.get_conversation(payload.conversation_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="会话不存在")
            conversation_id = payload.conversation_id
            persona = _normalise_persona(payload.persona or existing["persona"])
        else:
            persona = _normalise_persona(payload.persona)
            conversation_id = store.create_conversation(persona)["id"]

        store.append_message(conversation_id, "user", message)
        title = store.auto_title(conversation_id, message)

        # Read history *after* persisting the new turn, so the model always sees
        # the message it is supposed to answer.
        history = store.messages_for_model(conversation_id)
        messages = build_messages(history, persona, settings.history_limit)
        temperature = payload.temperature if payload.temperature is not None else settings.temperature

        async def event_stream() -> AsyncIterator[str]:
            yield _sse(
                "meta",
                {
                    "conversation_id": conversation_id,
                    "title": title,
                    "provider": provider.name,
                    "model": provider.model,
                    "persona": persona,
                },
            )
            collected: list[str] = []
            try:
                async for delta in provider.stream_chat(messages, temperature, settings.max_tokens):
                    collected.append(delta)
                    yield _sse("delta", {"text": delta})
            except ProviderError as exc:
                if collected:
                    store.append_message(conversation_id, "assistant", "".join(collected), provider.name)
                yield _sse("error", {"message": str(exc), "hint": exc.hint})
                return
            except Exception as exc:  # pragma: no cover - network edge cases
                if collected:
                    store.append_message(conversation_id, "assistant", "".join(collected), provider.name)
                yield _sse(
                    "error",
                    {
                        "message": f"模型调用失败：{exc}",
                        "hint": "检查网络与模型配置，或把 GROKBOT_PROVIDER 设为 demo 先用起来。",
                    },
                )
                return

            answer = "".join(collected).strip()
            if not answer:
                yield _sse("error", {"message": "模型没有返回任何内容。", "hint": "换一个模型或稍后重试。"})
                return
            store.append_message(conversation_id, "assistant", answer, provider.name)
            yield _sse(
                "done",
                {
                    "conversation_id": conversation_id,
                    "title": title,
                    "model": provider.model,
                    "characters": len(answer),
                },
            )

        headers = {
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)

    @app.exception_handler(404)
    async def not_found(request: Request, exc: HTTPException) -> JSONResponse:  # pragma: no cover
        if request.url.path.startswith("/api"):
            return JSONResponse(status_code=404, content={"detail": str(exc.detail)})
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    current = app.state.settings if hasattr(app.state, "settings") else Settings.from_env()
    uvicorn.run("app.main:app", host=current.host, port=current.port, reload=False)
