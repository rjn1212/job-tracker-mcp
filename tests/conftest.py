import os
import socket
import tempfile
from pathlib import Path

# MUST run before anything imports job_tracker_mcp.server, which constructs
# `db = Database()` at module scope and would otherwise open -- and MIGRATE --
# the real data/jobs.db. conftest is imported before any test module, so this
# is the last moment it can be prevented. A test suite that silently upgrades
# the user's live database is worse than a failing one.
os.environ["JOB_TRACKER_DB"] = str(
    Path(tempfile.mkdtemp(prefix="job-tracker-tests-")) / "test.db")

import keyring
import pytest

from job_tracker_mcp.database import Database


@pytest.fixture
def db(tmp_path):
    """A Database backed by a throwaway file. Never touches data/jobs.db.

    A real file rather than :memory: so that WAL mode and the pragmas are
    exercised as configured in production.
    """
    database = Database(db_path=tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any real socket in the test suite is a bug.

    Blocks at the socket layer, NOT at httpx. msal talks over requests/urllib3,
    so patching httpx.HTTPTransport would leave the entire auth path free to
    reach the internet while this fixture reported success. Sockets are the one
    chokepoint both libraries share.

    httpx.MockTransport never opens a socket, so it sails through.
    """
    def _boom(*a, **k):
        raise AssertionError("test attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch):
    """Never touch the real Keychain, and never block on its dialog."""
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    monkeypatch.setattr(keyring, "set_password", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _never_touch_the_real_database():
    """Guard the guard: JOB_TRACKER_DB must point somewhere disposable."""
    p = Path(os.environ["JOB_TRACKER_DB"]).resolve()
    real = (Path(__file__).resolve().parent.parent / "data" / "jobs.db").resolve()
    assert p != real, f"tests would write to the live database at {real}"
