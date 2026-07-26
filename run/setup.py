#!/usr/bin/env python3
"""KAI setup & run CLI: a small, dependency-free launcher.

One FastAPI app + Postgres/pgvector. Uses its OWN port and database so it never
clashes with anything else running on this machine.

    python run/setup.py install      venv + deps + .env + ensure the Postgres DB
    python run/setup.py start        run the API (background) on KAI_PORT
    python run/setup.py stop
    python run/setup.py restart
    python run/setup.py status       running? + /health
    python run/setup.py ingest       POST /ingest (load the configured sources)
    python run/setup.py reindex      rebuild the vector index from scratch (keeps curated/feedback)
    python run/setup.py ui           serve the web chat UI (frontend/) in a browser
    python run/setup.py reset-db     drop + recreate the KAI database (clears EVERYTHING)
    python run/setup.py bot          run the chat bot (Webex/Slack/Teams; selected by CHAT_PLATFORM)
    python run/setup.py doctor       check Postgres / pgvector / Ollama / deps
    python run/setup.py fresh        WIPE the DB, then install + start + ingest (clean rebuild)
    python run/setup.py supervise    keep the API alive (auto-restart) for a demo

You still install the LLM/embedding models yourself (e.g. `ollama pull
qwen2.5:14b-instruct && ollama pull nomic-embed-text`); `doctor` checks they're up.

Config (env overrides):
    KAI_PORT (default 8100)   KAI_HOST (default 127.0.0.1)   KAI_DB (default kai)
    PGHOST/PGPORT/PGUSER/PGPASSWORD (default localhost/5432/postgres/postgres)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib import request as _req

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
LOG = Path("/tmp/kai.api.log")
PIDFILE = ROOT / ".kai.pid"

# KAI's own port/DB, deliberately distinct from any sibling app (e.g. 8090).
KAI_PORT = os.environ.get("KAI_PORT", "8100")
KAI_HOST = os.environ.get("KAI_HOST", "127.0.0.1")
KAI_DB = os.environ.get("KAI_DB", "kai")
PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = os.environ.get("PGPORT", "5432")
PGUSER = os.environ.get("PGUSER", "postgres")
PGPASSWORD = os.environ.get("PGPASSWORD", "postgres")

DEPS = [
    "fastapi",
    "uvicorn",
    "pydantic>=2",
    "pydantic-settings",
    "httpx",
    "psycopg[binary]>=3",
    "pgvector",
    "numpy",
    # Cross-encoder reranker (RERANKER=cross_encoder) and the chat bots.
    "sentence-transformers",
    "webex_bot",
    "slack_bolt",
    "pypdf",
]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _bin(name: str) -> Path:
    sub = "Scripts" if os.name == "nt" else "bin"
    return VENV / sub / (name + (".exe" if os.name == "nt" else ""))


def _py() -> Path:
    return _bin("python")


def ok(m: str) -> None:
    print(f"  ✓ {m}")


def err(m: str) -> None:
    print(f"  ✗ {m}")


def info(m: str) -> None:
    print(f"  {m}")


def _pg_env() -> dict:
    e = dict(os.environ)
    e["PGPASSWORD"] = PGPASSWORD
    return e


def psql(sql: str, db: str = "postgres") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", db, "-tAc", sql],
        env=_pg_env(),
        capture_output=True,
        text=True,
    )


def health_url() -> str:
    return f"http://{KAI_HOST}:{KAI_PORT}/health"


def get_health(timeout: float = 2.0):
    try:
        with _req.urlopen(health_url(), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def port_pids(port: str) -> list[str]:
    r = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    return [p for p in r.stdout.split() if p.strip()]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def do_install() -> None:
    print("KAI install")
    if not _py().exists():
        info("creating virtualenv (.venv)...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    ok("venv ready")
    info("installing dependencies...")
    subprocess.run([str(_bin("pip")), "install", "-q", "--upgrade", "pip"], check=False)
    subprocess.run([str(_bin("pip")), "install", "-q", *DEPS], check=True)
    ok("dependencies installed")
    _ensure_env()
    _ensure_db()
    info(
        "Next: edit .env (LLM / DB / Confluence), then `python run/setup.py start`, "
        "`ingest`, and `ui` (browser test)."
    )


def _ensure_env() -> None:
    """Create .env from .env.example on first install (so the next steps work)."""

    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        ok(".env present")
        return
    if example.exists():
        import shutil

        shutil.copyfile(example, env)
        ok("created .env from .env.example, edit it (LLM / DB / Confluence) before ingest")
    else:
        info("no .env or .env.example found, create a .env before starting")


def _ensure_db() -> None:
    probe = psql("SELECT 1")
    if probe.returncode != 0:
        err(f"Postgres not reachable at {PGUSER}@{PGHOST}:{PGPORT}, start it, then re-run.")
        return
    exists = psql(f"SELECT 1 FROM pg_database WHERE datname='{KAI_DB}'").stdout.strip() == "1"
    if exists:
        ok(f"database '{KAI_DB}' exists")
    else:
        subprocess.run(
            ["createdb", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, KAI_DB],
            env=_pg_env(),
            check=False,
        )
        ok(f"database '{KAI_DB}' created")
    psql("CREATE EXTENSION IF NOT EXISTS vector", db=KAI_DB)
    ok("pgvector extension ensured")


def do_start() -> None:
    if get_health():
        info(f"already running at {health_url()}")
        return
    for pid in port_pids(KAI_PORT):  # clear a stale holder of the port
        subprocess.run(["kill", "-9", pid], check=False)
    if not _bin("uvicorn").exists():
        err("uvicorn missing, run `python run/setup.py install` first.")
        return
    print(f"KAI starting on http://{KAI_HOST}:{KAI_PORT}")
    # Deliberately NOT a context manager: the handle is inherited by the uvicorn
    # child as its stdout/stderr and must stay open after this function returns.
    fh = open(LOG, "ab")  # noqa: SIM115
    proc = subprocess.Popen(
        [str(_bin("uvicorn")), "kai.app:app", "--host", KAI_HOST, "--port", KAI_PORT],
        cwd=str(ROOT),
        stdout=fh,
        stderr=fh,
        start_new_session=True,
    )
    PIDFILE.write_text(str(proc.pid))
    for _ in range(60):
        if get_health():
            ok(f"up, API {health_url()}  ·  docs http://{KAI_HOST}:{KAI_PORT}/docs")
            info(f"logs: {LOG}")
            return
        time.sleep(1)
    err(f"did not become healthy in time, check {LOG}")


def do_stop() -> None:
    stopped = False
    if PIDFILE.exists():
        try:
            os.kill(int(PIDFILE.read_text().strip()), signal.SIGTERM)
            stopped = True
        except Exception:
            pass
        PIDFILE.unlink(missing_ok=True)
    for pid in port_pids(KAI_PORT):
        subprocess.run(["kill", "-9", pid], check=False)
        stopped = True
    ok("stopped" if stopped else "not running")


def do_restart() -> None:
    do_stop()
    time.sleep(1)
    do_start()


def do_supervise() -> None:
    """Keep the API alive: start it, then restart whenever /health stops responding.

    Run this instead of ``start`` for an unattended demo, if uvicorn dies (crash,
    OOM, etc.) it is brought back within a few seconds, so a long demo or eval
    never silently goes dark. Ctrl-C to quit.
    """

    info("supervising the API, auto-restart on failure (Ctrl-C to stop)")
    misses = 0
    while True:
        if get_health():
            misses = 0
        else:
            misses += 1
            info(f"API not healthy (miss #{misses}), (re)starting...")
            do_stop()
            time.sleep(1)
            do_start()
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            info("supervisor stopped (the API keeps running; use `stop` to end it)")
            return


def do_status() -> None:
    h = get_health()
    if h:
        ok(f"running at {health_url()} → {h}")
    else:
        err(f"not running on :{KAI_PORT}")


def do_reset_db() -> None:
    print("KAI reset-db (drops the ENTIRE KAI database, index, curated answers, telemetry)")
    do_stop()
    subprocess.run(
        ["dropdb", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "--if-exists", KAI_DB],
        env=_pg_env(),
        check=False,
    )
    subprocess.run(
        ["createdb", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, KAI_DB], env=_pg_env(), check=False
    )
    psql("CREATE EXTENSION IF NOT EXISTS vector", db=KAI_DB)
    ok(f"database '{KAI_DB}' reset")


def do_ingest() -> None:
    if not get_health():
        err("API not running, `python run/setup.py start` first.")
        return
    info("ingesting the configured knowledge source (.env CONFLUENCE_*)...")
    req = _req.Request(f"http://{KAI_HOST}:{KAI_PORT}/ingest", method="POST")
    try:
        with _req.urlopen(req, timeout=900) as r:
            ok(f"ingest → {r.read().decode()}")
    except Exception as exc:  # noqa: BLE001
        err(f"ingest failed: {type(exc).__name__}: {exc}")


def do_reindex() -> None:
    if not get_health():
        err("API not running, `python run/setup.py start` first.")
        return
    info("rebuilding the vector index from scratch (sources + approved curated)...")
    req = _req.Request(f"http://{KAI_HOST}:{KAI_PORT}/admin/reindex", method="POST")
    try:
        with _req.urlopen(req, timeout=1800) as r:
            ok(f"reindex → {r.read().decode()}")
    except Exception as exc:  # noqa: BLE001
        err(f"reindex failed: {type(exc).__name__}: {exc}")


def do_doctor() -> None:
    print("KAI doctor")
    ok(f"python {sys.version.split()[0]}")
    (ok if _py().exists() else err)(
        "venv " + ("present" if _py().exists() else "MISSING (run install)")
    )
    if _py().exists():
        r = subprocess.run(
            [
                str(_py()),
                "-c",
                "import fastapi,uvicorn,httpx,psycopg,pgvector,numpy,pydantic_settings;print('ok')",
            ],
            capture_output=True,
            text=True,
        )
        (ok if r.returncode == 0 else err)(
            "python deps " + ("importable" if r.returncode == 0 else "MISSING (run install)")
        )
    pr = psql("SELECT 1")
    (ok if pr.returncode == 0 else err)(
        f"postgres {PGUSER}@{PGHOST}:{PGPORT} "
        + ("reachable" if pr.returncode == 0 else "UNREACHABLE")
    )
    if pr.returncode == 0:
        ev = psql("SELECT 1 FROM pg_available_extensions WHERE name='vector'").stdout.strip()
        (ok if ev == "1" else err)("pgvector " + ("available" if ev == "1" else "NOT available"))
        dv = psql(f"SELECT 1 FROM pg_database WHERE datname='{KAI_DB}'").stdout.strip()
        (ok if dv == "1" else err)(
            f"database '{KAI_DB}' " + ("exists" if dv == "1" else "missing (run install)")
        )
    try:
        with _req.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            n = len(json.loads(r.read()).get("models", []))
            ok(f"ollama up on :11434 ({n} models), for real local LLM/embeddings")
    except Exception:
        info("ollama not reachable on :11434 (only needed for real local models)")
    h = get_health()
    (ok if h else info)(
        "API " + (f"running on :{KAI_PORT}" if h else f"not running on :{KAI_PORT}")
    )


def do_fresh() -> None:
    """Full clean rebuild: WIPE the database, then install, start, and ingest.

    Destructive by design, ``fresh`` means a clean slate, so it drops the existing
    index (and curated answers / feedback / telemetry) before re-ingesting. To keep
    existing data, use ``ingest`` (incremental) or ``reindex`` (vector-only rebuild).
    """

    info("fresh: WIPING the database for a clean rebuild (drops everything)...")
    do_install()
    do_reset_db()
    do_start()
    do_ingest()


def do_ui() -> None:
    """Serve the standalone web chat UI (frontend/) for browser testing.

    The frontend is a SEPARATE static app that calls the API over HTTP/CORS, set
    its API base in the page header if the API isn't on the default port.
    """

    index = ROOT / "frontend" / "index.html"
    if not index.exists():
        err("frontend/index.html not found, nothing to serve.")
        return
    ui_port = os.environ.get("KAI_UI_PORT", "3000")
    info(
        f"serving the web UI at http://localhost:{ui_port}  (API expected at "
        f"http://{KAI_HOST}:{KAI_PORT}; set the API base in the header "
        "if different; Ctrl-C to stop)..."
    )
    subprocess.run(
        [str(_py()), "-m", "http.server", ui_port, "-d", str(ROOT / "frontend")],
        check=False,
    )


def _configured_platform() -> str:
    """Best-effort CHAT_PLATFORM (env override, else .env) for an accurate banner."""
    plat = os.environ.get("CHAT_PLATFORM", "").strip()
    if not plat and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("CHAT_PLATFORM=") and not stripped.startswith("#"):
                # strip an inline comment (the shipped .env.example uses one) then quotes
                plat = stripped.split("=", 1)[1].split(" #", 1)[0].strip().strip("\"'")
                break
    return plat or "webex"


_BOT_TOKENS = {
    "webex": "WEBEX_BOT_TOKEN",
    "slack": "SLACK_BOT_TOKEN + SLACK_APP_TOKEN",
    "teams": "TEAMS_APP_ID + TEAMS_APP_PASSWORD",
}


def do_bot() -> None:
    # The chat bot is a thin websocket/webhook client over the running KAI API. The
    # platform (and the tokens it needs) are selected by CHAT_PLATFORM in .env.
    if not get_health():
        err("API not running, `python run/setup.py start` first (the bot calls /ask).")
        return
    platform = _configured_platform()
    tokens = _BOT_TOKENS.get(platform, "the platform tokens")
    info(f"starting the KAI {platform} bot (needs {tokens} in .env; Ctrl-C to stop)...")
    subprocess.run([str(_py()), "-m", "kai.bot"], cwd=str(ROOT), check=False)


COMMANDS = {
    "install": do_install,
    "start": do_start,
    "stop": do_stop,
    "restart": do_restart,
    "supervise": do_supervise,
    "status": do_status,
    "ingest": do_ingest,
    "reindex": do_reindex,
    "reset-db": do_reset_db,
    "ui": do_ui,
    "bot": do_bot,
    "doctor": do_doctor,
    "fresh": do_fresh,
}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    handler = COMMANDS.get(cmd)
    if not handler:
        print(__doc__)
        print(f"Unknown command: {cmd!r}\nCommands: {', '.join(COMMANDS)}")
        sys.exit(1)
    handler()


if __name__ == "__main__":
    main()
