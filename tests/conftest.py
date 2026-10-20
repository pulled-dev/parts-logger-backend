import json
import os
import socket
import sqlite3

import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_path(name: str) -> str:
    return os.path.join(FIXTURES, name)


def load_json(name: str):
    with open(fixture_path(name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Unit tests must never reach eBay (or anything else) over the network."""
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(f"network access blocked in tests: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("EBAY_APP_ID", "EBAY_CERT_ID", "EBAY_DEV_ID", "EBAY_AUTH_TOKEN", "EBAY_SITE_ID",
                "EBAY_USER_REFRESH_TOKEN", "PRICING_ADMIN_TOKEN", "PRICING_DAILY_SYNC", "PRICING_MODE",
                "STAFF_API_KEY", "BOT_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="session")
def _session_db_path(tmp_path_factory):
    return str(tmp_path_factory.mktemp("session-db") / "session.db")


@pytest.fixture(autouse=True)
def isolated_db_path(_session_db_path, monkeypatch):
    """No test may touch the repo's ./vehicles.db: anything that reads the
    part-name database without asking for tmp_db gets a shared throwaway file
    (seeded once per run)."""
    import db
    monkeypatch.setenv("DB_PATH", _session_db_path)
    monkeypatch.setattr(db, "DB_PATH", _session_db_path)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point both db.py and the pricing store at a throwaway SQLite file."""
    import db
    from pricing.store import init_pricing_db

    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    init_pricing_db()
    return path


@pytest.fixture
def conn(tmp_db):
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


STAFF_KEY = "test-staff-key-0123456789"


@pytest.fixture
def client(tmp_db, monkeypatch):
    """The app as the logger page uses it: staff key configured and sent."""
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setenv("STAFF_API_KEY", STAFF_KEY)
    with TestClient(main.app, headers={"X-Staff-Key": STAFF_KEY}) as c:
        yield c


@pytest.fixture
def anon_client(client):
    """Same app and database, but sending no staff key."""
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c
