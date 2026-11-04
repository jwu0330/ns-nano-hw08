import os

import pytest

needs_db = pytest.mark.skipif(not os.getenv("DB_URL"), reason="set DB_URL to run database tests")


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app import app

    return TestClient(app)


@pytest.fixture(scope="session")
def ref():
    from tests.reference import Reference

    return Reference()
