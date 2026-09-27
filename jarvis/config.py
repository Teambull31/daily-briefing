"""Configuration chargée depuis les variables d'environnement (ou un fichier .env)."""

from __future__ import annotations

import os
import re
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


AGENT_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
# Noms déjà pris par les commandes du bot : un agent ne peut pas s'appeler comme elles.
RESERVED_NAMES = frozenset(
    "start aide help ask do projet project taches tasks log stop get reset briefing id agent agents cmd "
    "relancer retry rappel rappels effacer_rappel note memoire oublie suite continue etat status".split()
)


def parse_agents(env: dict[str, str]) -> dict[str, str]:
    """Lit les agents déclarés en AGENT_<NOM>=commande (ex: AGENT_CLAUDE=claude -p {prompt}).

    Compatibilité : un simple AGENT_CMD devient l'agent "local".
    """
    agents = {}
    for key, value in env.items():
        if not key.startswith("AGENT_") or key == "AGENT_CMD" or not value.strip():
            continue
        name = key[len("AGENT_"):].lower()
        if not AGENT_NAME_RE.match(name) or name in RESERVED_NAMES:
            raise SystemExit(f"Nom d'agent invalide ou réservé : {key}")
        agents[name] = value.strip()
    if not agents:
        agents["local"] = env.get("AGENT_CMD", DEFAULT_AGENT_CMD)
    return agents


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    ollama_url: str = "http://localhost:11434"
    chat_model: str = "qwen3:14b"
    agents: dict[str, str] = field(default_factory=lambda: {"local": DEFAULT_AGENT_CMD})
    default_agent: str = "local"
    workspace: Path = Path.home() / "jarvis-workspace"
    max_parallel_tasks: int = 1
    auto_route: bool = True
    chat_think: bool = False
    progress_minutes: int = 5
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
        agents = parse_agents(dict(env))
        default_agent = env.get("DEFAULT_AGENT", "").strip().lower() or next(iter(agents))
        if default_agent not in agents:
            raise SystemExit(f"DEFAULT_AGENT={default_agent} ne correspond à aucun AGENT_<NOM>.")
        # ALLOWED_USER_IDS vide = mode configuration : le bot ne répond qu'à /id (voir bot.py).
        return cls(
            telegram_token=token,
            allowed_user_ids=_ids(env.get("ALLOWED_USER_IDS", "")),
            ollama_url=env.get("OLLAMA_URL", cls.ollama_url).rstrip("/"),
            chat_model=env.get("CHAT_MODEL", cls.chat_model),
            agents=agents,
            default_agent=default_agent,
            workspace=Path(env.get("WORKSPACE", str(cls.workspace))).expanduser(),
            max_parallel_tasks=max(1, int(env.get("MAX_PARALLEL_TASKS", "1"))),
            auto_route=env.get("AUTO_ROUTE", "1") not in ("0", "false", "no"),
            chat_think=env.get("CHAT_THINK", "0") in ("1", "true", "yes"),
            progress_minutes=max(0, int(env.get("PROGRESS_MINUTES", "5"))),
            whisper_model=env.get("WHISPER_MODEL", cls.whisper_model),
            timezone=env.get("TIMEZONE", cls.timezone),
            briefing_time=env.get("BRIEFING_TIME", "").strip(),
            latitude=float(env.get("LATITUDE", cls.latitude)),
            longitude=float(env.get("LONGITUDE", cls.longitude)),
            city=env.get("CITY", cls.city),
            feeds=tuple(f.strip() for f in env.get("FEEDS", DEFAULT_FEEDS).split(",") if f.strip()),
        )
