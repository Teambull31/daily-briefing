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

    async def send_message(chat_id, text):
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
