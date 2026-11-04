"""FastAPI application: Neurosynth functional-dissociation backend."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import analysis
from . import queries as q
from .db import get_engine
from .parsing import InputError, fmt_coords, normalize_term, parse_coords

STATIC = Path(__file__).resolve().parent / "static"
VERSION = "1.0.0"

log = logging.getLogger("nsapi")


def _warm_up() -> None:
    try:
        analysis._loader.get()
    except Exception:  # the first analyze request will retry and report the error
        log.exception("analysis index warm-up failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Load the analysis index in the background so startup is not delayed.
    threading.Thread(target=_warm_up, daemon=True).start()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Neurosynth Dissociation API",
    version=VERSION,
    description=(
        "Functional dissociation queries over the Neurosynth database "
        "(NTU Psychoinformatics HW08). Returns studies that mention A but not B, "
        "for terms or MNI coordinates."
    ),
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ------------------------------------------------------------------ errors
class NotFound(Exception):
    def __init__(self, message: str, suggestions: list[str]):
        self.message, self.suggestions = message, suggestions


@app.exception_handler(InputError)
async def _input_error(_: Request, exc: InputError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": {"message": exc.message, "hint": exc.hint}})


@app.exception_handler(NotFound)
async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse(
        status_code=404, content={"error": {"message": exc.message, "suggestions": exc.suggestions}}
    )


# Every error has the same shape: {"error": {"message": ..., ...}}.
@app.exception_handler(RequestValidationError)
async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{"parameter": ".".join(map(str, e["loc"][1:])) or str(e["loc"][0]), "problem": e["msg"]} for e in exc.errors()]
    message = "; ".join(f"{d['parameter']}: {d['problem']}" for d in details)
    return JSONResponse(status_code=422, content={"error": {"message": message, "details": details}})


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = "No such endpoint. See /docs." if exc.status_code == 404 else str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": {"message": message}}, headers=exc.headers)


@app.middleware("http")
async def _timing_and_cache(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
    # The data never changes between deployments, so successful lookups can be cached.
    if response.status_code == 200 and request.url.path.startswith(("/dissociate", "/terms", "/locations")):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# ------------------------------------------------------------------ shared parameters
Threshold = Annotated[
    float, Query(ge=0, lt=1, description="A study mentions a term when its TF-IDF weight is above this.")
]
Radius = Annotated[
    float, Query(gt=0, le=30, description="A study mentions a location when it reports a focus within this many mm.")
]
Both = Annotated[bool, Query(description="Also return the opposite direction (B but not A).")]
Analyze = Annotated[bool, Query(description="Add terms (and, for term queries, brain locations) that characterise each side.")]
Limit = Annotated[int | None, Query(ge=1, le=20000, description="Maximum studies listed per direction (default: all).")]
Offset = Annotated[int, Query(ge=0, description="Skip this many studies (for paging).")]
TermA = Annotated[str, PathParam(description="Term A (spaces or `_`).", openapi_examples={"PCC": {"value": "posterior cingulate"}})]
TermB = Annotated[str, PathParam(description="Term B (spaces or `_`).", openapi_examples={"vmPFC": {"value": "ventromedial prefrontal"}})]
CoordsA = Annotated[str, PathParam(description="Location A as `x_y_z` (MNI mm).", openapi_examples={"PCC": {"value": "0_-52_26"}})]
CoordsB = Annotated[str, PathParam(description="Location B as `x_y_z` (MNI mm).", openapi_examples={"vmPFC": {"value": "-2_50_-6"}})]


# Study-id sets are pure functions of the (static) database, so they are memoised.
@lru_cache(maxsize=1024)
def _term_set(term: str, threshold: float) -> frozenset[str]:
    with get_engine().connect() as conn:
        if not q.term_exists(conn, term):
            raise NotFound(f"Term {term!r} is not in the Neurosynth vocabulary.", q.suggest_terms(conn, term))
        return q.term_studies(conn, term, threshold)


@lru_cache(maxsize=1024)
def _near_set(xyz: tuple[float, float, float], radius: float) -> frozenset[str]:
    with get_engine().connect() as conn:
        return q.near_studies(conn, xyz, radius)


def _direction(conn, ids: frozenset[str], limit: int | None, offset: int) -> dict[str, Any]:
    return {"count": len(ids), "studies": q.study_details(conn, ids, limit, offset)}


def _dissociation(
    query: dict[str, Any],
    set_a: frozenset[str],
    set_b: frozenset[str],
    *,
    both: bool,
    analyze: bool,
    limit: int | None,
    offset: int,
    term_exclude: tuple[str, ...] = (),
    threshold: float = 0.001,
    with_locations: bool = False,
) -> dict[str, Any]:
    a_only, b_only = set_a - set_b, set_b - set_a
    out: dict[str, Any] = {
        "query": query,
        "counts": {
            "a": len(set_a),
            "b": len(set_b),
            "a_and_b": len(set_a & set_b),
            "a_not_b": len(a_only),
            "b_not_a": len(b_only),
        },
    }
    with get_engine().connect() as conn:
        out["a_not_b"] = _direction(conn, a_only, limit, offset)
        if both:
            out["b_not_a"] = _direction(conn, b_only, limit, offset)
    if analyze:
        out["characteristic_terms"] = analysis.characteristic_terms(a_only, b_only, threshold, term_exclude)
        if with_locations:
            out["characteristic_locations"] = analysis.characteristic_locations(a_only, b_only)
    return out


# ------------------------------------------------------------------ assignment endpoints
@app.get("/dissociate/terms/{term_a}/{term_b}", tags=["dissociation"])
def dissociate_terms(
    term_a: TermA,
    term_b: TermB,
    threshold: Threshold = 0.001,
    both: Both = False,
    analyze: Analyze = False,
    limit: Limit = None,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Studies that mention **term_a** but not **term_b**.

    Spaces in terms may be written as `_` (e.g. `posterior_cingulate`).
    """
    a, b = normalize_term(term_a), normalize_term(term_b)
    if a == b:
        raise InputError("The two terms are the same.", "Give two different terms.")
    return _dissociation(
        {"type": "terms", "a": a, "b": b, "threshold": threshold},
        _term_set(a, threshold),
        _term_set(b, threshold),
        both=both, analyze=analyze, limit=limit, offset=offset,
        term_exclude=(a, b), threshold=threshold, with_locations=True,
    )


@app.get("/dissociate/locations/{coords_a}/{coords_b}", tags=["dissociation"])
def dissociate_locations(
    coords_a: CoordsA,
    coords_b: CoordsB,
    radius: Radius = 6.0,
    both: Both = False,
    analyze: Analyze = False,
    limit: Limit = None,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Studies reporting activation near **[x1, y1, z1]** but not near **[x2, y2, z2]**.

    Coordinates are MNI millimetres written as `x_y_z`, e.g. `0_-52_26`.
    """
    a, b = parse_coords(coords_a), parse_coords(coords_b)
    if a == b:
        raise InputError("The two locations are the same.", "Give two different coordinates.")
    return _dissociation(
        {"type": "locations", "a": fmt_coords(a), "b": fmt_coords(b), "radius_mm": radius},
        _near_set(a, radius),
        _near_set(b, radius),
        both=both, analyze=analyze, limit=limit, offset=offset,
    )


# ------------------------------------------------------------------ single-set endpoints (template stubs, implemented)
@app.get("/terms/{term}/studies", tags=["lookup"])
def studies_by_term(term: str, threshold: Threshold = 0.001, limit: Limit = None, offset: Offset = 0) -> dict[str, Any]:
    """Studies that mention a term."""
    t = normalize_term(term)
    ids = _term_set(t, threshold)
    with get_engine().connect() as conn:
        return {"query": {"type": "term", "term": t, "threshold": threshold}, **_direction(conn, ids, limit, offset)}


@app.get("/locations/{coords}/studies", tags=["lookup"])
def studies_by_location(coords: str, radius: Radius = 6.0, limit: Limit = None, offset: Offset = 0) -> dict[str, Any]:
    """Studies that report a focus near an MNI coordinate."""
    xyz = parse_coords(coords)
    ids = _near_set(xyz, radius)
    with get_engine().connect() as conn:
        return {
            "query": {"type": "location", "xyz": fmt_coords(xyz), "radius_mm": radius},
            **_direction(conn, ids, limit, offset),
        }


@app.get("/terms", tags=["lookup"])
def list_terms(
    q_: Annotated[str, Query(alias="q", max_length=60, description="Part of a term; empty lists the most common.")] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 20,
) -> dict[str, Any]:
    """Search the term vocabulary (used for autocomplete)."""
    try:
        query = normalize_term(q_)
    except InputError:  # empty after normalisation: list the most common terms
        query = ""
    with get_engine().connect() as conn:
        return {"query": query, "terms": q.search_terms(conn, query, limit)}


# ------------------------------------------------------------------ smoke tests from the template
@app.get("/", response_class=HTMLResponse, tags=["service"])
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/health", tags=["service"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}


@app.get("/img", tags=["service"])
def show_img() -> FileResponse:
    return FileResponse(STATIC / "amygdala.gif", media_type="image/gif")


@app.get("/test_db", tags=["service"])
def test_db() -> JSONResponse:
    """Database connectivity check (same fields as the template)."""
    eng = get_engine()
    payload: dict[str, Any] = {"ok": False, "dialect": eng.dialect.name}
    try:
        with eng.connect() as conn:
            payload["version"] = conn.exec_driver_sql("SELECT version()").scalar()
            for key, table in (
                ("coordinates_count", "coordinates"),
                ("metadata_count", "metadata"),
                ("annotations_terms_count", "annotations_terms"),
                ("terms_count", "terms"),
            ):
                payload[key] = conn.execute(text(f"SELECT count(*) FROM ns.{table}")).scalar()
            payload["coordinates_sample"] = [dict(r) for r in conn.execute(text(
                "SELECT study_id, ST_X(geom) AS x, ST_Y(geom) AS y, ST_Z(geom) AS z FROM ns.coordinates LIMIT 3"
            )).mappings()]
            payload["metadata_sample"] = [dict(r) for r in conn.execute(text(
                "SELECT study_id, title, journal, year FROM ns.metadata LIMIT 3"
            )).mappings()]
            payload["annotations_terms_sample"] = [dict(r) for r in conn.execute(text(
                "SELECT study_id, contrast_id, term, weight FROM ns.annotations_terms LIMIT 3"
            )).mappings()]
            payload["build_info"] = dict(conn.execute(text("SELECT key, value FROM ns.build_info")).all())
        payload["ok"] = True
        return JSONResponse(payload)
    except Exception as exc:  # report, do not crash: this is a diagnostic endpoint
        payload["error"] = str(exc)
        return JSONResponse(payload, status_code=500)
