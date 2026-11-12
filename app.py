"""Neurosynth functional-dissociation backend (NTU Psychoinformatics HW08).

Sections, top to bottom:
  1. database connection      (the URL comes only from the DB_URL environment variable)
  2. input parsing             (terms and x_y_z coordinates from the URL)
  3. SQL queries
  4. web application           (the two /dissociate endpoints, lookups, template smoke tests)

The optional analysis (``analyze=true``) lives in analysis.py; the database is built by load_db.py.
Definitions (Neurosynth conventions, both adjustable per request):
  * a study mentions a term when its TF-IDF weight > ``threshold`` (default 0.001);
  * a study mentions a location when one of its foci lies within ``radius`` mm (default 6).
Run locally: ``uvicorn app:app``; in production: ``gunicorn app:app -k uvicorn_worker.UvicornWorker``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Iterable

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

import analysis


# ============================================================================
# 1. database connection
# ============================================================================
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

# ============================================================================
# 2. input parsing
# ============================================================================
_NUM = r"[-+]?\d+(?:\.\d+)?"
_COORDS = re.compile(rf"^\s*({_NUM})\s*[_,]\s*({_NUM})\s*[_,]\s*({_NUM})\s*$")
MNI_LIMIT = 150.0  # mm; generously larger than the MNI bounding box
MAX_TERM = 60  # longest vocabulary term has 31 characters


class InputError(ValueError):
    """Bad user input; carries a hint that the API returns to the client."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


def parse_coords(text: str) -> tuple[float, float, float]:
    """``"0_-52_26"`` -> ``(0.0, -52.0, 26.0)``. Commas are accepted too."""
    m = _COORDS.match(text)
    if not m:
        raise InputError(
            f"Cannot read coordinates {text!r}.",
            "Use x_y_z in MNI millimetres, e.g. 0_-52_26.",
        )
    xyz = tuple(float(v) for v in m.groups())
    if any(abs(v) > MNI_LIMIT for v in xyz):
        raise InputError(
            f"Coordinates {text!r} are outside the brain.",
            f"Each value must be within ±{MNI_LIMIT:g} mm.",
        )
    return xyz  # type: ignore[return-value]


def normalize_term(text: str) -> str:
    """URL form -> vocabulary form: underscores/pluses become spaces, lower case."""
    term = re.sub(r"[_+\s]+", " ", text).strip().lower()
    if not term:
        raise InputError("Empty term.", "Give a Neurosynth term, e.g. amygdala.")
    if not re.fullmatch(r"[\w\s'-]+", term):  # vocabulary terms are letters, digits and spaces
        raise InputError("Terms may contain only letters, digits and spaces.", "Give a Neurosynth term, e.g. amygdala.")
    if len(term) > MAX_TERM:
        raise InputError(f"Term is longer than {MAX_TERM} characters.", "Give a Neurosynth term, e.g. amygdala.")
    return term


def fmt_coords(xyz: tuple[float, float, float]) -> list[float]:
    return [int(v) if float(v).is_integer() else v for v in xyz]

# ============================================================================
# 3. SQL queries
# ============================================================================
# ------------------------------------------------------------------ study sets
SQL_TERM_STUDIES = text(
    "SELECT study_id FROM ns.annotations_terms WHERE term = :term AND weight > :threshold"
)
# ST_3DDWithin uses the n-D GiST index (coordinates_geom_nd); distances are in mm.
SQL_NEAR_STUDIES = text(
    "SELECT DISTINCT study_id FROM ns.coordinates "
    "WHERE ST_3DDWithin(geom, ST_MakePoint(:x, :y, :z), :radius)"
)


def term_studies(conn: Connection, term: str, threshold: float) -> frozenset[str]:
    return frozenset(conn.execute(SQL_TERM_STUDIES, {"term": term, "threshold": threshold}).scalars())


def near_studies(conn: Connection, xyz: tuple[float, float, float], radius: float) -> frozenset[str]:
    x, y, z = xyz
    rows = conn.execute(SQL_NEAR_STUDIES, {"x": x, "y": y, "z": z, "radius": radius}).scalars()
    return frozenset(rows)


# ------------------------------------------------------------------ vocabulary
def term_exists(conn: Connection, term: str) -> bool:
    return conn.execute(text("SELECT 1 FROM ns.terms WHERE term = :t"), {"t": term}).first() is not None


def suggest_terms(conn: Connection, term: str, k: int = 5) -> list[str]:
    """Closest vocabulary entries (trigram similarity or substring; no LIKE wildcards)."""
    rows = conn.execute(
        text(
            "SELECT term FROM ns.terms "
            "WHERE term % :t OR strpos(term, :t) > 0 "
            "ORDER BY similarity(term, :t) DESC, n_studies DESC LIMIT :k"
        ),
        {"t": term, "k": k},
    ).scalars()
    return list(rows)


def search_terms(conn: Connection, q: str, limit: int) -> list[dict]:
    rows = conn.execute(
        text(
            "SELECT term, n_studies FROM ns.terms "
            "WHERE (:q = '' OR strpos(term, :q) > 0 OR term % :q) "
            "ORDER BY (strpos(term, :q) = 1) DESC, similarity(term, :q) DESC, n_studies DESC, term "
            "LIMIT :limit"
        ),
        {"q": q, "limit": limit},
    ).mappings()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ study details
def study_details(conn: Connection, ids: Iterable[str], limit: int | None, offset: int) -> list[dict]:
    """Metadata for the given studies, newest first."""
    ids = list(ids)
    if not ids:
        return []
    rows = conn.execute(
        text(
            "SELECT study_id, title, authors, journal, year FROM ns.metadata "
            "WHERE study_id = ANY(:ids) ORDER BY year DESC, study_id "
            "LIMIT :limit OFFSET :offset"
        ),
        {"ids": ids, "limit": limit, "offset": offset},
    ).mappings()
    return [dict(r) for r in rows]

# ============================================================================
# 4. web application
# ============================================================================
STATIC = Path(__file__).resolve().parent / "static"
VERSION = "1.1.0"

log = logging.getLogger("app")
analysis.configure(get_engine)


def _warm_up() -> None:
    try:
        analysis.get_index()
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


# Transient conditions -> 503 (retry). Connection loss, statement timeout, a full pool,
# or data reloaded under a running service (caches are dropped so the retry reloads).
@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
@app.exception_handler(PoolTimeout)
@app.exception_handler(analysis.StaleIndex)
async def _unavailable(_: Request, exc: Exception) -> JSONResponse:
    log.warning("service unavailable: %r", exc)
    if isinstance(exc, analysis.StaleIndex):
        _term_set.cache_clear()
        _near_set.cache_clear()
        analysis.reset()
    return JSONResponse(
        status_code=503,
        content={"error": {"message": "The database is busy or was just reloaded; please retry in a moment."}},
        headers={"Retry-After": "10"},
    )


@app.exception_handler(Exception)
async def _bug(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")  # real bugs are logged in full and not disguised as "retry"
    return JSONResponse(status_code=500, content={"error": {"message": "Internal server error."}})


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = "No such endpoint. See /docs." if exc.status_code == 404 else str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": {"message": message}}, headers=exc.headers)


@app.middleware("http")
async def _timing_and_cache(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
    # The data only changes when load_db.py is rerun (followed by a restart),
    # so successful lookups can be cached by browsers for an hour.
    if response.status_code == 200 and request.url.path.startswith(("/dissociate", "/terms", "/locations", "/lotus")):
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
DEFAULT_LIMIT = 1000  # every example in the template returns fewer studies than this
Limit = Annotated[int, Query(ge=1, le=20000, description="Maximum studies listed per direction; `count` is always the full total.")]
Offset = Annotated[int, Query(ge=0, le=1_000_000, description="Skip this many studies (for paging).")]
TermA = Annotated[str, PathParam(description="Term A (spaces or `_`).", openapi_examples={"PCC": {"value": "posterior cingulate"}})]
TermB = Annotated[str, PathParam(description="Term B (spaces or `_`).", openapi_examples={"vmPFC": {"value": "ventromedial prefrontal"}})]
CoordsA = Annotated[str, PathParam(description="Location A as `x_y_z` (MNI mm).", openapi_examples={"PCC": {"value": "0_-52_26"}})]
CoordsB = Annotated[str, PathParam(description="Location B as `x_y_z` (MNI mm).", openapi_examples={"vmPFC": {"value": "-2_50_-6"}})]


# Study-id sets are pure functions of the (static) database, so they are memoised.
# The cache is small on purpose: a set can hold ~8,000 ids (~1 MB) and keys are
# arbitrary floats, so a large cache could exhaust the 512 MB free instance.
@lru_cache(maxsize=32)
def _term_set(term: str, threshold: float) -> frozenset[str]:
    with get_engine().connect() as conn:
        if not term_exists(conn, term):
            raise NotFound(f"Term {term!r} is not in the Neurosynth vocabulary.", suggest_terms(conn, term))
        return term_studies(conn, term, threshold)


@lru_cache(maxsize=32)
def _near_set(xyz: tuple[float, float, float], radius: float) -> frozenset[str]:
    with get_engine().connect() as conn:
        return near_studies(conn, xyz, radius)


def _direction(conn, ids: frozenset[str], limit: int, offset: int) -> dict[str, Any]:
    return {"count": len(ids), "studies": study_details(conn, ids, limit, offset)}


def _dissociation(
    query: dict[str, Any],
    set_a: frozenset[str],
    set_b: frozenset[str],
    *,
    both: bool,
    analyze: bool,
    limit: int,
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
    limit: Limit = DEFAULT_LIMIT,
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
    limit: Limit = DEFAULT_LIMIT,
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
def studies_by_term(term: str, threshold: Threshold = 0.001, limit: Limit = DEFAULT_LIMIT, offset: Offset = 0) -> dict[str, Any]:
    """Studies that mention a term."""
    t = normalize_term(term)
    ids = _term_set(t, threshold)
    with get_engine().connect() as conn:
        return {"query": {"type": "term", "term": t, "threshold": threshold}, **_direction(conn, ids, limit, offset)}


@app.get("/locations/{coords}/studies", tags=["lookup"])
def studies_by_location(coords: str, radius: Radius = 6.0, limit: Limit = DEFAULT_LIMIT, offset: Offset = 0) -> dict[str, Any]:
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
    try:  # ignore characters no term contains (e.g. "amyg%" searches "amyg")
        query = normalize_term(re.sub(r"[^\w\s'-]", "", q_))
    except InputError:  # nothing left: list the most common terms
        query = ""
    with get_engine().connect() as conn:
        return {"query": query, "terms": search_terms(conn, query, limit)}


# ------------------------------------------------------------------ smoke tests from the template
@app.get("/", response_class=HTMLResponse, tags=["service"])
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/stats", tags=["service"])
def stats() -> dict[str, Any]:
    """What is loaded: row counts, source and load time (cheap; used by the web page)."""
    with get_engine().connect() as conn:
        info = dict(conn.execute(text("SELECT key, value FROM ns.build_info")).all())
    return {"row_counts": json.loads(info.pop("row_counts")), **info}


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
        log.exception("test_db failed")
        payload["error"] = type(exc).__name__  # details go to the log, not to the client
        return JSONResponse(payload, status_code=500)


# ============================================================================
# 5. LoTUS-BF compatible API (added for HW09, 2026-11-12)
# ============================================================================
# The HW09 front end (LoTUS-BF template) speaks the course LoTUS API. The course server
# was down on 2026-11-12, so the same endpoints are served here from the HW08 database:
#   /lotus/terms, /lotus/terms/{term}, /lotus/query/{q}/studies|locations|nii, /lotus/decode/{x_y_z}
# Query syntax: terms and [x,y,z] combined with AND / OR / NOT and parentheses.
# Definitions as in HW08: TF-IDF > 0.001 for terms; foci within r mm (default 6) for locations.
import gzip
import math
import struct

import numpy as np
from fastapi import APIRouter
from fastapi.responses import Response

lotus = APIRouter(prefix="/lotus", tags=["lotus (HW09)"])
LOTUS_THRESHOLD = 0.001
_TOKEN = re.compile(r"\[[^\]]*\]|\(|\)|[^\s()\[\]]+")
_OPS = {"AND", "OR", "NOT"}


class LotusError(Exception):
    """Errors in the course-API shape: {"error": "message"}."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


@app.exception_handler(LotusError)
async def _lotus_error(_: Request, exc: LotusError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": exc.message})


@lru_cache(maxsize=1)
def _universe() -> frozenset[str]:
    with get_engine().connect() as conn:
        return frozenset(conn.execute(text("SELECT study_id FROM ns.metadata")).scalars())


def _tokens(q: str) -> list[tuple[str, Any]]:
    """Words next to each other form one term ("default mode"); operators are AND/OR/NOT in any case."""
    out: list[tuple[str, Any]] = []
    for tok in _TOKEN.findall(q):
        if tok in "()":
            out.append((tok, None))
        elif tok.startswith("["):
            try:
                xyz = parse_coords(tok[1:-1])
            except InputError as e:
                raise LotusError(f"{e.message} {e.hint or ''}".strip())
            out.append(("loc", xyz))
        elif tok.upper() in _OPS:
            out.append((tok.upper(), None))
        elif out and out[-1][0] == "term":
            out[-1] = ("term", out[-1][1] + " " + tok)
        else:
            out.append(("term", tok))
    return out


class _Parser:
    """expr := and (OR and)* ; and := unary ([AND] unary)* ; unary := NOT unary | ( expr ) | term | [x,y,z]"""

    def __init__(self, q: str, r: float):
        self.toks, self.i, self.r = _tokens(q), 0, r
        self.terms: list[str] = []
        self.locations: list[list[float]] = []
        if not self.toks:
            raise LotusError("Empty query.")

    def peek(self):
        return self.toks[self.i][0] if self.i < len(self.toks) else None

    def take(self):
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self) -> frozenset[str]:
        result = self.expr()
        if self.peek() is not None:
            raise LotusError(f"Unexpected {self.peek()!r} in the query.")
        return result

    def expr(self):
        result = self.and_()
        while self.peek() == "OR":
            self.take()
            result = result | self.and_()
        return result

    def and_(self):
        result = self.unary()
        while self.peek() in ("AND", "NOT", "(", "term", "loc"):
            if self.peek() == "AND":
                self.take()
            result = result & self.unary()
        return result

    def unary(self):
        kind = self.peek()
        if kind is None:
            raise LotusError("The query ends where a term or [x,y,z] is expected.")
        kind, value = self.take()
        if kind == "NOT":
            return _universe() - self.unary()
        if kind == "(":
            result = self.expr()
            if self.peek() != ")":
                raise LotusError("Missing closing parenthesis.")
            self.take()
            return result
        if kind == "term":
            try:
                term = normalize_term(value)
                ids = _term_set(term, LOTUS_THRESHOLD)
            except InputError as e:
                raise LotusError(e.message)
            except NotFound as e:
                hint = f" Did you mean: {', '.join(e.suggestions)}?" if e.suggestions else ""
                raise LotusError(e.message + hint, 404)
            self.terms.append(term)
            return ids
        if kind == "loc":
            self.locations.append(fmt_coords(value))
            return _near_set(value, self.r)
        raise LotusError(f"Unexpected {kind!r} in the query.")


LotusR = Annotated[float, Query(gt=0, le=30, description="Radius (mm) for [x,y,z] terms.")]


def _run(q: str, r: float) -> tuple[frozenset[str], dict[str, Any]]:
    p = _Parser(q, r)
    ids = p.parse()
    return ids, {"query": q, "terms": p.terms, "locations": p.locations, "r": r, "threshold": LOTUS_THRESHOLD}


def _coords_of(ids: Iterable[str]) -> list[dict]:
    ids = list(ids)
    if not ids:
        return []
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT study_id, ST_X(geom) AS x, ST_Y(geom) AS y, ST_Z(geom) AS z "
            "FROM ns.coordinates WHERE study_id = ANY(:ids) ORDER BY study_id"
        ), {"ids": ids}).mappings()
        return [dict(r) for r in rows]


@lotus.get("/terms")
def lotus_terms() -> dict[str, Any]:
    """All vocabulary terms that at least one study mentions."""
    with get_engine().connect() as conn:
        return {"terms": list(conn.execute(text("SELECT term FROM ns.terms WHERE n_studies > 0 ORDER BY term")).scalars())}


@lotus.get("/terms/{term}")
def lotus_related(term: str, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    """Terms that co-occur with `term` (shared studies and Jaccard overlap)."""
    try:
        t = normalize_term(term)
    except InputError as e:
        raise LotusError(e.message)
    try:
        a = _term_set(t, LOTUS_THRESHOLD)
    except NotFound as e:
        raise LotusError(e.message, 404)
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT at.term, count(DISTINCT at.study_id) AS co, max(t.n_studies) AS n "
            "FROM ns.annotations_terms at JOIN ns.terms t USING (term) "
            "WHERE at.study_id = ANY(:ids) AND at.weight > :th AND at.term <> :t GROUP BY at.term"
        ), {"ids": list(a), "th": LOTUS_THRESHOLD, "t": t}).all()
    related = [{"term": term_, "co_count": co, "jaccard": co / (len(a) + n - co)} for term_, co, n in rows if len(a) + n - co > 0]
    related.sort(key=lambda d: (-d["jaccard"], -d["co_count"], d["term"]))
    return {"term": t, "count": len(a), "related": related[:limit]}


@lotus.get("/query/{q}/studies")
def lotus_studies(q: str, r: LotusR = 6.0) -> dict[str, Any]:
    ids, applied = _run(q, r)
    with get_engine().connect() as conn:
        rows = study_details(conn, ids, None, 0)
    for row in rows:
        row["id"] = row["study_id"]
    return {"applied": applied, "count": len(ids), "results": rows}


@lotus.get("/query/{q}/locations")
def lotus_locations(q: str, r: LotusR = 6.0) -> dict[str, Any]:
    ids, applied = _run(q, r)
    rows = _coords_of(ids)
    return {"applied": applied, "location_counts": len(rows), "study_counts": len({d["study_id"] for d in rows}), "results": rows}


# MNI152 2 mm grid used by the front end: x = -2i + 90, y = 2j - 126, z = 2k - 72.
GRID = (91, 109, 91)
SROW = ((-2.0, 0.0, 0.0, 90.0), (0.0, 2.0, 0.0, -126.0), (0.0, 0.0, 2.0, -72.0))


def _smooth_matrix(n: int, kernel: np.ndarray) -> np.ndarray:
    """n x n matrix that convolves a length-n axis with a centred 1-D kernel (zero padding)."""
    half = len(kernel) // 2
    m = np.zeros((n, n), dtype=np.float32)
    for off, w in enumerate(kernel, start=-half):
        m += np.eye(n, k=off, dtype=np.float32) * w
    return m


def _density(coords: list[dict], fwhm: float, kernel: str) -> np.ndarray:
    """MKDA-style map: each study counts once per voxel, then a separable Gaussian or box filter."""
    vol = np.zeros(GRID, dtype=np.float32)
    if coords:
        xyz = np.array([[c["x"], c["y"], c["z"]] for c in coords], dtype=np.float64)
        ijk = np.rint([(90 - xyz[:, 0]) / 2, (xyz[:, 1] + 126) / 2, (xyz[:, 2] + 72) / 2]).astype(int).T
        ok = np.all((ijk >= 0) & (ijk < np.array(GRID)), axis=1)
        study = np.array([c["study_id"] for c in coords])[ok]
        keys = {(s, *v) for s, v in zip(study, map(tuple, ijk[ok]))}  # one vote per study per voxel
        if keys:
            idx = np.array([k[1:] for k in keys])
            np.add.at(vol, (idx[:, 0], idx[:, 1], idx[:, 2]), 1.0)
    if fwhm > 0:
        if kernel == "uniform":
            width = max(1, int(round(fwhm / 2)))
            k1 = np.ones(2 * (width // 2) + 1, dtype=np.float32)
            k1 /= k1.sum()
        else:
            sigma = fwhm / (2 * math.sqrt(2 * math.log(2))) / 2.0  # in 2 mm voxels
            half = max(1, int(math.ceil(3 * sigma)))
            k1 = np.exp(-0.5 * (np.arange(-half, half + 1) / sigma) ** 2).astype(np.float32)
            k1 /= k1.sum()
        for axis, n in enumerate(GRID):
            vol = np.moveaxis(np.tensordot(_smooth_matrix(n, k1), np.moveaxis(vol, axis, 0), axes=1), 0, axis)
    return vol


def _nifti_gz(vol: np.ndarray, description: str) -> bytes:
    """Minimal NIfTI-1 (.nii.gz) writer, float32, MNI sform."""
    hdr = struct.pack(
        "<i10s18sihcc8h3f4h8f3fhcc4f2i80s24s2h6f4f4f4f16s4s",
        348, b"", b"", 0, 0, b"r", b"\0",
        3, *GRID, 1, 1, 1, 1,
        0.0, 0.0, 0.0,
        0, 16, 32, 0,
        1.0, 2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0,
        352.0, 1.0, 0.0,
        0, b"\0", bytes([10]),
        float(vol.max()), 0.0, 0.0, 0.0,
        0, 0,
        description.encode()[:79], b"",
        0, 4,
        0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        *SROW[0], *SROW[1], *SROW[2],
        b"", b"n+1\0",
    )
    return gzip.compress(hdr + b"\0\0\0\0" + vol.astype("<f4").tobytes(order="F"), compresslevel=6)


@lotus.get("/query/{q}/nii")
def lotus_nii(
    q: str,
    r: LotusR = 6.0,
    fwhm: Annotated[float, Query(ge=0, le=30)] = 10.0,
    kernel: Annotated[str, Query(pattern="^(gauss|uniform)$")] = "gauss",
    voxel: Annotated[float, Query(description="Only 2 mm is supported (MNI152 2 mm grid).")] = 2.0,
) -> Response:
    ids, _ = _run(q, r)
    vol = _density(_coords_of(ids), fwhm, kernel)
    body = _nifti_gz(vol, f"LoTUS-BF HW09 {len(ids)} studies")
    return Response(body, media_type="application/gzip",
                    headers={"Content-Disposition": 'attachment; filename="lotus_map.nii.gz"', "X-Study-Count": str(len(ids))})


@lotus.get("/decode/{coords}")
def lotus_decode(coords: str, r: LotusR = 6.0, limit: Annotated[int, Query(ge=1, le=100)] = 30) -> dict[str, Any]:
    """Reverse inference: terms over-represented among studies reporting foci near [x, y, z]."""
    try:
        xyz = parse_coords(coords)
    except InputError as e:
        raise LotusError(f"{e.message} {e.hint or ''}".strip())
    ids = _near_set(xyz, r)
    total = len(_universe())
    out: dict[str, Any] = {"xyz": fmt_coords(xyz), "r": r, "n_studies": len(ids), "n_total": total, "terms": []}
    if not ids:
        return out
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT at.term, count(DISTINCT at.study_id) AS k, max(t.n_studies) AS n "
            "FROM ns.annotations_terms at JOIN ns.terms t USING (term) "
            "WHERE at.study_id = ANY(:ids) AND at.weight > :th GROUP BY at.term"
        ), {"ids": list(ids), "th": LOTUS_THRESHOLD}).all()
    min_k = max(3, math.ceil(0.05 * len(ids)))
    terms = [
        {"term": term, "k": k, "rate": k / len(ids), "base_rate": n / total, "lift": (k / len(ids)) / (n / total)}
        for term, k, n in rows if k >= min_k and n
    ]
    terms.sort(key=lambda d: (-d["lift"], -d["k"]))
    out["terms"] = terms[:limit]
    return out


app.include_router(lotus)
