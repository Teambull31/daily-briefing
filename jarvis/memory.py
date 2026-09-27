"""Mémoire à long terme (notes sur l'utilisateur) et rappels, stockés dans un fichier JSON."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"notes": [], "reminders": [], "next_id": 1}
        if path.is_file():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                path.rename(path.with_suffix(".corrompu"))  # on garde une copie, on repart à zéro

    # ---------- notes ----------

    @property
    def notes(self) -> list[str]:
        return self.data["notes"]

    def add_note(self, text: str) -> None:
        self.notes.append(text.strip())
        self.save()

    def remove_note(self, index: int) -> str | None:
        if not 1 <= index <= len(self.notes):
            return None
        removed = self.notes.pop(index - 1)
        self.save()
        return removed

    # ---------- rappels ----------

    @property
    def reminders(self) -> list[dict]:
        return sorted(self.data["reminders"], key=lambda r: r["when"])

    def add_reminder(self, chat_id: int, when: datetime, text: str) -> dict:
        reminder = {"id": self.data["next_id"], "chat_id": chat_id, "when": when.isoformat(), "text": text}
        self.data["next_id"] += 1
        self.data["reminders"].append(reminder)
        self.save()
        return reminder

    def remove_reminder(self, reminder_id: int) -> bool:
        before = len(self.data["reminders"])
        self.data["reminders"] = [r for r in self.data["reminders"] if r["id"] != reminder_id]
        self.save()
        return len(self.data["reminders"]) < before

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


# ---------- compréhension des rappels en français ----------

REMINDER_PREFIX = re.compile(r"^\s*(?:rappelle[- ]moi|rappel\s*:?)\s*", re.I)
REMEMBER_PREFIX = re.compile(r"^\s*(?:souviens[- ]toi|retiens)\s*(?:que|qu'|:)?\s*", re.I)

_RELATIVE = re.compile(r"\bdans\s+(\d+)\s*(minutes?|mins?|m|heures?|h|jours?|j)\b", re.I)
_DAY = re.compile(r"\b(aujourd'hui|aujourd’hui|ce soir|demain|après[- ]demain)\b", re.I)
_TIME = re.compile(r"\bà\s*(\d{1,2})\s*(?:h|:)\s*(\d{2})?\b|\bà\s*(\d{1,2})\s*heures?\b", re.I)
_LEADING_JUNK = re.compile(r"^(?:de\s+|d'|d’|que\s+|qu'|qu’|pour\s+|[:,\-–]\s*)+", re.I)


def parse_reminder(text: str, now: datetime) -> tuple[datetime, str] | None:
    """Comprend « rappelle-moi dans 20 min de… », « … demain à 9h d'appeler… », « … à 18h30 … ».

    Retourne (date, texte du rappel) ou None si l'heure n'est pas reconnue (le LLM prend le relais).
    """
    body = REMINDER_PREFIX.sub("", text, count=1)
    when: datetime | None = None
    spans: list[tuple[int, int]] = []

    if m := _RELATIVE.search(body):
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = (
            timedelta(minutes=n) if unit.startswith("m")
            else timedelta(hours=n) if unit.startswith("h")
            else timedelta(days=n)
        )
        when = now + delta
        spans.append(m.span())
    else:
        day_m, time_m = _DAY.search(body), _TIME.search(body)
        if not day_m and not time_m:
            return None
        day_word = day_m.group(1).lower().replace("’", "'") if day_m else ""
        offset = {"demain": 1, "après-demain": 2, "après demain": 2}.get(day_word, 0)
        if time_m:
            hour = int(time_m.group(1) or time_m.group(3))
            minute = int(time_m.group(2) or 0)
            spans.append(time_m.span())
        else:
            hour, minute = (19, 0) if day_word == "ce soir" else (9, 0)
        if hour > 23 or minute > 59:
            return None
        if day_m:
            spans.append(day_m.span())
        when = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now and not day_m:  # « à 8h » alors qu'il est 10h : demain
            when += timedelta(days=1)
        if when <= now:
            return None

    for start, end in sorted(spans, reverse=True):
        body = body[:start] + " " + body[end:]
    what = _LEADING_JUNK.sub("", re.sub(r"\s+", " ", body).strip()).strip(" .!,")
    return when, what or "(rappel)"
