#!/usr/bin/env python3
"""Load the Neurosynth parquet files into PostgreSQL + PostGIS.

Replaces the teacher's ``create_db.py`` (which is left untouched) and fixes
three issues found in it:

1. Term names keep the ``terms_abstract_tfidf__`` prefix there, because the
   regex ``^terms_[^_]*__`` never matches; here the prefix is removed so that
   ``posterior cingulate`` can be queried directly.
2. MNI coordinates are millimetres, not longitude/latitude, so geometries use
   SRID 0 (plain Cartesian) instead of 4326.
3. Distance queries use ``ST_3DDWithin``, which needs an n-D GiST index
   (``gist_geometry_ops_nd``); with the default 2-D index the lookup is ~130x slower.

It also stores a compact sparse index (``ns.artifacts``) that the optional
analysis uses, because aggregating ~10^5 rows per request is too slow on a
free-tier database.

Tables keep the teacher's names (``ns.coordinates``, ``ns.metadata``,
``ns.annotations_terms``) so his ``/test_db`` query still works.

Everything is built in a staging schema and swapped in with one transaction,
so the live API never sees a half-loaded database.

Usage:
    python load_db.py --url "$DB_URL" [--data-dir ../raw/ns-nano]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = Path(os.getenv("NS_DATA_DIR", HERE.parent / "raw" / "ns-nano"))
TERM_PREFIX_SEP = "__"  # column names look like terms_abstract_tfidf__<term>
SCHEMA = "ns"
STAGE = "ns_build"


def conninfo(url: str) -> str:
    """psycopg wants a plain libpq URL; strip SQLAlchemy driver suffixes."""
    for scheme in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgres://"):
        if url.startswith(scheme):
            return "postgresql://" + url[len(scheme):]
    return url


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- read + check
def read_inputs(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    coords = pd.read_parquet(data_dir / "coordinates.parquet")
    meta = pd.read_parquet(data_dir / "metadata.parquet")
    ann = pd.read_parquet(data_dir / "annotations.parquet")

    for name, df in (("coordinates", coords), ("metadata", meta), ("annotations", ann)):
        if df["study_id"].isna().any():
            raise ValueError(f"{name}: missing study_id")
    if not meta["study_id"].is_unique or not ann["study_id"].is_unique:
        raise ValueError("metadata/annotations must have one row per study")
    xyz = coords[["x", "y", "z"]].to_numpy(dtype=float)
    if not np.isfinite(xyz).all():
        raise ValueError("coordinates contain non-finite values")
    return coords, meta, ann


def melt_terms(ann: pd.DataFrame) -> pd.DataFrame:
    """Wide TF-IDF matrix -> sparse (study_id, contrast_id, term, weight) rows with weight > 0."""
    term_cols = [c for c in ann.columns if str(c).startswith("terms_")]
    terms = [str(c).split(TERM_PREFIX_SEP, 1)[1].strip().lower() for c in term_cols]
    if len(set(terms)) != len(terms):
        raise ValueError("term names collide after removing the prefix")
    w = ann[term_cols].to_numpy(dtype=float)
    rows, cols = np.nonzero(np.isfinite(w) & (w > 0))
    return pd.DataFrame({
        "study_id": ann["study_id"].to_numpy()[rows],
        "contrast_id": ann["contrast_id"].to_numpy()[rows],
        "term": np.asarray(terms, dtype=object)[cols],
        "weight": w[rows, cols],
    })


CELL_MM = 10  # grid used by the "characteristic locations" analysis


def build_index(meta: pd.DataFrame, coords: pd.DataFrame, terms: pd.DataFrame) -> bytes:
    """Compact in-memory index for the analysis endpoints (stored in ns.artifacts).

    Two sparse study-by-feature matrices in CSR layout, rows = studies in
    ``study_ids`` order:
    * terms: column = term id, value = TF-IDF weight (float64, same as the table);
    * cells: column = id of the 10 mm grid cell (nearest grid point) holding at
      least one of the study's foci.
    The analysis then counts features over a set of studies with one bincount,
    instead of aggregating ~10^5 rows in PostgreSQL per request.
    """
    def strings(values) -> np.ndarray:  # fixed-width unicode, loadable without pickle
        return np.asarray(list(values), dtype=str)

    study_ids = np.sort(strings(meta["study_id"]))
    vocab = np.sort(strings(terms["term"].unique()))

    s_idx = np.searchsorted(study_ids, strings(terms["study_id"]))
    t_idx = np.searchsorted(vocab, strings(terms["term"]))
    order = np.lexsort((t_idx, s_idx))
    term_ptr = np.concatenate([[0], np.cumsum(np.bincount(s_idx, minlength=len(study_ids)))])

    # nearest grid point, halves rounded up (np.rint would round halves to even and
    # bias the 11 % of foci that sit exactly between two grid points)
    grid = np.floor(coords[["x", "y", "z"]].to_numpy(dtype=float) / CELL_MM + 0.5).astype(np.int16) * CELL_MM
    cells, cell_of_focus = np.unique(grid, axis=0, return_inverse=True)
    c_study = np.searchsorted(study_ids, strings(coords["study_id"]))
    pairs = np.unique(np.stack([c_study, cell_of_focus.ravel()], axis=1), axis=0)  # one per study and cell
    cell_ptr = np.concatenate([[0], np.cumsum(np.bincount(pairs[:, 0], minlength=len(study_ids)))])

    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        study_ids=study_ids,
        vocab=vocab,
        term_ptr=term_ptr.astype(np.int32),
        term_idx=t_idx[order].astype(np.int16),
        term_w=terms["weight"].to_numpy()[order].astype(np.float64),
        cells=cells.astype(np.int16),
        cell_ptr=cell_ptr.astype(np.int32),
        cell_idx=pairs[:, 1].astype(np.int32),
        cell_mm=np.int16(CELL_MM),
    )
    return buf.getvalue()


# ---------------------------------------------------------------- write
def copy_df(cur: psycopg.Cursor, table: str, df: pd.DataFrame) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    cols = ", ".join(df.columns)
    with cur.copy(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '\\N')") as cp:
        while chunk := buf.read(1 << 20):
            cp.write(chunk)


DDL = f"""
-- Extensions live in public: if they sat in "ns", replacing the schema would drop them.
CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public;
DROP SCHEMA IF EXISTS {STAGE} CASCADE;
CREATE SCHEMA {STAGE};

CREATE TABLE {STAGE}.metadata (
    id          TEXT NOT NULL,
    study_id    TEXT PRIMARY KEY,
    contrast_id TEXT,
    authors     TEXT,
    journal     TEXT,
    year        INTEGER,
    title       TEXT,
    fts tsvector GENERATED ALWAYS AS (
        to_tsvector('english'::regconfig,
            coalesce(title, '') || ' ' || coalesce(authors, '') || ' ' || coalesce(journal, ''))
    ) STORED
);

CREATE TABLE {STAGE}.coordinates (
    study_id TEXT NOT NULL,
    x DOUBLE PRECISION NOT NULL,
    y DOUBLE PRECISION NOT NULL,
    z DOUBLE PRECISION NOT NULL,
    geom geometry(PointZ, 0) GENERATED ALWAYS AS (ST_MakePoint(x, y, z)) STORED
);

CREATE TABLE {STAGE}.annotations_terms (
    study_id    TEXT NOT NULL,
    contrast_id TEXT,
    term        TEXT NOT NULL,
    weight      DOUBLE PRECISION NOT NULL
);

CREATE TABLE {STAGE}.build_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE {STAGE}.artifacts (name TEXT PRIMARY KEY, data BYTEA NOT NULL);
"""

INDEXES = f"""
ALTER TABLE {STAGE}.annotations_terms ADD PRIMARY KEY (term, study_id);
CREATE INDEX ON {STAGE}.annotations_terms (study_id);
CREATE INDEX ON {STAGE}.coordinates (study_id);
CREATE INDEX coordinates_geom_nd ON {STAGE}.coordinates USING GIST (geom gist_geometry_ops_nd);
CREATE INDEX ON {STAGE}.metadata USING GIN (fts);

CREATE TABLE {STAGE}.terms AS
    SELECT term, count(*)::int AS n_studies
    FROM {STAGE}.annotations_terms GROUP BY term;
ALTER TABLE {STAGE}.terms ADD PRIMARY KEY (term);
CREATE INDEX terms_trgm ON {STAGE}.terms USING GIN (term gin_trgm_ops);

ALTER TABLE {STAGE}.coordinates ADD FOREIGN KEY (study_id) REFERENCES {STAGE}.metadata (study_id);
ALTER TABLE {STAGE}.annotations_terms ADD FOREIGN KEY (study_id) REFERENCES {STAGE}.metadata (study_id);
"""


def source_commit(data_dir: Path) -> str:
    src = data_dir.parent / "SOURCE.txt"
    if src.exists():
        return src.read_text(encoding="utf-8").strip()
    try:
        return subprocess.check_output(["git", "-C", str(data_dir), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.getenv("DB_URL"), help="PostgreSQL URL (default: $DB_URL)")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA, help="folder with the three parquet files")
    args = ap.parse_args()
    if not args.url:
        ap.error("--url or DB_URL is required")

    t0 = time.time()
    log(f"reading parquet from {args.data_dir}")
    coords, meta, ann = read_inputs(args.data_dir)
    terms = melt_terms(ann)
    expected = {
        "metadata": len(meta),
        "coordinates": len(coords),
        "annotations_terms": len(terms),
        "terms": terms["term"].nunique(),
    }
    log(f"expected rows: {expected}")

    with psycopg.connect(conninfo(args.url), autocommit=False) as conn:
        with conn.cursor() as cur:
            log("creating staging schema")
            cur.execute(DDL)
            log("copying metadata")
            copy_df(cur, f"{STAGE}.metadata",
                    meta[["id", "study_id", "contrast_id", "authors", "journal", "year", "title"]])
            log("copying coordinates")
            copy_df(cur, f"{STAGE}.coordinates", coords[["study_id", "x", "y", "z"]])
            log(f"copying {len(terms):,} annotation rows")
            copy_df(cur, f"{STAGE}.annotations_terms", terms)
            log("building indexes and the term table")
            cur.execute(INDEXES)
            index_blob = build_index(meta, coords, terms)
            cur.execute(f"INSERT INTO {STAGE}.artifacts VALUES ('analysis_index', %s)", (index_blob,))
            log(f"stored analysis index ({len(index_blob) / 1e6:.1f} MB)")

            got = {t: cur.execute(f"SELECT count(*) FROM {STAGE}.{t}").fetchone()[0] for t in expected}
            if got != expected:
                raise RuntimeError(f"row counts differ: expected {expected}, got {got}")
            info = {
                "source": source_commit(args.data_dir),
                "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "row_counts": json.dumps(got),
                "term_threshold_note": "weights are TF-IDF; only weight > 0 rows are stored",
                "srid": "0 (MNI millimetres)",
            }
            cur.executemany(f"INSERT INTO {STAGE}.build_info VALUES (%s, %s)", list(info.items()))
            # the staged schema must answer a real location query before it goes live
            n_near = cur.execute(
                f"SELECT count(DISTINCT study_id) FROM {STAGE}.coordinates "
                "WHERE ST_3DDWithin(geom, ST_MakePoint(0, -52, 26), 6)"
            ).fetchone()[0]
            if not n_near:
                raise RuntimeError("check failed: no studies near [0, -52, 26] in the staged schema")
            conn.commit()

            log("swapping staging schema into place")
            in_schema = cur.execute(
                "SELECT extname FROM pg_extension WHERE extnamespace::regnamespace::text IN (%s, %s)", (SCHEMA, STAGE)
            ).fetchall()
            if in_schema:
                raise RuntimeError(f"extensions {in_schema} live in {SCHEMA}/{STAGE}; dropping it would remove them")
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute(f"ALTER SCHEMA {STAGE} RENAME TO {SCHEMA}")
            conn.commit()

        conn.autocommit = True
        for t in ("metadata", "coordinates", "annotations_terms", "terms"):
            conn.execute(f"ANALYZE {SCHEMA}.{t}")
        size = conn.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]

    log(f"done in {time.time() - t0:.0f}s; rows {got}; database size {size}")
    log("restart the web service so its caches and analysis index pick up the new data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
