"""Doctor/install must honor DATABASE_URL when PG* env vars are unset."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SETUP_PATH = Path(__file__).resolve().parents[1] / "run" / "setup.py"


def _load_setup():
    spec = importlib.util.spec_from_file_location("kai_setup_cli", SETUP_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def setup_mod(monkeypatch, tmp_path):
    mod = _load_setup()
    env_file = tmp_path / ".env"
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    # Clear PG* so DATABASE_URL alone supplies host/user/password/db.
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "KAI_DB", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    return mod, env_file


def test_resolve_pg_from_database_url_env(setup_mod, monkeypatch):
    mod, _env_file = setup_mod
    monkeypatch.setenv("DATABASE_URL", "postgresql://kai_user:s3cret@db.example:6543/kai_prod")
    host, port, user, password, db = mod._resolve_pg()
    assert (host, port, user, password, db) == (
        "db.example",
        "6543",
        "kai_user",
        "s3cret",
        "kai_prod",
    )


def test_resolve_pg_from_dotenv_file(setup_mod):
    mod, env_file = setup_mod
    env_file.write_text("DATABASE_URL=postgresql://file_user:file_pw@127.0.0.1:5432/kai_file\n")
    host, port, user, password, db = mod._resolve_pg()
    assert (host, port, user, password, db) == (
        "127.0.0.1",
        "5432",
        "file_user",
        "file_pw",
        "kai_file",
    )


def test_explicit_pg_env_wins_over_database_url(setup_mod, monkeypatch):
    mod, _env_file = setup_mod
    monkeypatch.setenv("DATABASE_URL", "postgresql://url_user:url_pw@db.example:6543/kai_url")
    monkeypatch.setenv("PGHOST", "localhost")
    monkeypatch.setenv("PGUSER", "env_user")
    monkeypatch.setenv("PGPASSWORD", "env_pw")
    host, port, user, password, db = mod._resolve_pg()
    assert host == "localhost"
    assert user == "env_user"
    assert password == "env_pw"
    assert db == "kai_url"  # still from URL when KAI_DB unset
    assert port == "6543"
