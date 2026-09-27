import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from jarvis.bot import Jarvis, resolve_inside, split_message
from jarvis.briefing import parse_feed
from jarvis.config import Config, load_dotenv, parse_agents
from jarvis.llm import strip_thinking
from jarvis.tasks import TaskManager, agent_env, build_argv

PY = sys.executable.replace("\\", "/")


def test_build_argv_keeps_prompt_as_single_argument():
    argv = build_argv("echo --flag {prompt}", "rm -rf / ; $(whoami)")
    assert argv[1:] == ["--flag", "rm -rf / ; $(whoami)"]


def test_build_argv_appends_prompt_without_token():
    assert build_argv("echo -n", "salut")[1:] == ["-n", "salut"]


def test_task_runs_and_notifies(tmp_path: Path):
    async def scenario():
        done = asyncio.Event()
        seen = []

        async def on_done(task):
            seen.append(task)
            done.set()

        tm = TaskManager(f'{PY} -c "import sys; print(sys.argv[1])" {{prompt}}', logs_dir=tmp_path / "logs")
        task = tm.submit("bonjour jarvis", tmp_path / "proj", on_done)
        await asyncio.wait_for(done.wait(), 10)
        return task, seen

    task, seen = asyncio.run(scenario())
    assert seen == [task]
    assert task.status == "terminée" and task.returncode == 0
    assert "bonjour jarvis" in task.tail()


def test_failed_and_missing_commands(tmp_path: Path):
    async def scenario():
        tm = TaskManager(f'{PY} -c "import sys; sys.exit(3)"', logs_dir=tmp_path)
        bad = TaskManager("commande-qui-nexiste-pas-xyz", logs_dir=tmp_path)
        t1 = tm.submit("x", tmp_path)
        t2 = bad.submit("x", tmp_path)
        await asyncio.gather(*tm._runners, *bad._runners)
        return t1, t2

    t1, t2 = asyncio.run(scenario())
    assert t1.status == "échouée" and t1.returncode == 3
    assert t2.status == "échouée" and "Erreur au lancement" in t2.tail()


def test_cancel_running_task(tmp_path: Path):
    async def scenario():
        tm = TaskManager(f'{PY} -c "import time; time.sleep(60)"', logs_dir=tmp_path)
        task = tm.submit("dors", tmp_path)
        while task.proc is None:
            await asyncio.sleep(0.05)
        start = time.time()
        assert tm.cancel(task.id)
        await asyncio.gather(*tm._runners)
        return task, time.time() - start

    task, elapsed = asyncio.run(scenario())
    assert task.status == "annulée"
    assert elapsed < 10


def test_parallelism_limit_queues_tasks(tmp_path: Path):
    async def scenario():
        tm = TaskManager(f'{PY} -c "import time; time.sleep(0.3)"', max_parallel=1, logs_dir=tmp_path)
        a = tm.submit("a", tmp_path)
        b = tm.submit("b", tmp_path)
        await asyncio.sleep(0.1)
        states = (a.status, b.status)
        await asyncio.gather(*tm._runners)
        return states

    assert asyncio.run(scenario()) == ("en cours", "en attente")


def test_parse_rss_and_atom():
    rss = """<rss><channel><title>Flux</title><image><title>Logo</title></image>
    <item><title>Titre 1</title></item><item><title> Titre 2 </title></item></channel></rss>"""
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><title>Flux</title>
    <entry><title>A</title></entry><entry><title>B</title></entry></feed>"""
    assert parse_feed(rss) == ["Titre 1", "Titre 2"]
    assert parse_feed(atom, limit=1) == ["A"]


def test_resolve_inside_blocks_escape(tmp_path: Path):
    assert resolve_inside(tmp_path, "a/b.txt") == (tmp_path / "a/b.txt").resolve()
    assert resolve_inside(tmp_path, "../secret") is None
    assert resolve_inside(tmp_path, "/etc/passwd") is None


def test_split_message():
    parts = split_message("ligne\n" * 2000, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "".join(parts).count("ligne") == 2000


def test_strip_thinking():
    assert strip_thinking("<think>hmm\n</think>\nRéponse") == "Réponse"


def test_parse_agents():
    agents = parse_agents({"AGENT_LOCAL": "opencode run {prompt}", "AGENT_CLAUDE": "claude -p {prompt}", "X": "y"})
    assert agents == {"local": "opencode run {prompt}", "claude": "claude -p {prompt}"}
    assert parse_agents({"AGENT_CMD": "aider --message"}) == {"local": "aider --message"}
    with pytest.raises(SystemExit):
        parse_agents({"AGENT_LOG": "x"})  # /log est déjà une commande


def test_claude_agent_never_gets_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert "ANTHROPIC_API_KEY" not in agent_env(["/usr/bin/claude", "-p", "x"])
    assert "ANTHROPIC_API_KEY" not in agent_env([r"C:\npm\claude.cmd", "-p", "x"])
    assert agent_env(["/usr/bin/opencode", "run"])["ANTHROPIC_API_KEY"] == "sk-ant-test"


def test_task_uses_requested_agent(tmp_path: Path):
    async def scenario():
        tm = TaskManager(
            {"local": f'{PY} -c "print(\'LOCAL\')"', "claude": f'{PY} -c "print(\'CLAUDE\')"'}, logs_dir=tmp_path
        )
        a = tm.submit("x", tmp_path)
        b = tm.submit("x", tmp_path, agent="claude")
        await asyncio.gather(*tm._runners)
        return a, b

    a, b = asyncio.run(scenario())
    assert (a.agent, b.agent) == ("local", "claude")
    assert "LOCAL" in a.tail() and "CLAUDE" in b.tail()


def test_agent_prefix_detection(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AGENT_LOCAL", "opencode run {prompt}")
    monkeypatch.setenv("AGENT_CLAUDE", "claude -p {prompt}")
    jarvis = Jarvis(Config.from_env())
    m = jarvis._agent_prefix.match("Claude, crée un site vitrine")
    assert (m.group(2).lower(), m.group(3)) == ("claude", "crée un site vitrine")
    m = jarvis._agent_prefix.match("@local fais un script")
    assert (m.group(1), m.group(3)) == ("local", "fais un script")
    assert jarvis._agent_prefix.match("claude est-il meilleur que qwen ?") is None


def test_config_from_env(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('TELEGRAM_TOKEN="abc"\nALLOWED_USER_IDS=1, 2\n# commentaire\nBRIEFING_TIME=07:30\n')
    for key in ("TELEGRAM_TOKEN", "ALLOWED_USER_IDS", "BRIEFING_TIME"):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(env)
    cfg = Config.from_env()
    assert cfg.telegram_token == "abc"
    assert cfg.allowed_user_ids == {1, 2}
    assert cfg.briefing_time == "07:30"


def test_task_history_survives_restart(tmp_path: Path):
    async def scenario():
        tm = TaskManager(f'{PY} -c "print(1)"', logs_dir=tmp_path)
        done = tm.submit("fini", tmp_path)
        await asyncio.gather(*tm._runners)
        return done

    done = asyncio.run(scenario())
    # Simule un arrêt brutal pendant une 2e tâche
    state = tmp_path / "taches.json"
    data = json.loads(state.read_text())
    data.insert(0, {**data[0], "id": 2, "prompt": "coupée", "status": "en cours"})
    state.write_text(json.dumps(data))

    reloaded = TaskManager("echo", logs_dir=tmp_path)
    assert reloaded.tasks[done.id].status == "terminée"
    assert [t.id for t in reloaded.interrupted] == [2]
    assert reloaded.tasks[2].status == "interrompue"
    assert next(reloaded._ids) == 3
    # Au redémarrage suivant, elle n'est plus signalée une 2e fois
    assert TaskManager("echo", logs_dir=tmp_path).interrupted == []


def test_tail_strips_ansi_and_carriage_returns(tmp_path: Path):
    from jarvis.tasks import Task

    log = tmp_path / "t.log"
    log.write_bytes(b"\x1b[32mvert\x1b[0m\r\n10%\r50%\r100%\n\x1b]0;titre\x07fin\n")
    task = Task(1, "p", tmp_path, log)
    assert task.tail() == "vert\n10%\n50%\n100%\nfin\n"
    assert task.last_line() == "fin"


def test_ollama_think_flag_and_fallback():
    import httpx

    from jarvis.llm import Ollama

    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append("think" in body)
        if "think" in body:
            return httpx.Response(400, json={"error": "unknown field think"})
        return httpx.Response(200, json={"message": {"content": "ok"}})

    async def scenario():
        o = Ollama("http://x", "m")
        o._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return await o.ask("a"), await o.ask("b")

    assert asyncio.run(scenario()) == ("ok", "ok")
    assert seen == [True, False, False]  # un seul essai avec "think", puis plus jamais


def test_bot_builds_with_persistence(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    app = Jarvis(Config.from_env()).build_app()
    commands = {c for h in app.handlers[0] for c in getattr(h, "commands", ())}
    assert {"relancer", "local", "briefing"} <= commands
    assert app.persistence is not None


def test_progress_updates_status_message(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    from jarvis.tasks import Task

    monkeypatch.setenv("TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    jarvis = Jarvis(Config.from_env())
    log = tmp_path / "t.log"
    log.write_text("étape 1\nécriture de index.html\n")
    task = Task(7, "site", tmp_path, log, "local", status="en cours", started=0.0)

    class Msg:
        text = None

        async def edit_text(self, text):
            self.text = text

    msg, removed = Msg(), []
    job = SimpleNamespace(data=(task, msg), schedule_removal=lambda: removed.append(True))
    asyncio.run(jarvis._progress(SimpleNamespace(job=job)))
    assert "#7" in msg.text and "écriture de index.html" in msg.text and not removed

    task.status = "terminée"
    asyncio.run(jarvis._progress(SimpleNamespace(job=job)))
    assert removed == [True]
