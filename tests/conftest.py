import socket

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
