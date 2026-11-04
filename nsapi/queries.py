"""SQL for the API. Every function takes an open SQLAlchemy connection.

Definitions (Neurosynth conventions, both adjustable per request):
* a study *mentions a term* when its TF-IDF weight for that term > ``threshold``
  (default 0.001, the Neurosynth/NiMARE frequency threshold);
* a study *mentions a location* when any of its reported foci lies within
  ``radius`` mm (default 6 mm, as in the original neurosynth package) of it.
"""

from __future__ import annotations

import math
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


# ------------------------------------------------------------------ contrast statistics
def _log_odds(n_a: int, size_a: int, n_b: int, size_b: int) -> tuple[float, float]:
    """Log odds ratio (Haldane +0.5 correction) and its z score."""
    a, b = n_a + 0.5, size_a - n_a + 0.5
    c, d = n_b + 0.5, size_b - n_b + 0.5
    lor = math.log(a / b) - math.log(c / d)
    se = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return lor, lor / se


def _rank(rows: list[dict], size_a: int, size_b: int, k: int, min_count: int) -> dict[str, list[dict]]:
    scored = []
    for r in rows:
        lor, z = _log_odds(r["n_a"], size_a, r["n_b"], size_b)
        r.update(
            p_a=round(r["n_a"] / size_a, 4),
            p_b=round(r["n_b"] / size_b, 4),
            log_odds=round(lor, 3),
            z=round(z, 2),
        )
        scored.append(r)
    a_side = [r for r in scored if r["n_a"] >= min_count and r["z"] > 0]
    b_side = [r for r in scored if r["n_b"] >= min_count and r["z"] < 0]
    a_side.sort(key=lambda r: -r["z"])
    b_side.sort(key=lambda r: r["z"])
    return {"a_not_b": a_side[:k], "b_not_a": b_side[:k]}


def characteristic_terms(
    conn: Connection,
    a_only: frozenset[str],
    b_only: frozenset[str],
    threshold: float,
    exclude: Iterable[str] = (),
    k: int = 10,
) -> dict[str, list[dict]]:
    """Terms over-represented in A\\B relative to B\\A (and vice versa).

    ``exclude`` lists the query terms; any term sharing a word with them is left out.
    """
    if not a_only or not b_only:
        return {"a_not_b": [], "b_not_a": []}
    rows = conn.execute(
        text(
            "SELECT term, "
            "  count(*) FILTER (WHERE study_id = ANY(:a)) AS n_a, "
            "  count(*) FILTER (WHERE study_id = ANY(:b)) AS n_b "
            "FROM ns.annotations_terms "
            "WHERE study_id = ANY(:ab) AND weight > :threshold AND NOT (term = ANY(:exclude)) "
            "GROUP BY term"
        ),
        {
            "a": list(a_only),
            "b": list(b_only),
            "ab": list(a_only | b_only),
            "threshold": threshold,
            "exclude": list(exclude),
        },
    ).mappings()
    min_count = max(3, math.ceil(0.02 * min(len(a_only), len(b_only))))
    # Drop near-synonyms of the query terms ("cingulate cortex" for "posterior
    # cingulate"): they separate the sides trivially and say nothing new.
    query_words = {w for t in exclude for w in t.split()}
    rows = [dict(r) for r in rows if not query_words & set(r["term"].split())]
    return _rank(rows, len(a_only), len(b_only), k, min_count)


def characteristic_locations(
    conn: Connection,
    a_only: frozenset[str],
    b_only: frozenset[str],
    cell_mm: int = 10,
    k: int = 10,
) -> dict[str, list[dict]]:
    """Brain cells (cell_mm cubes) reported more often by A\\B than by B\\A studies."""
    if not a_only or not b_only:
        return {"a_not_b": [], "b_not_a": []}
    rows = conn.execute(
        text(
            "SELECT round(x / :c)::int * :c AS gx, round(y / :c)::int * :c AS gy, "
            "       round(z / :c)::int * :c AS gz, "
            "  count(DISTINCT study_id) FILTER (WHERE study_id = ANY(:a)) AS n_a, "
            "  count(DISTINCT study_id) FILTER (WHERE study_id = ANY(:b)) AS n_b "
            "FROM ns.coordinates WHERE study_id = ANY(:ab) GROUP BY 1, 2, 3"
        ),
        {"a": list(a_only), "b": list(b_only), "ab": list(a_only | b_only), "c": cell_mm},
    ).mappings()
    min_count = max(3, math.ceil(0.02 * min(len(a_only), len(b_only))))
    ranked = _rank([dict(r) for r in rows], len(a_only), len(b_only), k, min_count)
    for side in ranked.values():
        for r in side:
            r["center"] = [r.pop("gx"), r.pop("gy"), r.pop("gz")]
    return ranked
