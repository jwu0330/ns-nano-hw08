"""SQL for the API. Every function takes an open SQLAlchemy connection.

Definitions (Neurosynth conventions, both adjustable per request):
* a study *mentions a term* when its TF-IDF weight for that term > ``threshold``
  (default 0.001, the Neurosynth/NiMARE frequency threshold);
* a study *mentions a location* when any of its reported foci lies within
  ``radius`` mm (default 6 mm, as in the original neurosynth package) of it.
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import Connection, text

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
    """Closest vocabulary entries (trigram similarity, then substring)."""
    rows = conn.execute(
        text(
            "SELECT term FROM ns.terms "
            "WHERE term % :t OR term LIKE '%' || :t || '%' "
            "ORDER BY similarity(term, :t) DESC, n_studies DESC LIMIT :k"
        ),
        {"t": term, "k": k},
    ).scalars()
    return list(rows)


def search_terms(conn: Connection, q: str, limit: int) -> list[dict]:
    rows = conn.execute(
        text(
            "SELECT term, n_studies FROM ns.terms "
            "WHERE (:q = '' OR term LIKE '%' || :q || '%' OR term % :q) "
            "ORDER BY (term LIKE :q || '%') DESC, similarity(term, :q) DESC, n_studies DESC, term "
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
