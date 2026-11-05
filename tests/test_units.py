"""Tests that need no database: URL parsing, statistics, and the analysis index
built by load_db.py on hand-made data."""

import math

import pandas as pd
import pytest

import analysis
import load_db
from analysis import AnalysisIndex, StaleIndex
from app import InputError, fmt_coords, normalize_term, parse_coords


@pytest.mark.parametrize("text,xyz", [
    ("0_-52_26", (0, -52, 26)),
    ("-2_50_-6", (-2, 50, -6)),
    ("0.5_-52.25_+26", (0.5, -52.25, 26)),
    ("0,-52,26", (0, -52, 26)),
    (" 0 _ -52 _ 26 ", (0, -52, 26)),
])
def test_parse_coords_ok(text, xyz):
    assert parse_coords(text) == xyz


@pytest.mark.parametrize("text", ["0_-52", "0_-52_26_1", "a_b_c", "", "0__-52_26", "1e3_0_0", "0_-52_200"])
def test_parse_coords_bad(text):
    with pytest.raises(InputError):
        parse_coords(text)


@pytest.mark.parametrize("raw,term", [
    ("posterior cingulate", "posterior cingulate"),
    ("posterior_cingulate", "posterior cingulate"),
    ("Posterior+Cingulate", "posterior cingulate"),
    ("  ventromedial__prefrontal ", "ventromedial prefrontal"),
])
def test_normalize_term(raw, term):
    assert normalize_term(raw) == term


def test_normalize_term_empty():
    with pytest.raises(InputError):
        normalize_term(" _ ")


def test_fmt_coords():
    assert fmt_coords((0.0, -52.0, 26.5)) == [0, -52, 26.5]


def test_log_odds_symmetric_and_signed():
    lor, z = analysis.log_odds(30, 100, 10, 100)
    lor2, z2 = analysis.log_odds(10, 100, 30, 100)
    assert lor > 0 and z > 0
    assert math.isclose(lor, -lor2) and math.isclose(z, -z2)
    # hand-computed: ln(30.5/70.5) - ln(10.5/90.5)
    assert math.isclose(lor, math.log(30.5 / 70.5) - math.log(10.5 / 90.5))
    assert analysis.log_odds(10, 100, 10, 100) == (0.0, 0.0)


# ---------------------------------------------------------------- analysis index
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


def test_stale_index_resets_and_reloads(monkeypatch):
    loads = []
    monkeypatch.setattr(analysis._Loader, "index", property(lambda self: loads.append(1) or "idx"))
    analysis.reset()  # no cached index: must not fail
    assert analysis._loader.get() == "idx" and loads == [1]
