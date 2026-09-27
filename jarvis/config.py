"""Configuration chargée depuis les variables d'environnement (ou un fichier .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_AGENT_CMD = "opencode run -m ollama/qwen3-coder:30b {prompt}"
DEFAULT_FEEDS = "https://www.lemonde.fr/rss/une.xml,https://www.francetvinfo.fr/titres.rss"


def load_dotenv(path: Path) -> None:
    """Charge un fichier .env minimaliste (KEY=VALUE) sans écraser l'environnement existant."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(";", ",").split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    ollama_url: str = "http://localhost:11434"
    chat_model: str = "qwen3:14b"
    agent_cmd: str = DEFAULT_AGENT_CMD
    workspace: Path = Path.home() / "jarvis-workspace"
    max_parallel_tasks: int = 1
    auto_route: bool = True
    whisper_model: str = "small"
    timezone: str = "Europe/Paris"
    briefing_time: str = ""  # "07:30" pour un briefing quotidien automatique, vide = désactivé
    latitude: float = 48.8566
    longitude: float = 2.3522
    city: str = "Paris"
    feeds: tuple[str, ...] = field(default_factory=lambda: tuple(DEFAULT_FEEDS.split(",")))

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        token = env.get("TELEGRAM_TOKEN", "").strip()
        if not token:
            raise SystemExit("TELEGRAM_TOKEN manquant (voir .env.example).")
        # Vide = mode configuration : le bot ne répond qu'à /id (voir bot.py).
        return cls(
            telegram_token=token,
            allowed_user_ids=_ids(env.get("ALLOWED_USER_IDS", "")),
            ollama_url=env.get("OLLAMA_URL", cls.ollama_url).rstrip("/"),
            chat_model=env.get("CHAT_MODEL", cls.chat_model),
            agent_cmd=env.get("AGENT_CMD", DEFAULT_AGENT_CMD),
            workspace=Path(env.get("WORKSPACE", str(cls.workspace))).expanduser(),
            max_parallel_tasks=max(1, int(env.get("MAX_PARALLEL_TASKS", "1"))),
            auto_route=env.get("AUTO_ROUTE", "1") not in ("0", "false", "no"),
            whisper_model=env.get("WHISPER_MODEL", cls.whisper_model),
            timezone=env.get("TIMEZONE", cls.timezone),
            briefing_time=env.get("BRIEFING_TIME", "").strip(),
            latitude=float(env.get("LATITUDE", cls.latitude)),
            longitude=float(env.get("LONGITUDE", cls.longitude)),
            city=env.get("CITY", cls.city),
            feeds=tuple(f.strip() for f in env.get("FEEDS", DEFAULT_FEEDS).split(",") if f.strip()),
        )
