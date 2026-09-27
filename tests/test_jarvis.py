import asyncio
import sys
import time
from pathlib import Path

from jarvis.bot import resolve_inside, split_message
from jarvis.briefing import parse_feed
from jarvis.config import Config, load_dotenv
from jarvis.llm import strip_thinking
from jarvis.tasks import TaskManager, build_argv

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
