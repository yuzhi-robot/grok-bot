"""Model backends: OpenAI-compatible API, local Ollama, and an offline demo engine.

Every backend exposes the same tiny contract: an async generator that yields
plain text deltas. That keeps the API layer honest -- it never has to know which
model is behind it, and the UI always renders a token stream.
"""

from __future__ import annotations

import ast
import asyncio
import json
import operator
import re
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence

import httpx

from .config import Settings
from .prompts import build_system_prompt


class ProviderError(RuntimeError):
    """Raised when the upstream model refuses or fails to answer."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


Message = Dict[str, str]


def _hint_for_status(status: int) -> str:
    if status == 401 or status == 403:
        return "鉴权失败：检查 OPENAI_API_KEY 是否正确、是否已过期。"
    if status == 404:
        return "接口不存在：检查 OPENAI_BASE_URL 是否已包含 /v1，以及模型名是否存在。"
    if status == 429:
        return "请求被限流：稍后重试，或换用额度更充足的 Key。"
    if status >= 500:
        return "上游服务异常：稍后重试。"
    return ""


class BaseProvider:
    """Common shape of a backend."""

    name: str = "base"
    display_name: str = "模型"
    model: str = ""
    modality: str = "remote"

    async def stream_chat(
        self,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:  # pragma: no cover - interface
        raise NotImplementedError
        yield ""  # pragma: no cover

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "display_name": self.display_name,
            "model": self.model,
            "mode": self.modality,
        }


class OpenAICompatibleProvider(BaseProvider):
    """Any server that speaks the OpenAI ``/chat/completions`` dialect."""

    name = "openai"
    display_name = "OpenAI 兼容接口"
    modality = "remote"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def stream_chat(
        self,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", self.endpoint, json=payload, headers=self._headers()) as response:
                if response.status_code >= 300:
                    detail = await _read_error_body(response)
                    raise ProviderError(
                        f"上游返回 HTTP {response.status_code}：{detail}",
                        _hint_for_status(response.status_code),
                    )
                async for line in response.aiter_lines():
                    for delta in parse_openai_sse_line(line):
                        yield delta


def parse_openai_sse_line(line: str) -> Iterable[str]:
    """Turn one SSE line from an OpenAI-style stream into zero or more deltas."""
    if not line or not line.startswith("data:"):
        return ()
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return ()
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return ()
    if "error" in event and event["error"]:
        error = event["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise ProviderError(f"上游返回错误：{message}")
    choices = event.get("choices") or []
    if not choices:
        return ()
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if content:
        return (content,)
    return ()


class OllamaProvider(BaseProvider):
    """Local Ollama server (no API key, fully offline once the model is pulled)."""

    name = "ollama"
    display_name = "本地 Ollama"
    modality = "local"

    def __init__(self, base_url: str, model: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def stream_chat(
        self,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
                if response.status_code >= 300:
                    detail = await _read_error_body(response)
                    raise ProviderError(
                        f"Ollama 返回 HTTP {response.status_code}：{detail}",
                        f"确认已启动 Ollama 且已执行 ollama pull {self.model}",
                    )
                async for line in response.aiter_lines():
                    for delta in parse_ollama_line(line):
                        yield delta


def parse_ollama_line(line: str) -> Iterable[str]:
    """Turn one NDJSON line from Ollama's streaming chat API into deltas."""
    if not line.strip():
        return ()
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return ()
    if event.get("error"):
        raise ProviderError(f"Ollama 返回错误：{event['error']}")
    content = (event.get("message") or {}).get("content")
    if content:
        return (content,)
    return ()


_SAFE_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_SAFE_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def safe_eval(expression: str) -> Optional[float]:
    """Evaluate a bare arithmetic expression without ``eval``.

    Only numbers and the six basic operators are reachable, so nothing can call
    into the interpreter. Returns ``None`` when the input is not arithmetic.
    """

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_BINARY:
            return _SAFE_BINARY[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_UNARY:
            return _SAFE_UNARY[type(node.op)](_eval(node.operand))
        raise ValueError("unsupported expression")

    cleaned = expression.strip().replace("×", "*").replace("÷", "/").replace("，", ",")
    cleaned = re.sub(r"[\s,]|等于|等于几|多少|计算|算一下|=", "", cleaned)
    cleaned = cleaned.replace("^", "**")
    if not cleaned or not re.fullmatch(r"[\d\.\+\-\*/%\(\)]+", cleaned):
        return None
    if len(cleaned) > 60:
        return None
    try:
        result = _eval(ast.parse(cleaned, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError):
        return None
    return result


def _format_number(value: float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}" if isinstance(value, float) else str(value)


class DemoProvider(BaseProvider):
    """Offline fallback so the app is runnable before any key exists.

    It intentionally does **not** pretend to be a language model. It answers a
    narrow set of things it can do honestly -- arithmetic, capability notes,
    template scaffolding -- and otherwise explains how to connect a real model.
    """

    name = "demo"
    display_name = "离线演示引擎"
    model = "grokbot-demo"
    modality = "offline"

    _GREETING = re.compile(r"^(你好|您好|hi|hello|hey|在吗|哈喽)[!！?？。\s]*$", re.IGNORECASE)

    async def stream_chat(
        self,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        text = self.compose(messages)
        for chunk in _chunk_text(text):
            yield chunk
            await asyncio.sleep(0.012)

    def compose(self, messages: Sequence[Message]) -> str:
        user_turns = [m["content"] for m in messages if m.get("role") == "user"]
        question = (user_turns[-1] if user_turns else "").strip()
        lowered = question.lower()

        if not question:
            return self._welcome()

        if self._GREETING.match(question):
            return self._greeting()

        value = safe_eval(question)
        if value is not None:
            return (
                f"**{_format_number(value)}**\n\n"
                f"算式：`{question.strip()}`。这是演示引擎本地算出来的，"
                "不走网络也不调用模型——所以纯算术类问题在离线状态下也能拿到准确答案。"
            )

        if any(word in lowered for word in ("你是谁", "名字", "who are you", "what are you")):
            return self._identity()

        if any(word in lowered for word in ("能做什么", "功能", "怎么用", "help", "帮助", "有什么能力")):
            return self._capabilities()

        if any(word in lowered for word in ("代码", "报错", "bug", "函数", "code", "python", "接口")):
            return self._code_hint(question)

        return self._generic(question, len(user_turns))

    # -- canned replies -------------------------------------------------
    def _welcome(self) -> str:
        return (
            "在线。当前是**离线演示引擎**（没有接真实模型），"
            "我能做算术、给出结构化的回答骨架，也能告诉你怎样把真实模型接上。\n\n"
            "试试问我：`(12+8)*3 等于多少`、`你能做什么`，或者直接聊任何话题看看回答骨架。"
        )

    def _greeting(self) -> str:
        return (
            "在。\n\n"
            "先说清楚一件事：现在跑的是**离线演示引擎**，不是真正的大模型，"
            "所以复杂问题只会拿到回答骨架。想让它真的会思考，在 `.env` 里填一个 "
            "`OPENAI_API_KEY`（或启动本地 Ollama）再重启服务就行。\n\n"
            "顺手可以试试：`128*7`。"
        )

    def _identity(self) -> str:
        return (
            "我是 **GrokBot**，一个开源的 Grok 风格聊天机器人外壳：浏览器界面、流式输出、"
            "多会话记忆都齐了，模型后端可以随时换。\n\n"
            "- 后端支持三类：OpenAI 兼容接口 / 本地 Ollama / 离线演示引擎（当前）\n"
            "- 当前没有配置模型密钥，所以降级到演示引擎，保证项目开箱就能跑\n"
            "- 换成真实模型只需改 `.env` 里的三行配置"
        )

    def _capabilities(self) -> str:
        return (
            "当前**演示模式**下的能力边界：\n\n"
            "| 能做 | 做不到 |\n"
            "| --- | --- |\n"
            "| 四则运算、百分比、括号表达式 | 真正的自然语言理解与推理 |\n"
            "| 给出结构化回答骨架 | 联网检索、实时信息 |\n"
            "| 说明如何接入真实模型 | 长文本创作、代码生成 |\n\n"
            "接上真实模型后，这些限制都会消失。配置方式：\n\n"
            "```bash\n"
            "# .env\n"
            "GROKBOT_PROVIDER=openai\n"
            "OPENAI_BASE_URL=https://api.openai.com/v1\n"
            "OPENAI_API_KEY=sk-...\n"
            "OPENAI_MODEL=gpt-4o-mini\n"
            "```\n\n"
            "想完全离线的话，装好 Ollama 后执行 `ollama pull qwen2.5:7b`，"
            "再把 `GROKBOT_PROVIDER` 设成 `ollama`。"
        )

    def _code_hint(self, question: str) -> str:
        topic = _shorten(question)
        return (
            f"关于「{topic}」，当前是演示模式，我不做真实的代码推理。给你一个可用的回答骨架：\n\n"
            "1. **现象**：把报错原文、发生位置、最近改动的文件贴出来\n"
            "2. **定位**：先用最小复现缩小范围，再检查输入/状态/边界条件\n"
            "3. **修复**：改完跑测试，确认没有把别的地方弄坏\n\n"
            "接入真实模型后，这里会直接给出你的代码补丁。当前可以直接调本项目接口验证链路：\n\n"
            "```bash\n"
            "curl -N -X POST http://127.0.0.1:8000/api/chat \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            "  -d '{\"message\": \"这个 IndexError 怎么修\"}'\n"
            "```"
        )

    def _generic(self, question: str, turn_count: int) -> str:
        topic = _shorten(question)
        return (
            f"你问的是「{topic}」。说实话，**演示引擎答不了这个**——它没有语言模型，"
            "硬编造答案对你没有价值。\n\n"
            "我能给的是回答骨架，你接上真实模型后可以直接复用这个结构：\n\n"
            "1. **结论先行**：一句话回答核心问题\n"
            "2. **依据**：数据、来源、前提条件\n"
            "3. **边界**：什么情况下这个结论不成立\n"
            "4. **下一步**：可执行的动作\n\n"
            f"（这是本轮第 {turn_count} 次提问，多轮上下文已经存进本地 SQLite，换模型后会一并带上。）\n\n"
            "要让它真正回答，任选一种：填 `OPENAI_API_KEY`，或本地跑 `ollama serve` + 拉一个模型。"
        )


def _shorten(text: str, limit: int = 42) -> str:
    single_line = re.sub(r"\s+", " ", text).strip()
    return single_line if len(single_line) <= limit else single_line[:limit] + "…"


def _chunk_text(text: str, size: int = 4) -> Iterable[str]:
    """Slice text into small pieces so the UI renders a streaming effect."""
    for index in range(0, len(text), size):
        yield text[index : index + size]


async def _read_error_body(response: httpx.Response) -> str:
    try:
        body = await response.aread()
    except Exception:  # pragma: no cover - defensive
        return "无法读取响应体"
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return "空响应体"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:300]
    if isinstance(error, str):
        return error[:300]
    return text[:300]


def model_is_available(configured: str, available: Sequence[str]) -> bool:
    """Check whether the configured Ollama tag exists locally.

    Ollama resolves tags strictly, so a prefix like ``qwen2.5`` does **not** run
    ``qwen2.5:7b``. The only leniency applied here is the implicit ``:latest``
    suffix, which Ollama itself treats as equivalent.
    """
    if not configured:
        return False

    def normalise(tag: str) -> str:
        clean = tag.strip().lower()
        return clean[: -len(":latest")] if clean.endswith(":latest") else clean

    target = normalise(configured)
    return any(normalise(name) == target for name in available if name)


async def fetch_ollama_models(base_url: str, timeout: float = 1.5) -> Optional[List[str]]:
    """Return locally installed Ollama tags, or ``None`` when the server is unreachable."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/tags")
        if response.status_code != 200:
            return None
        payload = response.json()
    except Exception:
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    names = []
    for item in models:
        if isinstance(item, dict):
            name = item.get("name") or item.get("model") or ""
            if name:
                names.append(str(name))
    return names


async def ollama_alive(base_url: str, timeout: float = 1.5) -> bool:
    """Cheap liveness probe."""
    return await fetch_ollama_models(base_url, timeout) is not None


async def resolve_provider(settings: Settings) -> tuple[BaseProvider, str]:
    """Pick the backend and return ``(provider, human_readable_reason)``.

    ``auto`` prefers a configured API key, then a local Ollama that actually has
    the requested model pulled, and finally falls back to the demo engine so the
    app is never unusable.
    """
    if settings.provider == "openai":
        return (
            OpenAICompatibleProvider(
                settings.api_base, settings.api_key, settings.model, settings.timeout
            ),
            f"已按 GROKBOT_PROVIDER=openai 使用接口 {settings.api_base}（{settings.model}）。",
        )

    if settings.provider == "ollama":
        return (
            OllamaProvider(settings.ollama_base, settings.ollama_model, settings.timeout),
            f"已按 GROKBOT_PROVIDER=ollama 使用本地模型 {settings.ollama_model}。",
        )

    if settings.provider == "demo":
        return (
            DemoProvider(),
            "已按 GROKBOT_PROVIDER=demo 强制使用离线演示引擎（只擅长算术与回答骨架）。",
        )

    # auto
    if settings.api_key:
        return (
            OpenAICompatibleProvider(
                settings.api_base, settings.api_key, settings.model, settings.timeout
            ),
            f"检测到 OPENAI_API_KEY，使用接口 {settings.api_base}（{settings.model}）。",
        )

    models = await fetch_ollama_models(settings.ollama_base, 1.5)
    if models is None:
        return (
            DemoProvider(),
            "没有配置 OPENAI_API_KEY，也没检测到本地 Ollama，已降级为离线演示引擎。"
            "填一个 Key 或执行 ollama serve 即可接入真实模型。",
        )
    if not models:
        return (
            DemoProvider(),
            f"检测到本地 Ollama（{settings.ollama_base}），但一个模型都没拉取，已降级为离线演示引擎。"
            f"执行 ollama pull {settings.ollama_model} 后重启本服务即可自动切换。",
        )
    if model_is_available(settings.ollama_model, models):
        return (
            OllamaProvider(settings.ollama_base, settings.ollama_model, settings.timeout),
            f"发现本地 Ollama 并命中模型 {settings.ollama_model}。",
        )
    return (
        DemoProvider(),
        f"本地 Ollama 在线，但没有模型 {settings.ollama_model}（现有：{', '.join(models[:5])}），"
        f"已降级为离线演示引擎。执行 ollama pull {settings.ollama_model}，"
        "或在 .env 里把 OLLAMA_MODEL 改成已有模型后重启。",
    )


def build_messages(history: Sequence[Dict[str, str]], persona: str, limit: int) -> List[Message]:
    """Convert stored history into the message list sent upstream."""
    trimmed = list(history)[-limit:] if limit > 0 else list(history)
    system = {"role": "system", "content": build_system_prompt(persona)}
    return [system] + [{"role": m["role"], "content": m["content"]} for m in trimmed]
