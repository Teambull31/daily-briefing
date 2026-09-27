"""Bot Telegram : l'interface téléphone de Jarvis."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from datetime import time as dtime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, PicklePersistence, filters

from . import voice
from .briefing import build_briefing
from .config import Config
from .llm import Ollama
from .tasks import ACTIVE, Task, TaskManager

log = logging.getLogger("jarvis")

TG_LIMIT = 4000
HISTORY_TURNS = 10
DEFAULT_PROJECT = "general"
PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

HELP = """🤖 Jarvis — tes commandes

Écris simplement ce que tu veux : je décide seul s'il faut répondre ou agir sur ton PC.

/do <tâche> — force une action (coder, créer, lancer…)
/agent [nom] — voir / changer l'agent qui exécute les tâches
/<agent> <tâche> — une tâche avec cet agent (ex : /claude …)
@<agent> <tâche> ou « Claude, … » — pareil, en texte ou à la voix
/ask <question> — force une simple réponse
/projet [nom] — change de projet (dossier de travail)
/taches — liste des tâches
/log <n> — dernières lignes d'une tâche
/stop <n> — arrête une tâche
/relancer <n> — relance une tâche (échouée, interrompue…)
/get <fichier> — t'envoie un fichier du projet
/briefing — météo + actus du jour
/reset — oublie la conversation
/id — ton identifiant Telegram

🎙 Les messages vocaux marchent aussi (si faster-whisper est installé)."""


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    text = text or "(vide)"
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def resolve_inside(base: Path, rel: str) -> Path | None:
    """Résout un chemin relatif en refusant de sortir du dossier du projet."""
    target = (base / rel).resolve()
    return target if target.is_relative_to(base.resolve()) else None


class Jarvis:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.llm = Ollama(cfg.ollama_url, cfg.chat_model, think=cfg.chat_think)
        self.tasks = TaskManager(cfg.agents, cfg.max_parallel_tasks, cfg.workspace / ".jarvis-logs")
        names = "|".join(re.escape(n) for n in cfg.agents)
        # "@claude fais ça", "claude: fais ça", "Claude, fais ça" (utile à la voix)
        self._agent_prefix = re.compile(rf"^\s*(?:@({names})\b[\s,:]*|({names})\s*[,:]\s*)(.+)$", re.I | re.S)
        cfg.workspace.mkdir(parents=True, exist_ok=True)

    # ---------- utilitaires ----------

    def authorized(self, handler):
        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            if not user or user.id not in self.cfg.allowed_user_ids:
                if update.effective_message:
                    await update.effective_message.reply_text(
                        "⛔ Non autorisé. Envoie /id et ajoute ton identifiant à ALLOWED_USER_IDS."
                    )
                return
            await handler(update, context)

        return wrapper

    def current_agent(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        agent = context.chat_data.get("agent")
        return agent if agent in self.cfg.agents else self.cfg.default_agent

    def project_dir(self, context: ContextTypes.DEFAULT_TYPE) -> Path:
        return self.cfg.workspace / context.chat_data.get("project", DEFAULT_PROJECT)

    @staticmethod
    async def reply(update: Update, text: str) -> None:
        for chunk in split_message(text):
            await update.effective_message.reply_text(chunk)

    # ---------- logique principale ----------

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        match = self._agent_prefix.match(text)
        if match:  # agent désigné explicitement : c'est forcément une tâche
            agent = (match.group(1) or match.group(2)).lower()
            await self.start_task(update, context, match.group(3).strip(), agent)
            return
        if self.cfg.auto_route:
            await update.effective_chat.send_action(ChatAction.TYPING)
            if await self.llm.route(text) == "task":
                await self.start_task(update, context, text)
                return
        await self.answer(update, context, text)

    async def answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        history: list[dict] = context.chat_data.setdefault("history", [])
        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            reply = await self.llm.ask(text, history)
        except httpx.HTTPError as exc:
            await self.reply(update, f"⚠️ Ollama ne répond pas ({exc.__class__.__name__}). Est-il lancé ?")
            return
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        del history[: -2 * HISTORY_TURNS]
        await self.reply(update, reply)

    async def start_task(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        agent: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        agent = agent or self.current_agent(context)
        cwd = cwd or self.project_dir(context)
        _ensure_git(cwd)
        chat_id = update.effective_chat.id
        progress_job = None

        async def on_done(task: Task) -> None:
            if progress_job:
                progress_job.schedule_removal()
            icon = {"terminée": "✅", "échouée": "❌", "annulée": "🛑"}.get(task.status, "ℹ️")
            changes = _git_changes(task.cwd)
            text = (
                f"{icon} Tâche #{task.id} {task.status} en {task.duration} "
                f"(agent {task.agent}, projet {task.cwd.name})\n"
                + (f"\nFichiers modifiés :\n{changes}\n" if changes else "")
                + f"\n— fin du journal —\n{task.tail(2500)}"
            )
            others = [a for a in self.cfg.agents if a != task.agent]
            if task.status == "échouée" and others:
                text += f"\n\n💡 Réessayer avec un autre agent : /{others[0]} <tâche>"
            for chunk in split_message(text):
                await context.bot.send_message(chat_id, chunk)

        task = self.tasks.submit(prompt, cwd, on_done, agent)
        status_msg = await update.effective_message.reply_text(
            f"🛠 Tâche #{task.id} lancée avec « {agent} » dans « {cwd.name} ».\n"
            f"Je te préviens quand c'est fini (aucune limite de temps). /log {task.id} pour suivre."
        )
        if self.cfg.progress_minutes and context.job_queue and task.status in ACTIVE:
            # Ce message est mis à jour régulièrement (sans nouvelle notification sur le téléphone).
            progress_job = context.job_queue.run_repeating(
                self._progress, interval=self.cfg.progress_minutes * 60, data=(task, status_msg)
            )

    async def _progress(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        task, msg = context.job.data
        if task.status not in ACTIVE:
            context.job.schedule_removal()
            return
        text = f"🛠 Tâche #{task.id} ({task.agent}, {task.cwd.name}) — {task.status} depuis {task.duration}"
        if last := task.last_line():
            text += f"\n⏳ {last}"
        try:
            await msg.edit_text(text)
        except TelegramError:
            pass  # message identique ou supprimé : sans importance

    # ---------- commandes ----------

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, HELP)

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, f"Ton identifiant Telegram : {update.effective_user.id}")

    async def cmd_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self.reply(update, "Usage : /ask <question>")
            return
        await self.answer(update, context, " ".join(context.args))

    async def cmd_do(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self.reply(update, "Usage : /do <ce qu'il faut faire>")
            return
        await self.start_task(update, context, " ".join(context.args))

    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        current = self.current_agent(context)
        if not context.args:
            lines = [f"{'👉' if n == current else '  '} {n} : {cmd}" for n, cmd in self.cfg.agents.items()]
            await self.reply(update, "🤖 Agents :\n" + "\n".join(lines) + "\n\n/agent <nom> pour changer.")
            return
        await self._switch_agent(update, context, context.args[0].lower())

    async def _switch_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
        if name not in self.cfg.agents:
            await self.reply(update, f"Agent inconnu. Disponibles : {', '.join(self.cfg.agents)}")
            return
        context.chat_data["agent"] = name
        await self.reply(update, f"🤖 Les prochaines tâches utiliseront « {name} ».")

    def agent_command(self, name: str):
        """/claude <tâche> lance une tâche avec Claude ; /claude seul en fait l'agent par défaut."""

        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if context.args:
                await self.start_task(update, context, " ".join(context.args), name)
            else:
                await self._switch_agent(update, context, name)

        return handler

    async def cmd_project(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            existing = sorted(p.name for p in self.cfg.workspace.iterdir() if p.is_dir() and not p.name.startswith("."))
            await self.reply(
                update,
                f"📁 Projet actuel : {self.project_dir(context).name}\n"
                f"Projets : {', '.join(existing) or 'aucun'}\n/projet <nom> pour changer ou créer.",
            )
            return
        name = context.args[0]
        if not PROJECT_RE.match(name) or name.startswith("."):
            await self.reply(update, "Nom invalide (lettres, chiffres, - _ . uniquement).")
            return
        context.chat_data["project"] = name
        (self.cfg.workspace / name).mkdir(parents=True, exist_ok=True)
        await self.reply(update, f"📁 Projet actif : {name}")

    async def cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        tasks = self.tasks.recent()
        if not tasks:
            await self.reply(update, "Aucune tâche pour l'instant.")
            return
        lines = [f"#{t.id} [{t.status}, {t.duration}] {t.agent}@{t.cwd.name} : {t.prompt[:60]}" for t in tasks]
        await self.reply(update, "\n".join(lines))

    def _task_from_args(self, context: ContextTypes.DEFAULT_TYPE) -> Task | None:
        if context.args and context.args[0].lstrip("#").isdigit():
            return self.tasks.tasks.get(int(context.args[0].lstrip("#")))
        recent = self.tasks.recent(1)
        return recent[0] if recent else None

    async def cmd_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        task = self._task_from_args(context)
        if not task:
            await self.reply(update, "Tâche introuvable.")
            return
        await self.reply(update, f"📜 #{task.id} [{task.status}, {task.duration}]\n\n{task.tail(3500) or '(rien encore)'}")

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        task = self._task_from_args(context)
        ok = bool(task) and self.tasks.cancel(task.id)
        await self.reply(update, f"🛑 Tâche #{task.id} arrêtée." if ok else "Rien à arrêter.")

    async def cmd_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        task = self._task_from_args(context)
        if not task:
            await self.reply(update, "Tâche introuvable.")
            return
        if task.status in ACTIVE:
            await self.reply(update, f"La tâche #{task.id} est encore {task.status}.")
            return
        agent = task.agent if task.agent in self.cfg.agents else self.current_agent(context)
        await self.start_task(update, context, task.prompt, agent, task.cwd)

    async def cmd_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self.reply(update, "Usage : /get <chemin/dans/le/projet>")
            return
        target = resolve_inside(self.project_dir(context), " ".join(context.args))
        if not target or not target.is_file():
            await self.reply(update, "Fichier introuvable dans le projet actif.")
            return
        with target.open("rb") as fh:
            await update.effective_message.reply_document(fh, filename=target.name)

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.chat_data["history"] = []
        await self.reply(update, "🧹 Conversation oubliée.")

    async def cmd_briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_chat.send_action(ChatAction.TYPING)
        await self.reply(update, await build_briefing(self.cfg, self.llm))

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handle_text(update, context, update.effective_message.text)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not voice.available():
            await self.reply(update, "🎙 Vocal non activé : installe faster-whisper (voir README).")
            return
        await update.effective_chat.send_action(ChatAction.TYPING)
        media = update.effective_message.voice or update.effective_message.audio
        tg_file = await media.get_file()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.ogg"
            await tg_file.download_to_drive(path)
            text = await voice.transcribe(path, self.cfg.whisper_model)
        if not text:
            await self.reply(update, "🎙 Je n'ai rien compris, tu peux répéter ?")
            return
        await self.reply(update, f"🎙 « {text} »")
        await self.handle_text(update, context, text)

    async def _on_startup(self, app: Application) -> None:
        if not self.tasks.interrupted:
            return
        lines = [f"#{t.id} ({t.agent}, {t.cwd.name}) : {t.prompt[:60]}" for t in self.tasks.interrupted]
        text = "⚠️ Jarvis a redémarré, ces tâches ont été interrompues :\n" + "\n".join(lines)
        text += "\n\n/relancer <n> pour en reprendre une."
        for user_id in self.cfg.allowed_user_ids:
            try:
                await app.bot.send_message(user_id, text)
            except TelegramError:
                log.warning("Impossible de prévenir %s des tâches interrompues", user_id)

    async def daily_briefing(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = await build_briefing(self.cfg, self.llm)
        for user_id in self.cfg.allowed_user_ids:
            for chunk in split_message(text):
                await context.bot.send_message(user_id, chunk)

    # ---------- assemblage ----------

    def build_app(self) -> Application:
        # Projet actif, agent choisi et conversation survivent aux redémarrages.
        persistence = PicklePersistence(filepath=self.cfg.workspace / ".jarvis-state.pickle")
        app = (
            Application.builder()
            .token(self.cfg.telegram_token)
            .concurrent_updates(True)
            .persistence(persistence)
            .post_init(self._on_startup)
            .build()
        )
        auth = self.authorized
        app.add_handler(CommandHandler("id", self.cmd_id))
        for names, fn in [
            (["start", "aide", "help"], self.cmd_help),
            (["ask"], self.cmd_ask),
            (["do"], self.cmd_do),
            (["agent", "agents"], self.cmd_agent),
            (["projet", "project"], self.cmd_project),
            (["taches", "tasks"], self.cmd_tasks),
            (["log"], self.cmd_log),
            (["stop"], self.cmd_stop),
            (["relancer", "retry"], self.cmd_retry),
            (["get"], self.cmd_get),
            (["reset"], self.cmd_reset),
            (["briefing"], self.cmd_briefing),
        ]:
            app.add_handler(CommandHandler(names, auth(fn)))
        for name in self.cfg.agents:
            app.add_handler(CommandHandler(name, auth(self.agent_command(name))))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auth(self.on_text)))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, auth(self.on_voice)))

        if self.cfg.briefing_time:
            hour, minute = (int(x) for x in self.cfg.briefing_time.split(":"))
            app.job_queue.run_daily(
                self.daily_briefing, time=dtime(hour, minute, tzinfo=ZoneInfo(self.cfg.timezone))
            )
        return app


def _ensure_git(cwd: Path) -> None:
    """Chaque projet est un dépôt git : tu peux voir/annuler ce que l'agent a changé."""
    cwd.mkdir(parents=True, exist_ok=True)
    if shutil.which("git") and not (cwd / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=cwd, capture_output=True)


def _git_changes(cwd: Path, limit: int = 30) -> str:
    if not shutil.which("git") or not (cwd / ".git").exists():
        return ""
    out = subprocess.run(["git", "status", "--short"], cwd=cwd, capture_output=True, text=True).stdout
    lines = out.splitlines()
    extra = f"\n… et {len(lines) - limit} autres" if len(lines) > limit else ""
    return "\n".join(lines[:limit]) + extra
