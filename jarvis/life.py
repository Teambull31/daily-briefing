"""Habitudes (séries 🔥), relances quand ça n'avance pas, statistiques en graphique,
et agenda (rendez-vous, rappels avant événement, créneaux de focus). Mélangé dans Jarvis."""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .calendar_ics import fmt_event, free_slots, make_ics
from .stats import collect, render_png, summary_text

log = logging.getLogger("jarvis.life")

NUDGES = [
    "👋 Où en es-tu sur « {todo} » ?",
    "🔔 Petit point : « {todo} » attend toujours.",
    "💪 Et si tu avançais {minutes} minutes sur « {todo} », juste pour lancer la machine ?",
    "🧭 « {todo} » est toujours en tête de liste. On s'y remet ?",
]
NUDGE_HELP = (
    "/focus pour t'y mettre · /fait si c'est fini · /decoupe si c'est trop gros · /plustard pour la repousser"
)
HABIT_CLAIM = re.compile(r"^\s*j['’]?\s*ai\s+", re.I)
STREAK_MILESTONES = {
    3: "🔥", 7: "🏅 Une semaine !", 14: "🏅 Deux semaines !", 30: "🏆 Un mois !", 100: "👑 100 jours !",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


class LifeMixin:
    """Nécessite : self.cfg, self.store, self.tasks, self.calendar, self.now(), self.reply(), self.notify()."""

    # ---------- relances ----------

    def mark_progress(self) -> None:
        self.store.set_setting("last_progress", self.now().isoformat())

    def in_work_hours(self, when: datetime) -> bool:
        (d1, d2), (h1, h2) = self.cfg.work_days, self.cfg.work_hours
        return d1 <= when.isoweekday() <= d2 and h1 <= when.hour < h2

    async def nudge_check(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toutes les 15 min : relance si la tâche prioritaire n'a pas bougé depuis NUDGE_HOURS."""
        now = self.now()
        pending = self.store.pending_todos
        if not self.cfg.nudge_hours or not pending or self.store.focus or not self.in_work_hours(now):
            return
        settings = self.store.settings
        marks = [settings.get(k) for k in ("last_progress", "last_nudge")]
        last = max((datetime.fromisoformat(m) for m in marks if m), default=None)
        if last is None:  # premier passage : on commence à compter
            self.mark_progress()
            return
        if now - last < timedelta(hours=self.cfg.nudge_hours):
            return
        if self.calendar.enabled:  # pas de relance en plein rendez-vous
            ongoing = await self.calendar.events(now, now + timedelta(minutes=1))
            if any(not ev.all_day for ev in ongoing):
                return
        count = settings.get("nudge_count", 0)
        text = NUDGES[count % len(NUDGES)].format(todo=pending[0]["text"], minutes=self.cfg.focus_minutes)
        self.store.set_setting("nudge_count", count + 1)
        self.store.set_setting("last_nudge", now.isoformat())
        for user_id in self.cfg.allowed_user_ids:
            try:
                await self.notify(context.bot, user_id, f"{text}\n\n{NUDGE_HELP}")
            except TelegramError:
                log.warning("Relance non envoyée à %s", user_id)

    async def cmd_postpone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pending = self.store.pending_todos
        if not pending:
            await self.reply(update, "Rien à repousser.")
            return
        todo = pending[0]
        self.store.todos.remove(todo)
        self.store.todos.append(todo)
        self.store.save()
        self.mark_progress()  # les relances repartent de zéro
        nxt = self.store.pending_todos[0]["text"]
        await self.reply(update, f"⏭ « {todo['text']} » passe en fin de liste.\n👉 Maintenant : {nxt}")

    # ---------- habitudes ----------

    def habit_lines(self) -> list[str]:
        today = self.now().date()
        lines = []
        for i, h in enumerate(self.store.habits, 1):
            done = "✅" if self.store.habit_done_on(h, today) else "⬜"
            streak = self.store.streak(h, today)
            lines.append(f"{i}. {done} {h['name']}" + (f" — 🔥 {streak} j" if streak else ""))
        return lines

    def habit_summary_lines(self, day: date) -> list[str]:
        if not self.store.habits:
            return []
        missing = [h["name"] for h in self.store.habits if not self.store.habit_done_on(h, day)]
        done = len(self.store.habits) - len(missing)
        line = f"🔁 Habitudes : {done}/{len(self.store.habits)}"
        return [line + (f" (reste : {', '.join(missing)} — /check)" if missing else " 🎉")]

    def _find_habit(self, ref: str) -> dict | None:
        if ref.isdigit():
            i = int(ref)
            return self.store.habits[i - 1] if 1 <= i <= len(self.store.habits) else None
        ref = normalize(ref)
        return next((h for h in self.store.habits if normalize(h["name"]) in ref or ref in normalize(h["name"])), None)

    def match_habit_claim(self, text: str) -> dict | None:
        """« j'ai fait du sport » / « j'ai lu 20 pages » -> l'habitude correspondante, si elle existe."""
        if not HABIT_CLAIM.match(text):
            return None
        said = normalize(text)
        return next((h for h in self.store.habits if normalize(h["name"]) in said), None)

    async def check_habit(self, update: Update, habit: dict) -> None:
        today = self.now().date()
        fresh = self.store.check_habit(habit, today)
        streak = self.store.streak(habit, today)
        if not fresh:
            await self.reply(update, f"✅ « {habit['name']} » était déjà coché aujourd'hui (🔥 {streak} j).")
            return
        cheer = STREAK_MILESTONES.get(streak, "")
        await self.reply(update, f"✅ {habit['name']} : 🔥 {streak} jour(s) d'affilée ! {cheer}".strip())

    async def cmd_habit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        name = " ".join(context.args).strip()
        if not name:
            await self.reply(update, "Usage : /habitude sport (puis /check sport, ou dis « j'ai fait du sport »)")
            return
        self.store.add_habit(name, self.now().date())
        await self.reply(update, f"🔁 Nouvelle habitude : {name}. Coche-la chaque jour avec /check {name}.")

    async def cmd_habits(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lines = self.habit_lines()
        if not lines:
            await self.reply(update, "Aucune habitude suivie. Exemple : /habitude sport")
            return
        footer = "\n\n/check <n> · /suppr_habitude <n>"
        await self.reply(update, "🔁 Habitudes du jour :\n" + "\n".join(lines) + footer)

    async def cmd_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ref = " ".join(context.args).strip()
        habit = self._find_habit(ref) if ref else None
        if not habit:
            await self.reply(update, "Laquelle ?\n" + ("\n".join(self.habit_lines()) or "Aucune habitude (/habitude)."))
            return
        await self.check_habit(update, habit)

    async def cmd_remove_habit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        habit = self._find_habit(" ".join(context.args).strip()) if context.args else None
        if not habit:
            await self.reply(update, "Usage : /suppr_habitude <n> (voir /habitudes)")
            return
        self.store.remove_habit(habit)
        await self.reply(update, f"🗑 Habitude supprimée : {habit['name']}")

    # ---------- statistiques ----------

    async def send_stats(self, bot, chat_id: int, days: int = 7) -> None:
        rows = collect(self.store, self.tasks.tasks, self.now().date(), days)
        title = "Ta semaine" if days == 7 else f"Tes {days} derniers jours"
        text = f"📊 {title}\n\n{summary_text(rows)}"
        png = render_png(rows, title)
        if png:
            await bot.send_photo(chat_id, png, caption=text[:1024])
        else:
            await bot.send_message(chat_id, text + "\n\n(installe matplotlib pour le graphique)")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = int(context.args[0]) if context.args and context.args[0].isdigit() else 7
        await update.effective_chat.send_action("upload_photo")
        await self.send_stats(context.bot, update.effective_chat.id, max(3, min(90, days)))

    # ---------- agenda ----------

    async def calendar_lines(self, day: date) -> list[str]:
        try:
            return [fmt_event(ev) for ev in await self.calendar.day(day)]
        except Exception:
            log.exception("Lecture de l'agenda impossible")
            return []

    async def agenda(self) -> str:
        """Pour le briefing du matin : rendez-vous, liste du jour, rappels, habitudes."""
        parts = []
        if self.calendar.enabled:
            events = await self.calendar_lines(self.now().date())
            parts.append("📅 Agenda du jour :\n" + ("\n".join(events) if events else "rien de prévu"))
        todo_part = self.todo_agenda()
        if todo_part:
            parts.append(todo_part)
        if self.store.habits:
            parts.append("🔁 Habitudes :\n" + "\n".join(self.habit_lines()))
        return "\n\n".join(parts)

    async def cmd_agenda(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.calendar.enabled:
            await self.reply(
                update,
                "📅 Aucun agenda connecté. Dans Google Agenda (sur ordinateur) : Paramètres → ton agenda → "
                "« Adresse secrète au format iCal ». Copie-la dans CALENDAR_ICS_URLS (.env) puis redémarre Jarvis.",
            )
            return
        tomorrow = bool(context.args) and normalize(context.args[0]).startswith("demain")
        day = self.now().date() + timedelta(days=1 if tomorrow else 0)
        events = await self.calendar_lines(day)
        label = "Demain" if tomorrow else "Aujourd'hui"
        await self.reply(update, f"📅 {label} :\n" + ("\n".join(events) if events else "rien de prévu 🎉"))

    async def event_reminder_check(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toutes les 5 min : prévient EVENT_REMINDER_MINUTES avant chaque rendez-vous."""
        now = self.now()
        lead = timedelta(minutes=self.cfg.event_reminder_minutes)
        try:
            upcoming = await self.calendar.events(now, now + lead + timedelta(minutes=5))
        except Exception:
            log.exception("Lecture de l'agenda impossible")
            return
        notified = self.store.settings.get("notified_events", [])
        for ev in upcoming:
            if ev.all_day or ev.start <= now or ev.start - now > lead or ev.key in notified:
                continue
            minutes = max(1, round((ev.start - now).total_seconds() / 60))
            where = f" ({ev.location})" if ev.location else ""
            for user_id in self.cfg.allowed_user_ids:
                try:
                    await self.notify(context.bot, user_id, f"📅 Dans {minutes} min : {ev.title}{where}")
                except TelegramError:
                    log.warning("Rappel d'événement non envoyé à %s", user_id)
            notified = (notified + [ev.key])[-200:]
            self.store.set_setting("notified_events", notified)

    def _work_window(self, day: date, not_before: datetime) -> tuple[datetime, datetime] | None:
        (d1, d2), (h1, h2) = self.cfg.work_days, self.cfg.work_hours
        if not d1 <= day.isoweekday() <= d2:
            return None
        start = datetime(day.year, day.month, day.day, h1, tzinfo=self.tz)
        end = start.replace(hour=h2)
        start = max(start, not_before)
        return (start, end) if start < end else None

    async def find_focus_slot(self, minutes: int) -> tuple[datetime, datetime] | None:
        """Premier créneau libre de `minutes` dans les heures de travail, sur les 7 prochains jours."""
        now = self.now()
        not_before = (now + timedelta(minutes=15 - now.minute % 15)).replace(second=0, microsecond=0)
        for offset in range(8):
            day = now.date() + timedelta(days=offset)
            window = self._work_window(day, not_before)
            if not window:
                continue
            busy = await self.calendar.events(*window) if self.calendar.enabled else []
            slots = free_slots(busy, window[0], window[1], minutes)
            if slots:
                return slots[0][0], slots[0][0] + timedelta(minutes=minutes)
        return None

    async def cmd_block(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        default = 2 * self.cfg.focus_minutes
        minutes = int(context.args[0]) if context.args and context.args[0].isdigit() else default
        minutes = max(15, min(240, minutes))
        slot = await self.find_focus_slot(minutes)
        if not slot:
            await self.reply(update, "😕 Aucun créneau libre trouvé cette semaine dans tes heures de travail.")
            return
        start, end = slot
        pending = self.store.pending_todos
        topic = pending[0]["text"] if pending else "travail concentré"
        ics = make_ics(f"🎯 Focus : {topic}", start, end, "Créneau bloqué par Jarvis. Lance /focus à l'heure.")
        caption = f"🎯 Créneau trouvé : {self._fmt_slot(start, end)}. Touche le fichier pour l'ajouter à ton agenda."
        if not self.calendar.enabled:
            caption += "\n(Aucun agenda connecté : je ne vois pas tes rendez-vous.)"
        await update.effective_message.reply_document(ics, filename="focus.ics", caption=caption)
        reminder = self.store.add_reminder(
            update.effective_chat.id, start, f"C'est l'heure de ton créneau de focus : {topic}. Lance /focus {minutes}"
        )
        self._schedule_reminder(context.job_queue, reminder)

    def _fmt_slot(self, start: datetime, end: datetime) -> str:
        from .llm import WEEKDAYS

        return f"{WEEKDAYS[start.weekday()]} {start:%d/%m} de {start:%H:%M} à {end:%H:%M}"
