"""Mémoire de Jarvis, stockée dans un fichier JSON : notes sur l'utilisateur, rappels,
liste de tâches (todos), sessions de concentration et réglages."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {
            "notes": [], "reminders": [], "next_id": 1,
            "todos": [], "focus": None, "focus_log": [], "settings": {}, "habits": [],
        }
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

    # ---------- liste de tâches ----------

    @property
    def todos(self) -> list[dict]:
        return self.data["todos"]

    @property
    def pending_todos(self) -> list[dict]:
        return [t for t in self.todos if not t["done"]]

    def add_todo(self, text: str, urgent: bool = False, now: datetime | None = None) -> dict:
        todo = {
            "id": self.data["next_id"], "text": text.strip(), "done": False,
            "created": (now or datetime.now()).isoformat(), "done_at": None,
        }
        self.data["next_id"] += 1
        if urgent:  # en tête de liste : ce sera le prochain /next
            self.todos.insert(0, todo)
        else:
            self.todos.append(todo)
        self.save()
        return todo

    def pending_by_number(self, number: int) -> dict | None:
        """Les todos sont affichés numérotés 1, 2, 3… dans l'ordre de la liste en attente."""
        pending = self.pending_todos
        return pending[number - 1] if 1 <= number <= len(pending) else None

    def complete_todo(self, todo: dict, now: datetime) -> None:
        todo["done"], todo["done_at"] = True, now.isoformat()
        self.save()

    def remove_todo(self, todo: dict) -> None:
        self.todos.remove(todo)
        self.save()

    def done_on(self, day) -> list[dict]:
        return [t for t in self.todos if t["done_at"] and datetime.fromisoformat(t["done_at"]).date() == day]

    def purge_old_done(self, before: datetime) -> None:
        """Garde la liste légère : on oublie les todos terminés depuis longtemps."""
        self.data["todos"] = [
            t for t in self.todos if not t["done_at"] or datetime.fromisoformat(t["done_at"]) >= before
        ]
        self.save()

    # ---------- concentration ----------

    @property
    def focus(self) -> dict | None:
        return self.data["focus"]

    def set_focus(self, session: dict | None) -> None:
        self.data["focus"] = session
        self.save()

    def log_focus(self, start: datetime, minutes: int, topic: str, completed: bool) -> None:
        self.data["focus_log"].append(
            {"start": start.isoformat(), "minutes": minutes, "topic": topic, "completed": completed}
        )
        self.data["focus_log"] = self.data["focus_log"][-500:]
        self.save()

    def focus_minutes_on(self, day) -> tuple[int, int]:
        """(nombre de sessions, minutes de concentration) pour un jour donné."""
        sessions = [f for f in self.data["focus_log"] if datetime.fromisoformat(f["start"]).date() == day]
        return len(sessions), sum(f["minutes"] for f in sessions)

    # ---------- habitudes ----------

    @property
    def habits(self) -> list[dict]:
        return self.data["habits"]

    def add_habit(self, name: str, today: date) -> dict:
        habit = {"id": self.data["next_id"], "name": name.strip(), "created": today.isoformat(), "log": []}
        self.data["next_id"] += 1
        self.habits.append(habit)
        self.save()
        return habit

    def remove_habit(self, habit: dict) -> None:
        self.habits.remove(habit)
        self.save()

    def check_habit(self, habit: dict, day: date) -> bool:
        """Coche l'habitude pour ce jour. False si c'était déjà fait."""
        if day.isoformat() in habit["log"]:
            return False
        habit["log"] = sorted(set(habit["log"]) | {day.isoformat()})[-400:]
        self.save()
        return True

    @staticmethod
    def habit_done_on(habit: dict, day: date) -> bool:
        return day.isoformat() in habit["log"]

    @staticmethod
    def streak(habit: dict, today: date) -> int:
        """Jours consécutifs jusqu'à aujourd'hui (ou hier, si pas encore fait aujourd'hui)."""
        done = set(habit["log"])
        day = today if today.isoformat() in done else today - timedelta(days=1)
        count = 0
        while day.isoformat() in done:
            count += 1
            day -= timedelta(days=1)
        return count

    def habits_rate_on(self, day: date) -> float | None:
        """Part des habitudes (existant ce jour-là) cochées ; None s'il n'y en avait aucune."""
        existing = [h for h in self.habits if date.fromisoformat(h["created"]) <= day]
        if not existing:
            return None
        return sum(self.habit_done_on(h, day) for h in existing) / len(existing)

    # ---------- réglages ----------

    @property
    def settings(self) -> dict:
        return self.data["settings"]

    def set_setting(self, key: str, value) -> None:
        self.settings[key] = value
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


# ---------- compréhension des rappels en français ----------

REMINDER_PREFIX = re.compile(r"^\s*(?:rappelle[- ]moi|rappel\s*:)\s*", re.I)
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


# ---------- sessions de concentration ----------

_FOCUS_MINUTES = re.compile(r"\b(\d{1,3})\s*(?:min(?:utes?)?|mn|m)?\b", re.I)
_FOCUS_ROUNDS = re.compile(r"\b[x×]\s*(\d{1,2})\b|\b(\d{1,2})\s*(?:sessions?|fois|pomodoros?)\b", re.I)
_FOCUS_WORDS = re.compile(
    r"^\s*(?:je\s+(?:me\s+)?(?:concentre|bosse|travaille)|concentration|focus|pomodoro|lance\s+(?:un\s+)?focus)"
    r"(?:\s+(?:pendant|de|sur|pour))?\s*",
    re.I,
)


def parse_focus(text: str, default_minutes: int = 25) -> tuple[int, int, str]:
    """« 45 min x2 sur le rapport » -> (45, 2, "le rapport"). Minutes bornées à 5–180."""
    body = _FOCUS_WORDS.sub("", text, count=1)
    rounds = 1
    if m := _FOCUS_ROUNDS.search(body):
        rounds = int(m.group(1) or m.group(2))
        body = body[: m.start()] + " " + body[m.end():]
    minutes = default_minutes
    if m := _FOCUS_MINUTES.search(body):
        minutes = int(m.group(1))
        body = body[: m.start()] + " " + body[m.end():]
    topic = re.sub(r"^(?:sur|pour|de|:|-)\s+", "", re.sub(r"\s+", " ", body).strip(), flags=re.I).strip(" .")
    return max(5, min(180, minutes)), max(1, min(12, rounds)), topic
