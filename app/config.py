"""Configuration loading for GrokBot.

Everything is driven by environment variables, optionally seeded from a local
``.env`` file so the project runs without exporting anything in the shell.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROVIDER_CHOICES = ("auto", "openai", "ollama", "demo")

DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"


def load_dotenv(path: Optional[Path] = None) -> None:
    """Minimal ``.env`` reader.

    Existing environment variables win, so ``.env`` never silently overrides an
    explicit ``export`` in the shell. Supports ``KEY=VALUE``, ``#`` comments and
    optional surrounding quotes.
    """
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the runtime configuration."""

    provider: str
    api_base: str
    api_key: str
    model: str
    ollama_base: str
    ollama_model: str
    host: str
    port: int
    temperature: float
    max_tokens: int
    timeout: float
    history_limit: int
    db_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        provider = _env("GROKBOT_PROVIDER", "auto").lower()
        if provider not in PROVIDER_CHOICES:
            provider = "auto"

        db_path = Path(_env("GROKBOT_DB", str(PROJECT_ROOT / "data" / "grokbot.db")))
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path

        return cls(
            provider=provider,
            api_base=_env("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE),
            api_key=_env("OPENAI_API_KEY"),
            model=_env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
            ollama_base=_env("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE),
            ollama_model=_env("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
            host=_env("GROKBOT_HOST", "127.0.0.1"),
            port=_env_int("GROKBOT_PORT", 8000),
            temperature=_env_float("GROKBOT_TEMPERATURE", 0.7),
            max_tokens=_env_int("GROKBOT_MAX_TOKENS", 1024),
            timeout=_env_float("GROKBOT_TIMEOUT", 120.0),
            history_limit=_env_int("GROKBOT_HISTORY_LIMIT", 20),
            db_path=db_path,
        )
