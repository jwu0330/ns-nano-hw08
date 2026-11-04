"""WSGI/ASGI entry point, kept at the same path as the template so that
`gunicorn app:app ...` works unchanged (with a Uvicorn worker, since this is FastAPI)."""

from nsapi.main import app

__all__ = ["app"]
