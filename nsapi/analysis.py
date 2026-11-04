"""What characterises each side of a dissociation (the optional ``analyze=true`` output).

For every term (or 10 mm brain cell) it compares how often the A-only studies
and the B-only studies mention it, as a log odds ratio with its z score.

Counting uses a sparse study-by-feature index that ``scripts/load_db.py`` builds
from the same data and stores in ``ns.artifacts``; it is read once per process.
Aggregating the ~10^5 matching rows in PostgreSQL took 2-3 s per request on the
free-tier database, the in-memory count takes milliseconds.
"""

from __future__ import annotations

import io
import math
import threading
from dataclasses import dataclass
from functools import cached_property
from typing import Iterable

import numpy as np
from sqlalchemy import text

from .db import get_engine


class StaleIndex(RuntimeError):
    """The in-memory index does not match the database (it was reloaded); restart the service."""


@dataclass(frozen=True)
class AnalysisIndex:
    study_ids: np.ndarray  # sorted study ids (row order)
    vocab: np.ndarray  # term per column
    term_ptr: np.ndarray
    term_idx: np.ndarray
    term_w: np.ndarray
    cells: np.ndarray  # (n_cells, 3) grid point, mm
    cell_ptr: np.ndarray
    cell_idx: np.ndarray

    @classmethod
    def from_bytes(cls, blob: bytes) -> "AnalysisIndex":
        z = np.load(io.BytesIO(blob), allow_pickle=False)
        return cls(**{f: z[f] for f in cls.__dataclass_fields__})

    def rows(self, ids: Iterable[str]) -> np.ndarray:
        ids = np.asarray(sorted(ids), dtype=str)
        pos = np.searchsorted(self.study_ids, ids)
        if len(ids) and (pos.max() >= len(self.study_ids) or (self.study_ids[pos] != ids).any()):
            raise StaleIndex("study id missing from the analysis index")
        return pos

    @staticmethod
    def _gather(ptr: np.ndarray, rows: np.ndarray) -> np.ndarray:
        """Flat positions of all CSR entries in the given rows."""
        starts, ends = ptr[rows], ptr[rows + 1]
        lengths = ends - starts
        if not lengths.sum():
            return np.empty(0, dtype=np.int64)
        offsets = np.repeat(starts - np.concatenate([[0], np.cumsum(lengths)[:-1]]), lengths)
        return np.arange(lengths.sum()) + offsets

    def term_counts(self, ids: Iterable[str], threshold: float) -> np.ndarray:
        pos = self._gather(self.term_ptr, self.rows(ids))
        keep = pos[self.term_w[pos] > threshold]
        return np.bincount(self.term_idx[keep], minlength=len(self.vocab))

    def cell_counts(self, ids: Iterable[str]) -> np.ndarray:
        pos = self._gather(self.cell_ptr, self.rows(ids))
        return np.bincount(self.cell_idx[pos], minlength=len(self.cells))


class _Loader:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    @cached_property
    def index(self) -> AnalysisIndex:
        with get_engine().connect() as conn:
            blob = conn.execute(text("SELECT data FROM ns.artifacts WHERE name = 'analysis_index'")).scalar_one()
        return AnalysisIndex.from_bytes(bytes(blob))

    def get(self) -> AnalysisIndex:
        with self._lock:  # load once even under concurrent first requests
            return self.index


_loader = _Loader()


# ------------------------------------------------------------------ statistics
def log_odds(n_a: int, size_a: int, n_b: int, size_b: int) -> tuple[float, float]:
    """Log odds ratio (Haldane +0.5 correction) and its z score."""
    a, b = n_a + 0.5, size_a - n_a + 0.5
    c, d = n_b + 0.5, size_b - n_b + 0.5
    lor = math.log(a / b) - math.log(c / d)
    se = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return lor, lor / se


def _rank(labels: list, n_a: np.ndarray, n_b: np.ndarray, size_a: int, size_b: int, k: int) -> dict[str, list[dict]]:
    """Top-k features for each side; a feature must appear in >= max(3, 2 %) of that side."""
    min_a, min_b = (max(3, math.ceil(0.02 * size)) for size in (size_a, size_b))
    rows = []
    for label, na, nb in zip(labels, n_a.tolist(), n_b.tolist()):
        if na < min_a and nb < min_b:
            continue
        lor, z = log_odds(na, size_a, nb, size_b)
        rows.append({
            **label, "n_a": na, "n_b": nb,
            "p_a": round(na / size_a, 4), "p_b": round(nb / size_b, 4),
            "log_odds": round(lor, 3), "z": round(z, 2),
        })
    a_side = sorted((r for r in rows if r["n_a"] >= min_a and r["z"] > 0), key=lambda r: -r["z"])
    b_side = sorted((r for r in rows if r["n_b"] >= min_b and r["z"] < 0), key=lambda r: r["z"])
    return {"a_not_b": a_side[:k], "b_not_a": b_side[:k]}


def characteristic_terms(
    a_only: frozenset[str], b_only: frozenset[str], threshold: float, exclude: Iterable[str] = (), k: int = 10
) -> dict[str, list[dict]]:
    """Terms over-represented in A\\B relative to B\\A (and vice versa).

    Terms sharing a word with the query terms (``exclude``) are left out: near-synonyms
    such as "cingulate cortex" for "posterior cingulate" separate the sides trivially.
    """
    if not a_only or not b_only:
        return {"a_not_b": [], "b_not_a": []}
    idx = _loader.get()
    n_a, n_b = idx.term_counts(a_only, threshold), idx.term_counts(b_only, threshold)
    query_words = {w for t in exclude for w in t.split()}
    keep = [i for i, t in enumerate(idx.vocab.tolist()) if not query_words & set(t.split())]
    labels = [{"term": t} for t in idx.vocab[keep].tolist()]
    return _rank(labels, n_a[keep], n_b[keep], len(a_only), len(b_only), k)


def characteristic_locations(a_only: frozenset[str], b_only: frozenset[str], k: int = 10) -> dict[str, list[dict]]:
    """10 mm grid cells where A\\B studies report foci more often than B\\A studies."""
    if not a_only or not b_only:
        return {"a_not_b": [], "b_not_a": []}
    idx = _loader.get()
    n_a, n_b = idx.cell_counts(a_only), idx.cell_counts(b_only)
    labels = [{"center": c} for c in idx.cells.tolist()]
    return _rank(labels, n_a, n_b, len(a_only), len(b_only), k)
