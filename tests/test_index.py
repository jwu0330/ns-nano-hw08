"""The sparse analysis index built by scripts/load_db.py, on hand-made data (no database)."""

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nsapi.analysis import AnalysisIndex, StaleIndex

spec = importlib.util.spec_from_file_location("load_db", Path(__file__).parents[1] / "scripts" / "load_db.py")
load_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load_db)


@pytest.fixture(scope="module")
def index():
    meta = pd.DataFrame({"study_id": ["s2", "s1", "s3"]})
    coords = pd.DataFrame({
        "study_id": ["s1", "s1", "s1", "s1", "s2", "s2", "s3"],
        "x": [5.0, -5.0, 15.0, 24.9, 5.0, 5.0, 0.0],  # halves round up: 5->10, -5->0, 15->20; 24.9->20
        "y": [0.0] * 7,
        "z": [0.0] * 7,
    })
    terms = pd.DataFrame({
        "study_id": ["s1", "s1", "s2", "s3"],
        "contrast_id": ["1"] * 4,
        "term": ["fear", "pain", "fear", "pain"],
        "weight": [0.5, 0.01, 0.002, 0.3],
    })
    return AnalysisIndex.from_bytes(load_db.build_index(meta, coords, terms))


def counts(index, arr, labels):
    return {lab: int(n) for lab, n in zip(labels, arr) if n}


def test_term_counts_respect_threshold(index):
    vocab = index.vocab.tolist()
    assert counts(index, index.term_counts({"s1", "s2", "s3"}, 0.001), vocab) == {"fear": 2, "pain": 2}
    assert counts(index, index.term_counts({"s1", "s2", "s3"}, 0.005), vocab) == {"fear": 1, "pain": 2}
    assert counts(index, index.term_counts({"s1", "s2"}, 0.1), vocab) == {"fear": 1}
    assert index.term_counts(set(), 0.001).sum() == 0


def test_cells_round_halves_up_and_count_studies_once(index):
    cells = [tuple(c) for c in index.cells.tolist()]
    got = counts(index, index.cell_counts({"s1", "s2", "s3"}), cells)
    # s1 -> 10, 0, 20, 20 (its two foci in cell 20 count once); s2 -> 10 twice (once); s3 -> 0
    assert got == {(10, 0, 0): 2, (0, 0, 0): 2, (20, 0, 0): 1}


def test_unknown_study_is_reported_as_stale(index):
    with pytest.raises(StaleIndex):
        index.term_counts({"s1", "nope"}, 0.001)
