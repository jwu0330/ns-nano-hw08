import os

import pytest

# Tests run in-process against DB_URL, or against a deployed server when API_BASE is set
# (e.g. API_BASE=https://ns-nano-hw08.onrender.com).
API_BASE = os.getenv("API_BASE")
needs_db = pytest.mark.skipif(
    not (os.getenv("DB_URL") or API_BASE), reason="set DB_URL (or API_BASE) to run database tests"
)


@pytest.fixture(scope="session")
def client():
    if API_BASE:
        import httpx

        with httpx.Client(base_url=API_BASE, timeout=120, follow_redirects=True) as c:
            yield c
        return
    from fastapi.testclient import TestClient

    from app import app

    yield TestClient(app)


@pytest.fixture(scope="session")
def ref():
    from tests.reference import Reference

    return Reference()
