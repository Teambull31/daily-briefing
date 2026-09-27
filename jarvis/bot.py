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
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import voice
from .briefing import build_briefing
from .config import Config
from .llm import Ollama
from .tasks import Task, TaskManager

log = logging.getLogger("jarvis")

TG_LIMIT = 4000
HISTORY_TURNS = 10
DEFAULT_PROJECT = "general"
PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

HELP = """🤖 Jarvis — tes commandes

Écris simplement ce que tu veux : je décide seul s'il faut répondre ou agir sur ton PC.

/do <tâche> — force une action (coder, créer, lancer…)
/ask <question> — force une simple réponse
/projet [nom] — change de projet (dossier de travail)
/taches — liste des tâches
/log <n> — dernières lignes d'une tâche
/stop <n> — arrête une tâche
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
        self.llm = Ollama(cfg.ollama_url, cfg.chat_model)
        self.tasks = TaskManager(cfg.agent_cmd, cfg.max_parallel_tasks, cfg.workspace / ".jarvis-logs")
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

    def project_dir(self, context: ContextTypes.DEFAULT_TYPE) -> Path:
        return self.cfg.workspace / context.chat_data.get("project", DEFAULT_PROJECT)

    @staticmethod
    async def reply(update: Update, text: str) -> None:
        for chunk in split_message(text):
            await update.effective_message.reply_text(chunk)

    # ---------- logique principale ----------

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
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

    async def start_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
        cwd = self.project_dir(context)
        _ensure_git(cwd)
        chat_id = update.effective_chat.id

        async def on_done(task: Task) -> None:
            icon = {"terminée": "✅", "échouée": "❌", "annulée": "🛑"}.get(task.status, "ℹ️")
            changes = _git_changes(task.cwd)
            text = (
                f"{icon} Tâche #{task.id} {task.status} en {task.duration} (projet {task.cwd.name})\n"
                + (f"\nFichiers modifiés :\n{changes}\n" if changes else "")
                + f"\n— fin du journal —\n{task.tail(2500)}"
            )
            for chunk in split_message(text):
                await context.bot.send_message(chat_id, chunk)

        task = self.tasks.submit(prompt, cwd, on_done)
        await self.reply(
            update,
            f"🛠 Tâche #{task.id} lancée dans « {cwd.name} ».\n"
            f"Je te préviens quand c'est fini (aucune limite de temps). /log {task.id} pour suivre.",
        )

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
        lines = [f"#{t.id} [{t.status}, {t.duration}] {t.cwd.name} : {t.prompt[:60]}" for t in tasks]
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

    async def daily_briefing(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = await build_briefing(self.cfg, self.llm)
        for user_id in self.cfg.allowed_user_ids:
            for chunk in split_message(text):
                await context.bot.send_message(user_id, chunk)

    # ---------- assemblage ----------

    def build_app(self) -> Application:
        app = Application.builder().token(self.cfg.telegram_token).concurrent_updates(True).build()
        auth = self.authorized
        app.add_handler(CommandHandler("id", self.cmd_id))
        for names, fn in [
            (["start", "aide", "help"], self.cmd_help),
            (["ask"], self.cmd_ask),
            (["do"], self.cmd_do),
            (["projet", "project"], self.cmd_project),
            (["taches", "tasks"], self.cmd_tasks),
            (["log"], self.cmd_log),
            (["stop"], self.cmd_stop),
            (["get"], self.cmd_get),
            (["reset"], self.cmd_reset),
            (["briefing"], self.cmd_briefing),
        ]:
            app.add_handler(CommandHandler(names, auth(fn)))
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
