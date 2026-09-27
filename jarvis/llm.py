"""Client minimal pour Ollama (LLM local et gratuit)."""

from __future__ import annotations

import json
import re

import httpx

SYSTEM_PROMPT = (
    "Tu es Jarvis, l'assistant personnel de ton utilisateur. Tu réponds en français, "
    "de façon concise et utile, formaté pour un écran de téléphone."
)

ROUTER_PROMPT = """Classe la demande suivante.
- "task" : il faut AGIR sur l'ordinateur (écrire/modifier du code, créer un projet ou des fichiers,
  lancer des commandes, installer, analyser un dossier, automatiser quelque chose).
- "chat" : une simple question, une explication, une conversation, un conseil.
Réponds UNIQUEMENT en JSON : {"action": "task"} ou {"action": "chat"}.

Demande : """

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


class Ollama:
    def __init__(self, base_url: str, model: str, timeout: float = 600.0):
        self.base_url = base_url
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    async def chat(self, messages: list[dict], *, json_mode: bool = False) -> str:
        payload: dict = {"model": self.model, "messages": messages, "stream": False}
        if json_mode:
            payload["format"] = "json"
        r = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        r.raise_for_status()
        return strip_thinking(r.json()["message"]["content"])

    async def ask(self, prompt: str, history: list[dict] | None = None) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *(history or [])]
        messages.append({"role": "user", "content": prompt})
        return await self.chat(messages)

    async def route(self, prompt: str) -> str:
        """Retourne "task" ou "chat". En cas de doute ou d'erreur : "chat" (le plus sûr)."""
        try:
            raw = await self.chat([{"role": "user", "content": ROUTER_PROMPT + prompt}], json_mode=True)
            action = json.loads(raw).get("action")
        except (httpx.HTTPError, ValueError, AttributeError):
            return "chat"
        return action if action in ("task", "chat") else "chat"

    async def aclose(self) -> None:
        await self._client.aclose()
