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


@pytest.fixture(autouse=True)
def _no_real_voice(monkeypatch):
    """Pas de synthèse vocale réelle (ni de téléchargement de voix) pendant les tests."""
    monkeypatch.setenv("VOICE_MODE", "off")
    monkeypatch.setattr("jarvis.tts.Speaker.available", staticmethod(lambda: False))


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


# ---------- mémoire, rappels, fichiers ----------

from datetime import datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from jarvis.memory import REMEMBER_PREFIX, Store, parse_reminder  # noqa: E402

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Europe/Paris"))  # un lundi


@pytest.mark.parametrize(
    "text, expected_when, expected_what",
    [
        ("rappelle-moi dans 20 minutes de sortir le linge", NOW + timedelta(minutes=20), "sortir le linge"),
        ("Rappelle moi demain à 9h d'appeler le garage", NOW.replace(day=29, hour=9), "appeler le garage"),
        ("rappelle-moi à 18h30 de faire les courses", NOW.replace(hour=18, minute=30), "faire les courses"),
        ("rappelle-moi à 8h de prendre mes médicaments", NOW.replace(day=29, hour=8), "prendre mes médicaments"),
        ("rappel: ce soir réunion parents", NOW.replace(hour=19), "réunion parents"),
        ("rappelle-moi après-demain à 14 heures qu'il faut payer", NOW.replace(day=30, hour=14), "il faut payer"),
        ("rappelle-moi dans 2 h : relancer le build", NOW + timedelta(hours=2), "relancer le build"),
    ],
)
def test_parse_reminder(text, expected_when, expected_what):
    assert parse_reminder(text, NOW) == (expected_when, expected_what)


@pytest.mark.parametrize("text", ["rappelle-moi lundi prochain de voir Paul", "rappelle-moi à 25h de x"])
def test_parse_reminder_unknown_goes_to_llm(text):
    assert parse_reminder(text, NOW) is None


def test_remember_prefix():
    assert REMEMBER_PREFIX.sub("", "Souviens-toi que je code en Python", count=1) == "je code en Python"
    assert not REMEMBER_PREFIX.match("note ça dans un fichier")


def test_store_persists_notes_and_reminders(tmp_path: Path):
    store = Store(tmp_path / "m.json")
    store.add_note("mon VPS est chez Hetzner")
    r1 = store.add_reminder(1, NOW + timedelta(hours=2), "b")
    r2 = store.add_reminder(1, NOW + timedelta(hours=1), "a")
    again = Store(tmp_path / "m.json")
    assert again.notes == ["mon VPS est chez Hetzner"]
    assert [r["text"] for r in again.reminders] == ["a", "b"]  # triés par date
    assert again.remove_reminder(r1["id"]) and not again.remove_reminder(999)
    assert again.add_reminder(1, NOW, "c")["id"] == r2["id"] + 1
    assert again.remove_note(1) == "mon VPS est chez Hetzner" and again.remove_note(1) is None


def test_corrupted_store_is_kept_aside(tmp_path: Path):
    (tmp_path / "m.json").write_text("{pas du json")
    assert Store(tmp_path / "m.json").notes == []
    assert (tmp_path / "m.corrompu").exists()


def test_llm_parse_when_and_router_actions():
    import httpx

    from jarvis.llm import Ollama

    answers = iter(['{"datetime": "2026-10-05T09:00", "texte": "voir Paul"}', '{"datetime": null}',
                    '{"action": "reminder"}', '{"action": "delete_everything"}'])

    def handler(req):
        return httpx.Response(200, json={"message": {"content": next(answers)}})

    async def scenario():
        o = Ollama("http://x", "m")
        o._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return (await o.parse_when("lundi prochain voir Paul", NOW), await o.parse_when("un jour", NOW),
                await o.route("rappelle-moi…"), await o.route("x"))

    when, none, action, fallback = asyncio.run(scenario())
    assert when == (datetime(2026, 10, 5, 9, 0, tzinfo=NOW.tzinfo), "voir Paul")
    assert none is None and action == "reminder" and fallback == "chat"


def test_assistant_context_includes_date_and_notes(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    jarvis = Jarvis(Config.from_env())
    jarvis.store.add_note("je m'appelle Max")
    ctx = jarvis.assistant_context()
    assert "Nous sommes le" in ctx and "- je m'appelle Max" in ctx


def test_file_helpers(tmp_path: Path):
    from jarvis.bot import _safe_filename, _unique_path

    assert _safe_filename("../../etc/pass wd?.txt") == "pass wd_.txt"
    assert _safe_filename("...") == "fichier"
    (tmp_path / "a.txt").write_text("x")
    assert _unique_path(tmp_path / "a.txt").name == "a-1.txt"


def test_reminder_flow_end_to_end(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    monkeypatch.setenv("TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    jarvis = Jarvis(Config.from_env())
    replies, sent, scheduled = [], [], []

    async def reply_text(text):
        replies.append(text)

    async def send_message(chat_id, text, **kw):
        sent.append((chat_id, text))

    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply_text),
                             effective_chat=SimpleNamespace(id=42))
    job_queue = SimpleNamespace(run_once=lambda cb, when, data, name: scheduled.append((cb, when, data, name)))
    context = SimpleNamespace(job_queue=job_queue, chat_data={}, bot=SimpleNamespace(send_message=send_message))

    asyncio.run(jarvis.handle_text(update, context, "rappelle-moi dans 10 minutes de sortir le chien"))
    assert "C'est noté" in replies[-1] and "sortir le chien" in replies[-1]
    callback, when, rid, name = scheduled[0]
    assert when.tzinfo is not None and name == f"rappel-{rid}"

    context.job = SimpleNamespace(data=rid)
    asyncio.run(callback(context))
    assert sent == [(42, "⏰ Rappel : sortir le chien")]
    assert jarvis.store.reminders == []


# ---------- /suite et /etat ----------


@pytest.mark.parametrize(
    "template, expected",
    [
        ("claude -p {prompt} --dangerously-skip-permissions", ["-p", "--continue", "go", "--dangerously-skip-permissions"]),
        ("opencode run -m ollama/x {prompt}", ["run", "-m", "ollama/x", "--continue", "go"]),
        ("aider --yes-always --message", ["--yes-always", "--message", "--restore-chat-history", "go"]),
        ("inconnu --x {prompt}", ["--x", "go"]),
    ],
)
def test_build_argv_resume(template, expected):
    assert build_argv(template, "go", resume=True)[1:] == expected
    assert "--continue" not in build_argv(template, "go")


def test_supports_resume_and_last_in(tmp_path: Path):
    from jarvis.tasks import supports_resume

    assert supports_resume("claude -p {prompt}") and supports_resume("/home/u/.opencode/bin/opencode run")
    assert not supports_resume("goose run -t {prompt}")

    async def scenario():
        tm = TaskManager(f'{PY} -c "print(1)"', logs_dir=tmp_path / "logs")
        tm.submit("a", tmp_path / "p1")
        b = tm.submit("b", tmp_path / "p2", resume=True)
        c = tm.submit("c", tmp_path / "p1")
        await asyncio.gather(*tm._runners)
        return tm, b, c

    tm, b, c = asyncio.run(scenario())
    assert tm.last_in(tmp_path / "p1") is c and tm.last_in(tmp_path / "p2") is b
    assert TaskManager("echo", logs_dir=tmp_path / "logs").tasks[b.id].resume is True


def test_amd_gpu_and_memory_from_sysfs(tmp_path: Path):
    from jarvis.status import amd_gpus, memory

    dev = tmp_path / "card1" / "device"
    (dev / "hwmon" / "hwmon3").mkdir(parents=True)
    (dev / "mem_info_vram_total").write_text(str(16 * 1024**3))
    (dev / "mem_info_vram_used").write_text(str(9 * 1024**3))
    (dev / "gpu_busy_percent").write_text("87\n")
    (dev / "hwmon" / "hwmon3" / "temp1_input").write_text("64000")
    (tmp_path / "card0" / "device").mkdir(parents=True)  # carte non AMD : ignorée
    assert amd_gpus(tmp_path) == ["GPU 87% · VRAM 9.0/16 Go · 64°C"]

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       32768000 kB\nMemAvailable:   16384000 kB\n")
    assert memory(meminfo) == "RAM 15.6/31 Go"


def test_ollama_status(monkeypatch):
    import httpx

    import jarvis.status as status

    def handler(req):
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.3"})
        return httpx.Response(200, json={"models": [{"name": "qwen3:14b", "size": 10 * 1024**3, "size_vram": 10 * 1024**3}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(status.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    lines = asyncio.run(status.ollama_lines("http://x"))
    assert lines == ["✅ Ollama 0.12.3", "  • qwen3:14b chargé (10.0 Go, 100% sur GPU)"]

    monkeypatch.setattr(status.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="x")), **kw))
    assert asyncio.run(status.ollama_lines("http://x")) == ["❌ Ollama ne répond pas"]


# ---------- recherche web ----------


def test_html_to_text_skips_scripts_and_menus():
    from jarvis.web import html_to_text

    html = """<html><head><style>p{}</style><script>var x=1</script></head><body>
    <nav>Menu Accueil</nav><h1>Titre</h1><p>Le  texte &eacute;tudi&eacute;.</p><footer>©</footer></body></html>"""
    assert html_to_text(html) == "Titre Le texte étudié."


def test_web_answer_cites_sources():
    import httpx

    from jarvis.llm import Ollama
    from jarvis.web import web_answer

    prompts = []

    def handler(req):
        if req.url.host == "searx":
            assert req.url.params["format"] == "json"
            return httpx.Response(200, json={"results": [
                {"title": "Page A", "url": "https://a.test/x", "content": "extrait A"},
                {"title": "Page B", "url": "https://b.test/y", "content": "extrait B"},
                {"title": "Page C", "url": "https://c.test/z", "content": "extrait C"},
                {"title": "sans url"},
            ]})
        if req.url.host == "a.test":
            return httpx.Response(200, html="<p>Contenu complet de A. " + "Détails utiles. " * 20 + "</p>")
        if req.url.host == "c.test":
            return httpx.Response(200, html="<p>Making sure you're not a bot! " + "Loading... " * 30 + "</p>")
        if req.url.host == "b.test":
            return httpx.Response(404)
        prompts.append(json.loads(req.content)["messages"][0]["content"])
        return httpx.Response(200, json={"message": {"content": "Réponse [1]"}})

    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    async def scenario(monkeypatch_client):
        import jarvis.web as web

        web.httpx.AsyncClient = monkeypatch_client
        try:
            o = Ollama("http://ollama", "m")
            o._client = real(transport=transport)
            return await web_answer(o, "http://searx", "question ?")
        finally:
            web.httpx.AsyncClient = real

    out = asyncio.run(scenario(lambda **kw: real(transport=transport, **kw)))
    assert out.startswith("Réponse [1]")
    assert "[1] https://a.test/x" in out and "[2] https://b.test/y" in out
    assert "Contenu complet de A" in prompts[0] and "extrait A" in prompts[0]
    assert "extrait B" in prompts[0]  # page 404 : on garde l'extrait du moteur
    assert "extrait C" in prompts[0] and "not a bot" not in prompts[0]  # page anti-robot ignorée


def test_web_answer_when_searxng_is_down():
    import httpx

    import jarvis.web as web
    from jarvis.llm import Ollama

    real = httpx.AsyncClient
    web.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw)
    try:
        out = asyncio.run(web.web_answer(Ollama("http://o", "m"), "http://searx", "q"))
    finally:
        web.httpx.AsyncClient = real
    assert "SearXNG est-il lancé" in out


# ---------- corrections de la relecture ----------


@pytest.mark.skipif(sys.platform == "win32", reason="signaux POSIX")
def test_cancel_force_kills_agent_ignoring_sigterm(tmp_path: Path, monkeypatch):
    import jarvis.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "FORCE_KILL_DELAY", 0.5)
    script = tmp_path / "tetu.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('pret', flush=True)\n"
        "time.sleep(60)\n"
    )

    async def scenario():
        tm = tasks_mod.TaskManager(f"{PY} {script.as_posix()}", logs_dir=tmp_path / "logs")
        task = tm.submit("têtu", tmp_path)
        while "pret" not in task.tail():
            await asyncio.sleep(0.05)
        start = time.time()
        tm.cancel(task.id)
        await asyncio.wait_for(asyncio.gather(*tm._runners), 10)
        return task, time.time() - start

    task, elapsed = asyncio.run(scenario())
    assert task.status == "annulée" and elapsed < 5


def test_stop_without_number_targets_last_active_task(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    from jarvis.tasks import Task

    monkeypatch.setenv("TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    jarvis = Jarvis(Config.from_env())
    jarvis.tasks.tasks = {
        1: Task(1, "a", tmp_path, tmp_path / "1.log", status="en cours"),
        2: Task(2, "b", tmp_path, tmp_path / "2.log", status="terminée"),
    }
    assert jarvis._task_from_args(SimpleNamespace(args=[]), active_only=True).id == 1
    assert jarvis._task_from_args(SimpleNamespace(args=[])).id == 2
    assert jarvis._task_from_args(SimpleNamespace(args=["#2"]), active_only=True).id == 2


def test_think_kept_on_unrelated_400():
    import httpx

    from jarvis.llm import Ollama

    def handler(req):
        return httpx.Response(400, json={"error": "prompt too long"})

    async def scenario():
        o = Ollama("http://x", "m")
        o._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            await o.ask("a")
        return o.think

    assert asyncio.run(scenario()) is False  # toujours actif (désactivation de la réflexion conservée)


def _fake_update(replies, user_id=1):
    from types import SimpleNamespace

    async def reply_text(text):
        replies.append(text)

    async def send_action(action):
        pass

    update = SimpleNamespace(
        effective_message=SimpleNamespace(reply_text=reply_text),
        effective_chat=SimpleNamespace(id=user_id, send_action=send_action),
        effective_user=SimpleNamespace(id=user_id),
    )
    return update


def test_missing_model_message(monkeypatch, tmp_path: Path):
    import httpx
    from types import SimpleNamespace

    monkeypatch.setenv("TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    jarvis = Jarvis(Config.from_env())
    jarvis.llm._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "model not found"}))
    )
    replies = []
    asyncio.run(jarvis.answer(_fake_update(replies), SimpleNamespace(chat_data={}), "salut"))
    assert "ollama pull qwen3:14b" in replies[0]


def test_error_handler_tells_authorized_user(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    monkeypatch.setenv("TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    jarvis = Jarvis(Config.from_env())
    monkeypatch.setattr("jarvis.bot.Update", SimpleNamespace)  # nos faux updates passent isinstance()
    ok, stranger = [], []
    ctx = SimpleNamespace(error=RuntimeError("boum"))
    asyncio.run(jarvis.on_error(_fake_update(ok, 1), ctx))
    asyncio.run(jarvis.on_error(_fake_update(stranger, 999), ctx))
    assert ok == ["⚠️ Erreur inattendue : RuntimeError: boum"] and stranger == []


def test_reminder_prefix_needs_colon_for_bare_rappel():
    from jarvis.memory import REMINDER_PREFIX

    assert REMINDER_PREFIX.match("rappel: sortir")
    assert REMINDER_PREFIX.match("Rappelle-moi demain")
    assert not REMINDER_PREFIX.match("rappel des faits de la guerre de 14")


# ---------- coach de productivité ----------


class FakeBot:
    def __init__(self):
        self.messages, self.voices, self.audios = [], [], []

    async def send_message(self, chat_id, text, disable_notification=False, **kw):
        self.messages.append((chat_id, text, disable_notification))

    async def send_voice(self, chat_id, audio, disable_notification=False, **kw):
        self.voices.append((chat_id, audio))

    async def send_audio(self, chat_id, audio, filename=None, disable_notification=False, **kw):
        self.audios.append((chat_id, audio, filename))


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, data=None, name=None):
        self.jobs.append((callback, when, name))

    def get_jobs_by_name(self, name):
        return []


def _coach(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Jarvis(Config.from_env())


def _ctx(bot=None, jq=None, args=None):
    from types import SimpleNamespace

    return SimpleNamespace(bot=bot or FakeBot(), job_queue=jq or FakeJobQueue(), chat_data={}, args=args or [])


@pytest.mark.parametrize(
    "text, expected",
    [
        ("todo: acheter du pain", "acheter du pain"),
        ("ajoute à ma liste appeler le comptable", "appeler le comptable"),
        ("ajoute appeler Paul à ma liste", "appeler Paul"),
        ("ajoute à ma liste de choses à faire : réviser le chapitre 3", "réviser le chapitre 3"),
        ("mets finir le devis dans ma liste de tâches", "finir le devis"),
    ],
)
def test_extract_todo(text, expected):
    from jarvis.coach import extract_todo

    assert extract_todo(text) == expected


def test_todo_store_order_and_completion(tmp_path: Path):
    store = Store(tmp_path / "m.json")
    store.add_todo("b", now=NOW)
    store.add_todo("c", now=NOW)
    store.add_todo("a urgent", urgent=True, now=NOW)
    assert [t["text"] for t in store.pending_todos] == ["a urgent", "b", "c"]
    store.complete_todo(store.pending_by_number(2), NOW)
    assert [t["text"] for t in store.pending_todos] == ["a urgent", "c"]
    assert [t["text"] for t in store.done_on(NOW.date())] == ["b"]
    assert store.pending_by_number(9) is None
    store.purge_old_done(NOW + timedelta(days=1))
    assert [t["text"] for t in Store(tmp_path / "m.json").todos] == ["a urgent", "c"]


def test_parse_focus():
    from jarvis.memory import parse_focus

    assert parse_focus("") == (25, 1, "")
    assert parse_focus("45 min sur le rapport client") == (45, 1, "le rapport client")
    assert parse_focus("50 minutes 3 sessions sur le site") == (50, 3, "le site")
    assert parse_focus("je me concentre pendant 30 min sur la compta") == (30, 1, "la compta")
    assert parse_focus("x4 révisions", default_minutes=20) == (20, 4, "révisions")
    assert parse_focus("999") == (180, 1, "")  # borné


def test_focus_full_cycle(monkeypatch, tmp_path: Path):
    jarvis = _coach(monkeypatch, tmp_path)
    jarvis.store.add_todo("écrire l'intro", now=NOW)
    replies, bot, jq = [], FakeBot(), FakeJobQueue()
    update = _fake_update(replies, 42)
    ctx = _ctx(bot, jq)

    asyncio.run(jarvis.start_focus(update, ctx, "25 x2"))
    session = jarvis.store.focus
    assert session["topic"] == "écrire l'intro" and session["rounds"] == 2  # sujet = prochaine tâche
    assert "C'est parti : 25 min" in replies[-1] and jarvis.focus_active()

    asyncio.run(jarvis.start_focus(update, ctx, "10"))  # déjà en cours
    assert "Déjà en concentration" in replies[-1]

    tick = jq.jobs[-1][0]
    asyncio.run(tick(ctx))  # fin session 1 -> pause
    assert jarvis.store.focus["phase"] == "break" and "Session 1/2 terminée" in bot.messages[-1][1]
    asyncio.run(tick(ctx))  # fin pause -> session 2
    assert jarvis.store.focus["round"] == 2 and "session 2/2" in bot.messages[-1][1]
    asyncio.run(tick(ctx))  # fin session 2 -> dernière pause
    assert "2 session(s), 50 min" in bot.messages[-1][1] and "/fait" in bot.messages[-1][1]
    asyncio.run(tick(ctx))  # fin de la pause -> relance vers la prochaine étape
    assert jarvis.store.focus is None and "Prochaine étape : écrire l'intro" in bot.messages[-1][1]
    assert jarvis.store.focus_minutes_on(jarvis.now().date()) == (2, 50)


def test_stop_focus_logs_partial_time(monkeypatch, tmp_path: Path):
    jarvis = _coach(monkeypatch, tmp_path)
    replies = []
    ctx = _ctx()
    asyncio.run(jarvis.start_focus(_fake_update(replies, 42), ctx, "30 sur le devis"))
    session = jarvis.store.focus
    session["end"] = (jarvis.now() + timedelta(minutes=18)).isoformat()  # 12 min déjà faites
    jarvis.store.set_focus(session)
    asyncio.run(jarvis.cmd_stop_focus(_fake_update(replies, 42), ctx))
    assert jarvis.store.focus is None
    assert jarvis.store.data["focus_log"][-1]["minutes"] == 12
    assert jarvis.store.data["focus_log"][-1]["completed"] is False


def test_task_notifications_are_silent_during_focus(monkeypatch, tmp_path: Path):
    jarvis = _coach(monkeypatch, tmp_path, AGENT_LOCAL=f'{PY} -c "print(1)"')
    bot, replies = FakeBot(), []

    async def scenario():
        from types import SimpleNamespace

        ctx = SimpleNamespace(bot=bot, job_queue=None, chat_data={}, args=[])
        await jarvis.start_focus(_fake_update(replies, 42), _ctx(bot), "25")
        await jarvis.start_task(_fake_update(replies, 42), ctx, "tâche")
        await asyncio.gather(*jarvis.tasks._runners)

    asyncio.run(scenario())
    done = [m for m in bot.messages if "Tâche #1" in m[1]]
    assert done and all(silent for _, _, silent in done)


def test_voice_notes_follow_mode(monkeypatch, tmp_path: Path):
    jarvis = _coach(monkeypatch, tmp_path)

    class FakeSpeaker:
        fmt = "ogg"

        async def speak(self, text):
            return (b"OggS" + text.encode(), self.fmt)

    jarvis.speaker = FakeSpeaker()
    bot = FakeBot()
    asyncio.run(jarvis.notify(bot, 42, "⏰ Rappel : sortir"))
    assert bot.voices == []  # VOICE_MODE=off (tests)

    jarvis.store.set_setting("voice", "rappels")
    asyncio.run(jarvis.notify(bot, 42, "⏰ Rappel : sortir"))
    assert bot.messages[-1][1] == "⏰ Rappel : sortir" and len(bot.voices) == 1

    jarvis.speaker.fmt = "wav"  # pas de ffmpeg : fichier audio au lieu d'un vocal
    asyncio.run(jarvis.notify(bot, 42, "x"))
    assert bot.audios[-1][2] == "jarvis.wav"

    class BrokenSpeaker:
        async def speak(self, text):
            raise RuntimeError("voix absente")

    jarvis.speaker = BrokenSpeaker()
    asyncio.run(jarvis.notify(bot, 42, "texte quand même"))
    assert bot.messages[-1][1] == "texte quand même"  # la voix ne bloque jamais le texte


def test_voice_command_changes_mode(monkeypatch, tmp_path: Path):
    jarvis = _coach(monkeypatch, tmp_path)
    replies = []
    asyncio.run(jarvis.cmd_voice(_fake_update(replies, 42), _ctx(args=["tout"])))
    assert jarvis.voice_mode == "tout" and "Mode vocal : tout" in replies[-1]
    asyncio.run(jarvis.cmd_voice(_fake_update(replies, 42), _ctx(args=["n'importe"])))
    assert jarvis.voice_mode == "tout"


def test_todo_commands_and_next(monkeypatch, tmp_path: Path):
    jarvis = _coach(monkeypatch, tmp_path)
    replies = []
    update = _fake_update(replies, 42)
    asyncio.run(jarvis.add_todos(update, "rédiger le devis\n! appeler le client\nenvoyer la facture"))
    assert "3 tâches" in replies[-1]
    asyncio.run(jarvis.cmd_next(update, _ctx()))
    assert "appeler le client" in replies[-1]  # l'urgent passe en premier
    asyncio.run(jarvis.cmd_done(update, _ctx()))  # sans numéro : la première
    assert "« appeler le client » est fait" in replies[-1] and "Ensuite : rédiger le devis" in replies[-1]
    asyncio.run(jarvis.cmd_done(update, _ctx(args=["2"])))
    assert "envoyer la facture" in replies[-1]
    asyncio.run(jarvis.cmd_todos(update, _ctx()))
    assert "1. rédiger le devis" in replies[-1] and "Faites aujourd'hui : 2" in replies[-1]
    summary = jarvis.daily_summary(jarvis.now().date())
    assert "Tâches cochées : 2" in summary and "rédiger le devis" in summary
    assert "📝 À faire aujourd'hui" in asyncio.run(jarvis.agenda())


def test_breakdown_adds_steps(monkeypatch, tmp_path: Path):
    import httpx

    jarvis = _coach(monkeypatch, tmp_path)
    steps = '{"etapes": ["Lister les pages", "Écrire la page d\'accueil", "Mettre en ligne"]}'
    jarvis.llm._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"message": {"content": steps}}))
    )
    replies = []
    asyncio.run(jarvis.cmd_breakdown(_fake_update(replies, 42), _ctx(args=["créer", "mon", "site"])))
    assert [t["text"] for t in jarvis.store.pending_todos] == [
        "Lister les pages", "Écrire la page d'accueil", "Mettre en ligne"
    ]
    assert "créer mon site" in replies[-1]


def test_natural_language_todo_and_focus_shortcuts(monkeypatch, tmp_path: Path):
    jarvis = _coach(monkeypatch, tmp_path)
    replies = []
    ctx = _ctx()
    asyncio.run(jarvis.handle_text(_fake_update(replies, 42), ctx, "ajoute à ma liste réserver le garage"))
    assert jarvis.store.pending_todos[0]["text"] == "réserver le garage"
    asyncio.run(jarvis.handle_text(_fake_update(replies, 42), ctx, "focus 40 min sur la compta"))
    assert jarvis.store.focus["minutes"] == 40 and jarvis.store.focus["topic"] == "la compta"


def test_clean_for_speech():
    from jarvis.tts import clean_for_speech

    assert clean_for_speech("⏰ ⏱️ Rappel : **sortir** 🎉 https://x.y /rappels") == "Rappel : sortir"
    long_text = "Phrase courte. " * 100
    assert len(clean_for_speech(long_text)) <= 600 and clean_for_speech(long_text).endswith(".")


# ---------- habitudes, relances, statistiques, agenda ----------

MONDAY_10H = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Europe/Paris"))

SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:standup
DTSTART:20260928T080000Z
DTEND:20260928T090000Z
SUMMARY:Réunion équipe
LOCATION:Bureau
RRULE:FREQ=DAILY;COUNT=5
END:VEVENT
BEGIN:VEVENT
UID:dentiste
DTSTART;TZID=Europe/Paris:20260928T110000
DTEND;TZID=Europe/Paris:20260928T123000
SUMMARY:Dentiste
END:VEVENT
BEGIN:VEVENT
UID:anniv
DTSTART;VALUE=DATE:20260928
DTEND;VALUE=DATE:20260929
SUMMARY:Anniversaire de Léa
END:VEVENT
END:VCALENDAR
"""


class FakeBotWithFiles(FakeBot):
    def __init__(self):
        super().__init__()
        self.photos = []

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.photos.append((chat_id, photo, caption))


def _life(monkeypatch, tmp_path, now=MONDAY_10H, **env):
    jarvis = _coach(monkeypatch, tmp_path, **env)
    monkeypatch.setattr(jarvis, "now", lambda: now)
    return jarvis


def _with_calendar(monkeypatch, jarvis, ics=SAMPLE_ICS):
    import httpx

    import jarvis.calendar_ics as cal

    real = httpx.AsyncClient
    monkeypatch.setattr(
        cal.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=ics)), **kw)
    )
    jarvis.calendar.urls = ("https://calendar.test/secret.ics",)


def test_habits_streaks_and_claims(monkeypatch, tmp_path: Path):
    jarvis = _life(monkeypatch, tmp_path)
    replies = []
    update = _fake_update(replies, 42)
    asyncio.run(jarvis.cmd_habit(update, _ctx(args=["sport"])))
    habit = jarvis.store.habits[0]
    for d in (1, 2, 3, 4, 5, 6):  # 6 jours d'affilée avant aujourd'hui
        jarvis.store.check_habit(habit, MONDAY_10H.date() - timedelta(days=d))
    asyncio.run(jarvis.handle_text(update, _ctx(), "J’ai fait du sport ce matin"))
    assert "🔥 7 jour(s) d'affilée" in replies[-1] and "Une semaine" in replies[-1]
    asyncio.run(jarvis.cmd_check(update, _ctx(args=["1"])))
    assert "déjà coché" in replies[-1]
    assert jarvis.habit_lines() == ["1. ✅ sport — 🔥 7 j"]
    assert "Habitudes : 1/1 🎉" in jarvis.daily_summary(MONDAY_10H.date())


def test_nudge_rules(monkeypatch, tmp_path: Path):
    jarvis = _life(monkeypatch, tmp_path)
    bot = FakeBot()
    ctx = _ctx(bot)
    jarvis.store.add_todo("finir le devis", now=MONDAY_10H)
    jarvis.store.set_setting("last_progress", (MONDAY_10H - timedelta(minutes=30)).isoformat())
    asyncio.run(jarvis.nudge_check(ctx))
    assert bot.messages == []  # trop tôt

    jarvis.store.set_setting("last_progress", (MONDAY_10H - timedelta(hours=3)).isoformat())
    asyncio.run(jarvis.nudge_check(ctx))
    assert len(bot.messages) == 1 and "finir le devis" in bot.messages[0][1] and "/plustard" in bot.messages[0][1]
    asyncio.run(jarvis.nudge_check(ctx))
    assert len(bot.messages) == 1  # pas deux relances d'affilée

    sunday = _life(monkeypatch, tmp_path / "dim", now=MONDAY_10H - timedelta(days=1))
    sunday.store.add_todo("x", now=MONDAY_10H)
    sunday.store.set_setting("last_progress", (MONDAY_10H - timedelta(days=2)).isoformat())
    asyncio.run(sunday.nudge_check(ctx))
    assert len(bot.messages) == 1  # pas de relance le week-end


def test_no_nudge_during_meeting_or_focus(monkeypatch, tmp_path: Path):
    meeting_time = MONDAY_10H.replace(hour=11, minute=30)  # pendant le dentiste
    jarvis = _life(monkeypatch, tmp_path, now=meeting_time)
    _with_calendar(monkeypatch, jarvis)
    bot = FakeBot()
    jarvis.store.add_todo("finir le devis", now=MONDAY_10H)
    jarvis.store.set_setting("last_progress", (MONDAY_10H - timedelta(hours=5)).isoformat())
    asyncio.run(jarvis.nudge_check(_ctx(bot)))
    assert bot.messages == []
    jarvis.calendar.urls = ()
    jarvis.store.set_focus({"phase": "focus"})
    asyncio.run(jarvis.nudge_check(_ctx(bot)))
    assert bot.messages == []


def test_postpone_moves_todo_and_resets_nudges(monkeypatch, tmp_path: Path):
    jarvis = _life(monkeypatch, tmp_path)
    replies = []
    for t in ("a", "b", "c"):
        jarvis.store.add_todo(t, now=MONDAY_10H)
    asyncio.run(jarvis.cmd_postpone(_fake_update(replies, 42), _ctx()))
    assert [t["text"] for t in jarvis.store.pending_todos] == ["b", "c", "a"]
    assert "Maintenant : b" in replies[-1]
    assert jarvis.store.settings["last_progress"] == MONDAY_10H.isoformat()


def test_stats_sends_png_chart(monkeypatch, tmp_path: Path):
    pytest.importorskip("matplotlib")
    jarvis = _life(monkeypatch, tmp_path)
    jarvis.store.log_focus(MONDAY_10H - timedelta(days=1), 50, "x", True)
    jarvis.store.complete_todo(jarvis.store.add_todo("y", now=MONDAY_10H), MONDAY_10H)
    bot = FakeBotWithFiles()
    asyncio.run(jarvis.send_stats(bot, 42, 7))
    chat_id, png, caption = bot.photos[0]
    assert png[:4] == b"\x89PNG" and "Concentration : 50 min" in caption and "Tâches cochées : 1" in caption


def test_stats_text_fallback_without_matplotlib(monkeypatch, tmp_path: Path):
    jarvis = _life(monkeypatch, tmp_path)
    monkeypatch.setattr("jarvis.life.render_png", lambda rows, title: None)
    bot = FakeBotWithFiles()
    asyncio.run(jarvis.send_stats(bot, 42, 7))
    assert bot.photos == [] and "Ta semaine" in bot.messages[0][1]


def test_calendar_parsing_and_ics_roundtrip():
    from jarvis.calendar_ics import fmt_event, make_ics, parse_events

    tz = ZoneInfo("Europe/Paris")
    day = datetime(2026, 9, 28, tzinfo=tz)
    events = parse_events([SAMPLE_ICS], day, day + timedelta(days=1), tz)
    assert [fmt_event(e) for e in events] == [
        "toute la journée · Anniversaire de Léa",
        "10:00–11:00 · Réunion équipe (Bureau)",  # 08:00 UTC -> 10:00 à Paris
        "11:00–12:30 · Dentiste",
    ]
    ics = make_ics("🎯 Focus : devis, v2", day.replace(hour=14), day.replace(hour=15), "a;b")
    back = parse_events([ics.decode()], day, day + timedelta(days=1), tz)
    assert back[0].title == "🎯 Focus : devis, v2" and back[0].start == day.replace(hour=14)


def test_agenda_and_event_reminders(monkeypatch, tmp_path: Path):
    jarvis = _life(monkeypatch, tmp_path, now=MONDAY_10H.replace(hour=10, minute=50))
    _with_calendar(monkeypatch, jarvis)
    agenda = asyncio.run(jarvis.agenda())
    assert "📅 Agenda du jour" in agenda and "Dentiste" in agenda
    bot = FakeBot()
    asyncio.run(jarvis.event_reminder_check(_ctx(bot)))
    assert [m[1] for m in bot.messages] == ["📅 Dans 10 min : Dentiste"]
    asyncio.run(jarvis.event_reminder_check(_ctx(bot)))
    assert len(bot.messages) == 1  # un seul rappel par événement


def test_block_finds_free_slot_around_meetings(monkeypatch, tmp_path: Path):
    jarvis = _life(monkeypatch, tmp_path, now=MONDAY_10H.replace(hour=9, minute=40))
    _with_calendar(monkeypatch, jarvis)
    slot = asyncio.run(jarvis.find_focus_slot(90))
    # 09:45 -> 10:00 trop court, réunion 10-11, dentiste 11-12:30 : premier créneau de 90 min à 12:30
    assert slot == (MONDAY_10H.replace(hour=12, minute=30), MONDAY_10H.replace(hour=14))

    friday_evening = _life(monkeypatch, tmp_path / "ven", now=datetime(2026, 10, 2, 18, 30, tzinfo=MONDAY_10H.tzinfo))
    slot = asyncio.run(friday_evening.find_focus_slot(60))
    assert slot[0] == datetime(2026, 10, 5, 9, 0, tzinfo=MONDAY_10H.tzinfo)  # saute le week-end


def test_block_command_sends_ics_and_schedules_reminder(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    jarvis = _life(monkeypatch, tmp_path, now=MONDAY_10H.replace(hour=15))
    jarvis.store.add_todo("écrire le rapport", now=MONDAY_10H)
    docs, replies, jq = [], [], FakeJobQueue()

    async def reply_document(data, filename=None, caption=None):
        docs.append((data, filename, caption))

    update = _fake_update(replies, 42)
    update.effective_message.reply_document = reply_document
    asyncio.run(jarvis.cmd_block(update, SimpleNamespace(args=["60"], job_queue=jq, chat_data={})))
    data, filename, caption = docs[0]
    assert filename == "focus.ics" and b"SUMMARY:\xf0\x9f\x8e\xaf Focus : \xc3\xa9crire le rapport" in data
    assert "lundi 28/09 de 15:15 à 16:15" in caption
    assert jq.jobs and "écrire le rapport" in jarvis.store.reminders[0]["text"]
