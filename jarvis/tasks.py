"""File de tâches longues exécutées par un agent de code (OpenCode, Aider, Claude Code...).

Chaque tâche lance la commande de l'agent choisi (AGENT_<NOM>) dans un dossier projet,
sans limite de durée,
et écrit toute la sortie dans un fichier journal consultable depuis le téléphone.
"""

from __future__ import annotations

import asyncio
import itertools
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

# Si l'une de ces variables existe, Claude Code facture à l'API au lieu d'utiliser
# l'abonnement (compte connecté via `claude login`) : on les retire pour lui.
API_BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def agent_env(argv: list[str]) -> dict[str, str]:
    env = dict(os.environ)
    exe = re.split(r"[\\/]", argv[0])[-1].lower()  # marche pour les chemins Windows et Unix
    if exe.rsplit(".", 1)[0] == "claude":
        for var in API_BILLING_VARS:
            env.pop(var, None)
    return env


def build_argv(template: str, prompt: str) -> list[str]:
    """Transforme la commande d'un agent en liste d'arguments.

    Le prompt est passé comme un argument unique (jamais interprété par un shell).
    """
    argv = shlex.split(template, posix=True)
    if PROMPT_TOKEN in argv:
        argv = [prompt if a == PROMPT_TOKEN else a for a in argv]
    else:
        argv.append(prompt)
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
    status: str = "en attente"  # en attente | en cours | terminée | échouée | annulée
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
        return data[-max_chars:]


Notifier = Callable[[Task], Awaitable[None]]


class TaskManager:
    def __init__(self, agents: dict[str, str] | str, max_parallel: int = 1, logs_dir: Path | None = None):
        self.agents = {"local": agents} if isinstance(agents, str) else dict(agents)
        self.logs_dir = logs_dir
        self.tasks: dict[int, Task] = {}
        self._ids = itertools.count(1)
        self._sem = asyncio.Semaphore(max_parallel)
        self._runners: set[asyncio.Task] = set()

    def submit(self, prompt: str, cwd: Path, on_done: Notifier | None = None, agent: str | None = None) -> Task:
        agent = agent or next(iter(self.agents))
        if agent not in self.agents:
            raise KeyError(agent)
        cwd.mkdir(parents=True, exist_ok=True)
        logs_dir = self.logs_dir or cwd / ".jarvis-logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        task_id = next(self._ids)
        task = Task(task_id, prompt, cwd, logs_dir / f"tache-{task_id}.log", agent)
        self.tasks[task_id] = task
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
            try:
                argv = build_argv(self.agents[task.agent], task.prompt)
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
        if on_done:
            await on_done(task)

    def cancel(self, task_id: int) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.status not in ("en attente", "en cours"):
            return False
        task.status = "annulée"
        if task.proc and task.proc.returncode is None:
            _kill_tree(task.proc)
        return True

    def recent(self, n: int = 10) -> list[Task]:
        return sorted(self.tasks.values(), key=lambda t: t.id, reverse=True)[:n]


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
