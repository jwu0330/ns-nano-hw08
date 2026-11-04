"""API results must equal the independent pandas reference, study by study."""

import pytest

from tests.conftest import needs_db

pytestmark = needs_db

TERM_CASES = [
    ("posterior cingulate", "ventromedial prefrontal", 0.001),  # template DMN example
    ("ventromedial prefrontal", "posterior cingulate", 0.001),
    ("amygdala", "fear", 0.001),
    ("working memory", "memory", 0.001),
    ("reward", "pain", 0.05),  # non-default threshold
    ("default mode", "attention", 0.1),
]
LOCATION_CASES = [
    ((0, -52, 26), (-2, 50, -6), 6.0),  # template DMN example
    ((-2, 50, -6), (0, -52, 26), 6.0),
    ((-22, 0, -20), (22, 0, -20), 6.0),  # left vs right amygdala
    ((0, -52, 26), (-2, 50, -6), 10.0),
    ((0.5, -51.5, 25.25), (-2, 50, -6), 4.0),  # fractional input, small radius
    ((40, 22, 0), (-40, 22, 0), 2.0),  # 2 mm: exercises the inclusive boundary on the 2 mm grid
]


def ids(direction):
    return {s["study_id"] for s in direction["studies"]}


@pytest.mark.parametrize("a,b,thr", TERM_CASES)
def test_terms_match_reference(client, ref, a, b, thr):
    r = client.get(f"/dissociate/terms/{a}/{b}", params={"threshold": thr, "both": True})
    assert r.status_code == 200
    j = r.json()
    exp_ab, exp_ba = ref.dissociate_terms(a, b, thr)
    assert ids(j["a_not_b"]) == exp_ab and j["a_not_b"]["count"] == len(exp_ab)
    assert ids(j["b_not_a"]) == exp_ba and j["b_not_a"]["count"] == len(exp_ba)
    assert j["counts"]["a"] == len(ref.studies_with_term(a, thr))
    assert j["counts"]["b"] == len(ref.studies_with_term(b, thr))


@pytest.mark.parametrize("a,b,radius", LOCATION_CASES)
def test_locations_match_reference(client, ref, a, b, radius):
    path = "/dissociate/locations/{}/{}".format("_".join(map(str, a)), "_".join(map(str, b)))
    r = client.get(path, params={"radius": radius, "both": True})
    assert r.status_code == 200
    j = r.json()
    exp_ab, exp_ba = ref.dissociate_locations(a, b, radius)
    assert ids(j["a_not_b"]) == exp_ab
    assert ids(j["b_not_a"]) == exp_ba
    assert j["counts"]["a"] == len(ref.studies_near(a, radius))


def test_single_set_endpoints_match_reference(client, ref):
    j = client.get("/terms/amygdala/studies").json()
    assert ids(j) == ref.studies_with_term("amygdala")
    j = client.get("/locations/-22_0_-20/studies", params={"radius": 8}).json()
    assert ids(j) == ref.studies_near((-22, 0, -20), 8)
