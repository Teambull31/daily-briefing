"""Coach de productivité : liste de tâches, sessions de concentration (pomodoro), bilans,
et messages vocaux pour les rappels. Mélangé dans la classe Jarvis (voir bot.py)."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .memory import parse_focus

log = logging.getLogger("jarvis.coach")

VOICE_MODES = ("off", "rappels", "tout")
TODO_PREFIX = re.compile(
    r"^\s*(?:todo\s*:|à faire\s*:|a faire\s*:"
    r"|ajoute(?:r)?\s+(?:à|a|dans)\s+ma\s+liste(?:\s+de\s+\w+(?:\s+à\s+faire)?)?\s*:?)\s*",
    re.I,
)
TODO_LIST_WORDS = re.compile(r"\s*(?:à|a|dans)\s+ma\s+liste(?:\s+de\s+(?:tâches|choses\s+à\s+faire|todo))?\s*", re.I)
TODO_VERB = re.compile(r"^\s*(?:ajoute(?:r)?|mets?|note)\s+", re.I)
FOCUS_PREFIX = re.compile(r"^\s*(?:focus|pomodoro|je\s+(?:me\s+)?concentre)\b", re.I)


def extract_todo(text: str) -> str:
    """« ajoute acheter du pain à ma liste » -> « acheter du pain »."""
    text = TODO_PREFIX.sub("", text, count=1)
    text = TODO_LIST_WORDS.sub(" ", text)
    text = TODO_VERB.sub("", text, count=1)
    return re.sub(r"\s+", " ", text).strip(" .:")


def fmt_minutes(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


class CoachMixin:
    """Nécessite : self.cfg, self.store, self.llm, self.tasks, self.speaker, self.now(), self.reply()."""

    # ---------- voix ----------

    @property
    def voice_mode(self) -> str:
        mode = self.store.settings.get("voice", self.cfg.voice_mode)
        return mode if mode in VOICE_MODES else "rappels"

    async def notify(self, bot, chat_id: int, text: str, *, spoken: bool = True, silent: bool = False) -> None:
        """Envoie un message ; en mode vocal, aussi en message vocal (et sur les haut-parleurs du PC)."""
        await bot.send_message(chat_id, text, disable_notification=silent)
        if spoken and self.voice_mode != "off":
            await self.send_voice(bot, chat_id, text, silent=True)  # la notification vient déjà du texte
        if spoken and self.cfg.pc_audio and self.speaker:
            try:
                await self.speaker.play_on_pc(text)
            except Exception:
                log.exception("Lecture sur le PC impossible")

    async def send_voice(self, bot, chat_id: int, text: str, silent: bool = False) -> None:
        if not self.speaker:
            return
        try:
            result = await self.speaker.speak(text)
            if not result:
                return
            audio, fmt = result
            if fmt == "ogg":
                await bot.send_voice(chat_id, audio, disable_notification=silent)
            else:
                await bot.send_audio(chat_id, audio, filename="jarvis.wav", disable_notification=silent)
        except Exception:  # la voix est un bonus : ne jamais bloquer le message texte
            log.exception("Message vocal impossible")

    async def cmd_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.args and context.args[0].lower() in VOICE_MODES:
            self.store.set_setting("voice", context.args[0].lower())
        extra = "" if self.speaker else "\n⚠️ Piper n'est pas installé : pip install piper-tts (voir README)."
        await self.reply(
            update,
            f"🔊 Mode vocal : {self.voice_mode}{extra}\n\n"
            "/voix off — jamais\n"
            "/voix rappels — rappels, focus, briefing, bilan\n"
            "/voix tout — aussi mes réponses",
        )

    # ---------- liste de tâches ----------

    def todo_lines(self, limit: int = 20) -> list[str]:
        pending = self.store.pending_todos
        lines = [f"{i}. {t['text']}" for i, t in enumerate(pending[:limit], 1)]
        if len(pending) > limit:
            lines.append(f"… et {len(pending) - limit} autres")
        return lines

    async def add_todos(self, update: Update, text: str) -> None:
        items = [extract_todo(line) for line in text.splitlines()]
        items = [i for i in items if i]
        if not items:
            await self.reply(update, "Usage : /todo <chose à faire> (une par ligne ; « ! » devant = urgent)")
            return
        for item in items:
            urgent = item.startswith("!")
            self.store.add_todo(item.lstrip("! "), urgent=urgent, now=self.now())
        count = len(self.store.pending_todos)
        added = items[0].lstrip("! ") if len(items) == 1 else f"{len(items)} tâches"
        hint = "/todos pour la liste, /next pour démarrer."
        await self.reply(update, f"📝 Ajouté : {added}\n{count} en attente — {hint}")

    async def cmd_todo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # On relit le message brut pour garder les retours à la ligne (plusieurs tâches d'un coup).
        raw = update.effective_message.text or ""
        await self.add_todos(update, raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else "")

    async def cmd_todos(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lines = self.todo_lines()
        if not lines:
            await self.reply(update, "🎉 Rien en attente. Ajoute avec /todo <chose à faire>.")
            return
        done_today = len(self.store.done_on(self.now().date()))
        await self.reply(
            update,
            "📝 À faire :\n" + "\n".join(lines)
            + f"\n\n✅ Faites aujourd'hui : {done_today}"
            + "\n/fait <n> · /retire <n> · /next · /decoupe <gros objectif>",
        )

    async def cmd_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        number = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
        todo = self.store.pending_by_number(number)
        if not todo:
            await self.reply(update, "Rien à cocher (voir /todos).")
            return
        self.store.complete_todo(todo, self.now())
        done_today = len(self.store.done_on(self.now().date()))
        following = self.store.pending_todos
        text = f"✅ Bravo ! « {todo['text']} » est fait. ({done_today} aujourd'hui)"
        text += f"\n👉 Ensuite : {following[0]['text']}" if following else "\n🎉 Liste vide, beau travail !"
        await self.reply(update, text)

    async def cmd_remove_todo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        number = int(context.args[0]) if context.args and context.args[0].isdigit() else 0
        todo = self.store.pending_by_number(number)
        if not todo:
            await self.reply(update, "Usage : /retire <n> (voir /todos)")
            return
        self.store.remove_todo(todo)
        await self.reply(update, f"🗑 Retiré : {todo['text']}")

    async def cmd_next(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pending = self.store.pending_todos
        if not pending:
            await self.reply(update, "🎉 Rien en attente. Ajoute avec /todo, ou découpe un objectif avec /decoupe.")
            return
        await self.reply(
            update,
            f"👉 Une seule chose maintenant : {pending[0]['text']}\n\n"
            f"/focus pour t'y mettre {self.cfg.focus_minutes} min · /fait quand c'est fini",
        )

    async def cmd_breakdown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        goal = " ".join(context.args)
        if not goal:
            await self.reply(update, "Usage : /decoupe <gros objectif> — je le transforme en petites étapes.")
            return
        await update.effective_chat.send_action("typing")
        steps = await self.llm.breakdown(goal)
        if not steps:
            await self.reply(update, "⚠️ Je n'ai pas réussi à découper cet objectif (IA locale indisponible ?).")
            return
        for step in steps:
            self.store.add_todo(step, now=self.now())
        lines = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        intro = f"🧩 « {goal} » en petites étapes (ajoutées à ta liste) :"
        await self.reply(update, f"{intro}\n{lines}\n\n/next pour démarrer.")

    # ---------- concentration ----------

    def focus_active(self) -> bool:
        session = self.store.focus
        return bool(session and session.get("phase") == "focus")

    async def start_focus(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        session = self.store.focus
        if session:
            left = max(0, int((datetime.fromisoformat(session["end"]) - self.now()).total_seconds() // 60))
            what = "concentration" if session["phase"] == "focus" else "pause"
            await self.reply(update, f"⏱ Déjà en {what} ({left} min restantes). /stopfocus pour arrêter.")
            return
        minutes, rounds, topic = parse_focus(text, self.cfg.focus_minutes)
        if not topic and self.store.pending_todos:
            topic = self.store.pending_todos[0]["text"]  # par défaut : la prochaine chose à faire
        now = self.now()
        session = {
            "chat_id": update.effective_chat.id, "phase": "focus", "topic": topic,
            "minutes": minutes, "rounds_left": rounds, "round": 1, "rounds": rounds,
            "session_start": now.isoformat(), "end": (now + timedelta(minutes=minutes)).isoformat(),
        }
        self.store.set_focus(session)
        self._schedule_focus(context.job_queue, session)
        cycles = f" ({rounds} sessions)" if rounds > 1 else ""
        about = f" sur « {topic} »" if topic else ""
        await self.reply(
            update,
            f"🎯 C'est parti : {minutes} min{about}{cycles}. Fin à {self._at(session['end'])}.\n"
            "🔕 Mes notifications de tâches sont silencieuses pendant ce temps. /stopfocus pour arrêter.",
        )

    def _at(self, iso: str) -> str:
        return f"{datetime.fromisoformat(iso):%H:%M}"

    def _schedule_focus(self, job_queue, session: dict) -> None:
        for job in job_queue.get_jobs_by_name("focus"):
            job.schedule_removal()
        job_queue.run_once(self._focus_tick, when=datetime.fromisoformat(session["end"]), name="focus")

    async def _focus_tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        session = self.store.focus
        if not session:
            return
        now, chat_id = self.now(), session["chat_id"]
        about = f" sur « {session['topic']} »" if session["topic"] else ""
        brk = self.cfg.break_minutes
        if session["phase"] == "focus":
            start = datetime.fromisoformat(session["end"]) - timedelta(minutes=session["minutes"])
            self.store.log_focus(start, session["minutes"], session["topic"], completed=True)
            session["rounds_left"] -= 1
            n_today, min_today = self.store.focus_minutes_on(now.date())
            if session["rounds_left"] > 0:
                session["phase"], session["end"] = "break", (now + timedelta(minutes=brk)).isoformat()
                text = (f"⏱ Session {session['round']}/{session['rounds']} terminée{about}. "
                        f"Pause de {brk} minutes, lève-toi et bois un verre d'eau !")
            else:
                session["phase"], session["end"] = "last_break", (now + timedelta(minutes=brk)).isoformat()
                text = (f"⏱ Session terminée{about}. Bravo ! Aujourd'hui : {n_today} session(s), "
                        f"{fmt_minutes(min_today)} de concentration. Pause de {brk} minutes.")
                pending = self.store.pending_todos
                if session["topic"] and pending and pending[0]["text"] == session["topic"]:
                    text += "\nC'est fini ? /fait pour le cocher."
        elif session["phase"] == "break":
            session["round"] += 1
            session["phase"] = "focus"
            session["end"] = (now + timedelta(minutes=session["minutes"])).isoformat()
            text = (f"🎯 Pause terminée : session {session['round']}/{session['rounds']}, "
                    f"{session['minutes']} minutes{about}. C'est reparti !")
        else:  # fin de la dernière pause
            self.store.set_focus(None)
            nxt = self.store.pending_todos
            text = "☕ Pause terminée. " + (
                f"Prochaine étape : {nxt[0]['text']}. /focus pour t'y remettre." if nxt else "/focus pour enchaîner ?"
            )
            await self.notify(context.bot, chat_id, text)
            return
        self.store.set_focus(session)
        self._schedule_focus(context.job_queue, session)
        await self.notify(context.bot, chat_id, text)

    async def cmd_focus(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.start_focus(update, context, " ".join(context.args))

    async def cmd_stop_focus(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        session = self.store.focus
        if not session:
            await self.reply(update, "Aucune session en cours. /focus pour en lancer une.")
            return
        for job in context.job_queue.get_jobs_by_name("focus"):
            job.schedule_removal()
        if session["phase"] == "focus":  # on compte quand même le temps passé
            start = datetime.fromisoformat(session["end"]) - timedelta(minutes=session["minutes"])
            done = int((self.now() - start).total_seconds() // 60)
            if done >= 1:
                self.store.log_focus(start, done, session["topic"], completed=False)
        self.store.set_focus(None)
        await self.reply(update, "⏹ Session arrêtée. Chaque minute compte : /bilan pour voir ta journée.")

    def restore_focus(self, job_queue) -> None:
        """Au démarrage : reprend la session en cours (ou déclenche tout de suite l'étape dépassée)."""
        session = self.store.focus
        if session:
            end = max(datetime.fromisoformat(session["end"]), self.now() + timedelta(seconds=5))
            job_queue.run_once(self._focus_tick, when=end, name="focus")

    # ---------- bilans ----------

    def daily_summary(self, day: date) -> str:
        done = self.store.done_on(day)
        n_sessions, minutes = self.store.focus_minutes_on(day)
        agent_tasks = [
            t for t in self.tasks.tasks.values()
            if t.status == "terminée" and t.finished and datetime.fromtimestamp(t.finished).date() == day
        ]
        lines = [f"✅ Tâches cochées : {len(done)}"]
        lines += [f"   • {t['text']}" for t in done[:10]]
        lines.append(f"🎯 Concentration : {n_sessions} session(s), {fmt_minutes(minutes)}")
        if agent_tasks:
            lines.append(f"🛠 Tâches réalisées par l'agent : {len(agent_tasks)}")
        pending = self.todo_lines(limit=3)
        if pending:
            lines.append("📝 Reste en tête de liste :")
            lines += [f"   {line}" for line in pending]
        return "\n".join(lines)

    def agenda(self) -> str:
        """Pour le briefing du matin : la liste du jour et les rappels d'aujourd'hui."""
        today = self.now().date()
        parts = []
        todos = self.todo_lines(limit=5)
        if todos:
            parts.append("📝 À faire aujourd'hui :\n" + "\n".join(todos))
        todays = [r for r in self.store.reminders if datetime.fromisoformat(r["when"]).date() == today]
        if todays:
            parts.append("⏰ Rappels du jour :\n" + "\n".join(
                f"{self._at(r['when'])} — {r['text']}" for r in todays
            ))
        n, minutes = self.store.focus_minutes_on(today - timedelta(days=1))
        if n:
            parts.append(f"🎯 Hier : {n} session(s) de concentration, {fmt_minutes(minutes)}.")
        return "\n\n".join(parts)

    async def cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, "📊 Ta journée\n\n" + self.daily_summary(self.now().date()))

    async def evening_review(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = self.now()
        self.store.purge_old_done(now - timedelta(days=30))
        text = (
            "🌙 Bilan du jour\n\n" + self.daily_summary(now.date())
            + "\n\nPar quoi commences-tu demain ? Réponds « /todo ! … » pour le mettre en tête de liste."
        )
        for user_id in self.cfg.allowed_user_ids:
            try:
                await self.notify(context.bot, user_id, text)
            except TelegramError:
                log.warning("Bilan non envoyé à %s", user_id)
