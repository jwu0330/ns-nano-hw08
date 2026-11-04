import math

import pytest

from nsapi.parsing import InputError, fmt_coords, normalize_term, parse_coords
from nsapi.queries import _log_odds


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
    lor, z = _log_odds(30, 100, 10, 100)
    lor2, z2 = _log_odds(10, 100, 30, 100)
    assert lor > 0 and z > 0
    assert math.isclose(lor, -lor2) and math.isclose(z, -z2)
    # hand-computed: ln(30.5/70.5) - ln(10.5/90.5)
    assert math.isclose(lor, math.log(30.5 / 70.5) - math.log(10.5 / 90.5))
    assert _log_odds(10, 100, 10, 100) == (0.0, 0.0)
