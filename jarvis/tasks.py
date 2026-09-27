"""File de tâches longues exécutées par un agent de code (OpenCode, Aider, Claude Code...).

Chaque tâche lance la commande de l'agent choisi (AGENT_<NOM>) dans un dossier projet,
sans limite de durée,
et écrit toute la sortie dans un fichier journal consultable depuis le téléphone.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

PROMPT_TOKEN = "{prompt}"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
ACTIVE = ("en attente", "en cours")
# Options pour reprendre la dernière session de l'agent dans le même dossier (/suite).
RESUME_FLAGS = {
    "claude": ["--continue"],
    "opencode": ["--continue"],
    "aider": ["--restore-chat-history"],
}
log = logging.getLogger("jarvis.tasks")

# Si l'une de ces variables existe, Claude Code facture à l'API au lieu d'utiliser
# l'abonnement (compte connecté via `claude login`) : on les retire pour lui.
API_BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def exe_name(path: str) -> str:
    """« /usr/bin/claude » ou « C:\\npm\\claude.cmd » -> « claude » (quel que soit le système)."""
    return re.split(r"[\\/]", path)[-1].lower().rsplit(".", 1)[0]


def supports_resume(template: str) -> bool:
    argv = shlex.split(template, posix=True)
    return bool(argv) and exe_name(argv[0]) in RESUME_FLAGS


def agent_env(argv: list[str]) -> dict[str, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"  # sorties lisibles sur Telegram (pas de codes couleur)
    if exe_name(argv[0]) == "claude":
        for var in API_BILLING_VARS:
            env.pop(var, None)
    return env


def build_argv(template: str, prompt: str, resume: bool = False) -> list[str]:
    """Transforme la commande d'un agent en liste d'arguments.

    Le prompt est passé comme un argument unique (jamais interprété par un shell).
    Avec resume=True, les options de reprise de session sont ajoutées juste avant le prompt.
    """
    argv = shlex.split(template, posix=True)
    if PROMPT_TOKEN not in argv:
        argv.append(PROMPT_TOKEN)
    pos = argv.index(PROMPT_TOKEN)
    extra = RESUME_FLAGS.get(exe_name(argv[0]), []) if resume else []
    argv = argv[:pos] + extra + [prompt] + [a for a in argv[pos + 1:] if a != PROMPT_TOKEN]
    resolved = shutil.which(argv[0])  # indispensable sous Windows (.cmd/.exe)
    if resolved:
        argv[0] = resolved
    return argv


@dataclass
class Task:
    id: int
    prompt: str
    cwd: Path
    log_path: Path
    agent: str = ""
    resume: bool = False
    status: str = "en attente"  # en attente | en cours | terminée | échouée | annulée | interrompue
    returncode: int | None = None
    started: float | None = None
    finished: float | None = None
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)

    @property
    def duration(self) -> str:
        if not self.started:
            return "-"
        secs = int((self.finished or time.time()) - self.started)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"

    def tail(self, max_chars: int = 3000) -> str:
        try:
            data = self.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        clean = ANSI_RE.sub("", data[-(max_chars * 2):]).replace("\r\n", "\n").replace("\r", "\n")
        return clean[-max_chars:]

    def last_line(self, max_chars: int = 200) -> str:
        lines = [line.strip() for line in self.tail(4000).splitlines() if line.strip()]
        return lines[-1][:max_chars] if lines else ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "prompt": self.prompt, "cwd": str(self.cwd), "log_path": str(self.log_path),
            "agent": self.agent, "resume": self.resume, "status": self.status, "returncode": self.returncode,
            "started": self.started, "finished": self.finished,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        task = cls(d["id"], d["prompt"], Path(d["cwd"]), Path(d["log_path"]), d.get("agent", ""))
        task.resume = d.get("resume", False)
        task.status, task.returncode = d.get("status", "terminée"), d.get("returncode")
        task.started, task.finished = d.get("started"), d.get("finished")
        return task


Notifier = Callable[[Task], Awaitable[None]]


class TaskManager:
    def __init__(self, agents: dict[str, str] | str, max_parallel: int = 1, logs_dir: Path | None = None):
        self.agents = {"local": agents} if isinstance(agents, str) else dict(agents)
        self.logs_dir = logs_dir
        self.tasks: dict[int, Task] = {}
        # Historique conservé entre deux redémarrages (/taches, /log continuent de marcher).
        self.state_file = logs_dir / "taches.json" if logs_dir else None
        self.interrupted: list[Task] = []  # tâches coupées par le dernier arrêt de Jarvis
        self._load()
        self._ids = itertools.count(max(self.tasks, default=0) + 1)
        self._sem = asyncio.Semaphore(max_parallel)
        self._runners: set[asyncio.Task] = set()

    def submit(
        self,
        prompt: str,
        cwd: Path,
        on_done: Notifier | None = None,
        agent: str | None = None,
        resume: bool = False,
    ) -> Task:
        agent = agent or next(iter(self.agents))
        if agent not in self.agents:
            raise KeyError(agent)
        cwd.mkdir(parents=True, exist_ok=True)
        logs_dir = self.logs_dir or cwd / ".jarvis-logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        task_id = next(self._ids)
        task = Task(task_id, prompt, cwd, logs_dir / f"tache-{task_id}.log", agent, resume)
        self.tasks[task_id] = task
        self._save()
        runner = asyncio.create_task(self._run(task, on_done))
        self._runners.add(runner)
        runner.add_done_callback(self._runners.discard)
        return task

    async def _run(self, task: Task, on_done: Notifier | None) -> None:
        async with self._sem:
            if task.status == "annulée":
                return
            task.status = "en cours"
            task.started = time.time()
            self._save()
            try:
                argv = build_argv(self.agents[task.agent], task.prompt, task.resume)
                with task.log_path.open("wb") as log:
                    log.write(f"$ {' '.join(argv[:-1])} <prompt>\n# {task.prompt}\n\n".encode())
                    log.flush()
                    task.proc = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=task.cwd,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=log,
                        stderr=asyncio.subprocess.STDOUT,
                        env=agent_env(argv),
                        **_new_process_group(),
                    )
                    task.returncode = await task.proc.wait()
                if task.status != "annulée":
                    task.status = "terminée" if task.returncode == 0 else "échouée"
            except Exception as exc:  # commande introuvable, droits, etc.
                task.status = "échouée"
                with task.log_path.open("a", encoding="utf-8") as log:
                    log.write(f"\n[jarvis] Erreur au lancement : {exc!r}\n")
            finally:
                task.finished = time.time()
                task.proc = None
                self._save()
        if on_done:
            try:
                await on_done(task)
            except Exception:  # ex. Telegram injoignable : ne pas perdre la tâche pour autant
                log.exception("Notification de fin de la tâche #%s impossible", task.id)

    def cancel(self, task_id: int) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.status not in ("en attente", "en cours"):
            return False
        task.status = "annulée"
        self._save()
        if task.proc and task.proc.returncode is None:
            _kill_tree(task.proc)
        return True

    def recent(self, n: int = 10) -> list[Task]:
        return sorted(self.tasks.values(), key=lambda t: t.id, reverse=True)[:n]

    def last_in(self, cwd: Path) -> Task | None:
        """Dernière tâche lancée dans ce dossier projet."""
        return next((t for t in self.recent(len(self.tasks)) if t.cwd == cwd), None)

    def running(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status in ACTIVE]

    def _load(self) -> None:
        if not self.state_file or not self.state_file.is_file():
            return
        try:
            for d in json.loads(self.state_file.read_text(encoding="utf-8")):
                task = Task.from_dict(d)
                if task.status in ACTIVE:  # Jarvis s'est arrêté pendant la tâche
                    task.status = "interrompue"
                    self.interrupted.append(task)
                self.tasks[task.id] = task
        except (ValueError, KeyError, TypeError):
            log.warning("Historique des tâches illisible, ignoré : %s", self.state_file)
        if self.interrupted:
            self._save()

    def _save(self, keep: int = 200) -> None:
        if not self.state_file:
            return
        data = [t.to_dict() for t in self.recent(keep)]
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)


def _new_process_group() -> dict:
    # Permet d'arrêter l'agent ET tous les processus qu'il a lancés.
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
