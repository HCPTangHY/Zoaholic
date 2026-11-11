"""Real process restart regression; only a disposable SQLite database is used."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
TEST_SECRET = "jwt-persistence-regression-only-not-a-deployment-secret"

_CHILD_SCRIPT = r"""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

# Check isolation before importing anything that can initialize the database.
root = Path(os.environ["JWT_TEST_ROOT"]).resolve()
db_path = Path(os.environ["DB_PATH"]).resolve()
assert db_path == root / "jwt.db"
assert Path.cwd().parent == root
assert os.environ["DATABASE_URL"] == "sqlite+aiosqlite:///" + str(db_path)
assert os.environ["DB_TYPE"] == "sqlite"
assert "JWT_SECRET" not in os.environ

jwt_utils = importlib.reload(importlib.import_module("core.jwt_utils"))
assert jwt_utils._CACHED_SECRET is None
try:
    jwt_utils.issue_jwt({"sub": "test-owner", "role": "admin"})
except RuntimeError:
    pass
else:
    raise AssertionError("JWT must not use a fixed or process-random fallback")

import db
from core.initialization import refresh_initialization

assert db.DB_TYPE == "sqlite"
assert not db.DISABLE_DATABASE
assert Path(db.db_engine.url.database).resolve() == db_path

async def run():
    try:
        # Supplying in-memory keys avoids config loading and any real api.yaml.
        app = SimpleNamespace(state=SimpleNamespace(
            api_list=["regression-only-admin-key"], admin_initialized=False,
        ))
        assert await refresh_initialization(app, force=True, repair_secret=True)
        assert app.state.admin_initialized
        token = json.load(sys.stdin).get("token")
        if token is None:
            token = jwt_utils.issue_jwt({"sub": "test-owner", "role": "admin"})
        payload = jwt_utils.decode_jwt(token)
        assert payload and payload["sub"] == "test-owner" and payload["role"] == "admin"
        import hashlib
        result = {
            "pid": os.getpid(), "token": token,
            "secret_sha256": hashlib.sha256(jwt_utils._jwt_secret().encode()).hexdigest(),
        }
        # Application logs also use stdout; keep this result in the isolated cwd.
        Path("result.json").write_text(json.dumps(result), encoding="utf-8")
    finally:
        await db.close_db()

asyncio.run(run())
"""


@pytest.fixture
def persisted_jwt_database(tmp_path):
    path = tmp_path / "jwt.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE admin_user (id INTEGER PRIMARY KEY, username TEXT NOT NULL, "
            "password_hash TEXT NOT NULL, jwt_secret TEXT)"
        )
        connection.execute(
            "INSERT INTO admin_user VALUES (?, ?, ?, ?)",
            (1, "test-owner", "not-a-real-password-hash", TEST_SECRET),
        )
    return path


def test_jwt_survives_two_independent_python_processes(persisted_jwt_database):
    database = persisted_jwt_database
    root = database.parent
    # Do not inherit deployment credentials, DB aliases, proxies, or config seeds.
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT") if name in os.environ}
    env.update({
        "PYTHONPATH": str(SOURCE_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(root), "TMPDIR": str(root), "TEMP": str(root), "TMP": str(root),
        "JWT_TEST_ROOT": str(root), "DISABLE_DATABASE": "false", "DB_TYPE": "sqlite",
        "DATABASE_URL": "sqlite+aiosqlite:///" + str(database), "DB_PATH": str(database),
        "API_YAML_PATH": str(root / "unused-api.yaml"), "CONFIG_STORAGE": "file",
        "DEBUG": "false",
    })
    results = []
    for name in ("issuer", "verifier"):
        cwd = root / name
        cwd.mkdir()
        request = {} if not results else {"token": results[0]["token"]}
        process = subprocess.run(
            [sys.executable, "-c", _CHILD_SCRIPT], cwd=cwd, env=env,
            input=json.dumps(request), capture_output=True, text=True, timeout=30,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        results.append(json.loads((cwd / "result.json").read_text(encoding="utf-8")))

    issuer, verifier = results
    assert issuer["pid"] != verifier["pid"]
    assert os.getpid() not in (issuer["pid"], verifier["pid"])
    assert verifier["token"] == issuer["token"]
    digest = hashlib.sha256(TEST_SECRET.encode()).hexdigest()
    assert issuer["secret_sha256"] == verifier["secret_sha256"] == digest
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT jwt_secret FROM admin_user WHERE id = 1").fetchone() == (TEST_SECRET,)
    assert not (root / "unused-api.yaml").exists()
