"""Database engine. The connection string comes only from the environment."""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import Engine, create_engine


def normalize_url(url: str) -> str:
    """Pin the psycopg (v3) driver.

    SQLAlchemy 2.1 changed the default PostgreSQL driver, and Render hands out
    ``postgresql://`` (sometimes ``postgres://``) URLs; naming the driver keeps
    the app working whatever SQLAlchemy version is installed.
    """
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("Missing DB_URL (or DATABASE_URL) environment variable.")
    return create_engine(
        normalize_url(url),
        pool_pre_ping=True,
        pool_size=int(os.getenv("DB_POOL_SIZE", "3")),
        max_overflow=2,
        pool_recycle=300,
        pool_timeout=10,  # fail fast (503) instead of holding requests for 30 s
        connect_args={"options": "-c search_path=ns,public -c statement_timeout=20000"},
    )
