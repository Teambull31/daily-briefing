"""Client minimal pour Ollama (LLM local et gratuit)."""

from __future__ import annotations

import json
import re
from datetime import datetime

import httpx

SYSTEM_PROMPT = (
    "Tu es Jarvis, l'assistant personnel de ton utilisateur. Tu réponds en français, "
    "de façon concise et utile, formaté pour un écran de téléphone."
)

ROUTER_PROMPT = """Classe la demande suivante.
- "task" : il faut AGIR sur l'ordinateur (écrire/modifier du code, créer un projet ou des fichiers,
  lancer des commandes, installer, analyser un dossier, automatiser quelque chose).
- "reminder" : l'utilisateur veut qu'on lui rappelle quelque chose à un moment donné.
- "remember" : l'utilisateur donne une information sur lui à retenir durablement
  (préférence, fait personnel, contexte de ses projets).
- "web" : une question qui demande des informations récentes ou précises à chercher sur internet
  (actualité, prix, horaires, résultats sportifs, sortie d'un produit, météo ailleurs, documentation).
- "chat" : une simple question, une explication, une conversation, un conseil.
Réponds UNIQUEMENT en JSON, par exemple {"action": "chat"}.

Demande : """

ACTIONS = ("task", "reminder", "remember", "web", "chat")

WHEN_PROMPT = """Nous sommes le {now} ({weekday}). Extrais le rappel demandé ci-dessous.
Réponds UNIQUEMENT en JSON : {{"datetime": "AAAA-MM-JJTHH:MM", "texte": "ce qu'il faut rappeler"}}.
Sans heure précisée, utilise 09:00. Si la date est impossible à déterminer : {{"datetime": null}}.

Demande : {text}"""

WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


class Ollama:
    def __init__(self, base_url: str, model: str, timeout: float = 600.0, think: bool = False):
        self.base_url = base_url
        self.model = model
        # Les modèles "qui réfléchissent" (qwen3...) répondent bien plus vite sans cette phase.
        self.think: bool | None = think
        self._client = httpx.AsyncClient(timeout=timeout)

    async def chat(self, messages: list[dict], *, json_mode: bool = False) -> str:
        payload: dict = {"model": self.model, "messages": messages, "stream": False}
        if json_mode:
            payload["format"] = "json"
        if self.think is not None:
            payload["think"] = self.think
        r = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        if r.status_code == 400 and "think" in payload and "think" in r.text.lower():
            # Ollama trop ancien ou modèle sans option de réflexion : on n'envoie plus le paramètre.
            self.think = None
            del payload["think"]
            r = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        r.raise_for_status()
        return strip_thinking(r.json()["message"]["content"])

    async def ask(self, prompt: str, history: list[dict] | None = None, context: str = "") -> str:
        system = SYSTEM_PROMPT + (f"\n\n{context}" if context else "")
        messages = [{"role": "system", "content": system}, *(history or [])]
        messages.append({"role": "user", "content": prompt})
        return await self.chat(messages)

    async def route(self, prompt: str) -> str:
        """Retourne une des ACTIONS. En cas de doute ou d'erreur : "chat" (le plus sûr)."""
        try:
            raw = await self.chat([{"role": "user", "content": ROUTER_PROMPT + prompt}], json_mode=True)
            action = json.loads(raw).get("action")
        except (httpx.HTTPError, ValueError, AttributeError):
            return "chat"
        return action if action in ACTIONS else "chat"

    async def parse_when(self, text: str, now: datetime) -> tuple[datetime, str] | None:
        """Comprend une date libre (« lundi prochain », « le 3 à midi »…). None si impossible."""
        prompt = WHEN_PROMPT.format(now=now.strftime("%Y-%m-%d %H:%M"), weekday=WEEKDAYS[now.weekday()], text=text)
        try:
            data = json.loads(await self.chat([{"role": "user", "content": prompt}], json_mode=True))
            when = datetime.fromisoformat(data["datetime"]).replace(tzinfo=now.tzinfo)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return None
        what = str(data.get("texte") or "").strip() or "(rappel)"
        return (when, what) if when > now else None

    async def aclose(self) -> None:
        await self._client.aclose()
