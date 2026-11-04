"""Behaviour of the HTTP layer: errors, options, smoke-test endpoints."""

import pytest

from tests.conftest import needs_db

pytestmark = needs_db

DMN_TERMS = "/dissociate/terms/posterior cingulate/ventromedial prefrontal"
DMN_LOCS = "/dissociate/locations/0_-52_26/-2_50_-6"


def test_template_smoke_endpoints(client):
    assert client.get("/").status_code == 200
    r = client.get("/img")
    assert r.status_code == 200 and r.headers["content-type"] == "image/gif"
    j = client.get("/test_db").json()
    assert j["ok"] and j["coordinates_count"] == 507891 and j["metadata_count"] == 14371
    assert j["annotations_terms_count"] == 1049299


@pytest.mark.parametrize("path", [DMN_TERMS, DMN_LOCS])
def test_default_is_one_direction(client, path):
    j = client.get(path).json()
    assert "a_not_b" in j and "b_not_a" not in j
    c = j["counts"]
    assert c["a_not_b"] == c["a"] - c["a_and_b"] and c["b_not_a"] == c["b"] - c["a_and_b"]


def test_underscore_and_space_forms_agree(client):
    a = client.get(DMN_TERMS).json()
    b = client.get("/dissociate/terms/posterior_cingulate/ventromedial_prefrontal").json()
    assert a == b


def test_paging(client):
    full = client.get(DMN_TERMS).json()["a_not_b"]["studies"]
    page = client.get(DMN_TERMS, params={"limit": 10, "offset": 5}).json()["a_not_b"]
    assert page["studies"] == full[5:15] and page["count"] == len(full)
    years = [s["year"] for s in full]
    assert years == sorted(years, reverse=True)


def test_analyze_terms(client):
    j = client.get(DMN_TERMS, params={"analyze": True}).json()
    ct = j["characteristic_terms"]
    a_terms = [t["term"] for t in ct["a_not_b"]]
    b_terms = [t["term"] for t in ct["b_not_a"]]
    words = {"posterior", "cingulate", "ventromedial", "prefrontal"}
    assert not any(words & set(t.split()) for t in a_terms + b_terms)  # query terms and near-synonyms excluded
    assert all(t["z"] > 0 for t in ct["a_not_b"]) and all(t["z"] < 0 for t in ct["b_not_a"])
    assert "reward" in b_terms or "value" in b_terms  # vmPFC side is about valuation
    locs = j["characteristic_locations"]
    for side in locs.values():
        for cell in side:
            assert all(isinstance(v, int) and v % 10 == 0 for v in cell["center"])  # 10 mm grid
            assert set(cell) == {"center", "n_a", "n_b", "p_a", "p_b", "log_odds", "z"}
    assert locs["a_not_b"][0]["center"][1] < 0  # PCC side peaks posterior
    assert locs["b_not_a"][0]["center"][1] > 0  # vmPFC side peaks anterior
    assert all(c["z"] > 0 for c in locs["a_not_b"]) and all(c["z"] < 0 for c in locs["b_not_a"])


@pytest.mark.parametrize("path,status", [
    ("/dissociate/terms/amygdale/fear", 404),
    ("/dissociate/terms/fear/fear", 400),
    ("/dissociate/locations/0_-52/0_0_0", 400),
    ("/dissociate/locations/0_-52_26/0_-52_26", 400),
    ("/dissociate/locations/0_-52_26/0_0_999", 400),
    (DMN_LOCS + "?radius=0", 422),
    (DMN_LOCS + "?radius=100", 422),
    (DMN_TERMS + "?threshold=-1", 422),
    (DMN_TERMS + "?limit=0", 422),
    (DMN_TERMS + "?threshold=nan", 422),
    (DMN_TERMS + "?offset=9223372036854775808", 422),
    ("/dissociate/terms/a%00b/fear", 400),
    ("/terms/fear%3Cscript%3E/studies", 400),
    ("/dissociate/terms/" + "a" * 500 + "/fear", 400),
    ("/no/such/endpoint", 404),
])
def test_errors(client, path, status):
    r = client.get(path)
    assert r.status_code == status
    assert r.json()["error"]["message"]  # one error shape for every status


def test_suggestions(client):
    j = client.get("/dissociate/terms/amygdale/fear").json()
    assert "amygdala" in j["error"]["suggestions"]


def test_term_search(client):
    j = client.get("/terms", params={"q": "cingul", "limit": 5}).json()
    assert len(j["terms"]) == 5 and all("cingul" in t["term"] for t in j["terms"])


def test_cors_and_cache_headers(client):
    r = client.get(DMN_LOCS, headers={"Origin": "https://example.org"})
    assert r.headers["access-control-allow-origin"] == "*"
    assert "max-age" in r.headers["cache-control"]


def test_term_search_has_no_wildcards(client):
    common = client.get("/terms", params={"q": ""}).json()["terms"]
    # '%' is not a term character, so it is ignored instead of acting as a LIKE wildcard
    assert client.get("/terms", params={"q": "%"}).json()["terms"] == common
    assert client.get("/terms", params={"q": "_"}).json()["terms"] == common
    assert all("cingul" in t["term"] for t in client.get("/terms", params={"q": "cingul"}).json()["terms"])


def test_long_term_is_not_echoed(client):
    r = client.get("/dissociate/terms/" + "a" * 500 + "/fear")
    assert len(r.content) < 300


def test_default_limit_keeps_full_count(client):
    j = client.get("/terms/memory/studies").json()
    assert j["count"] == 2744 and len(j["studies"]) == 1000
    assert len(client.get("/terms/memory/studies", params={"limit": 5000}).json()["studies"]) == 2744


def test_stats(client):
    j = client.get("/stats").json()
    assert j["row_counts"] == {"metadata": 14371, "coordinates": 507891, "annotations_terms": 1049299, "terms": 3228}
    assert "source" in j and "loaded_at" in j


def test_nul_in_search_is_harmless(client):
    assert client.get("/terms", params={"q": "a\x00b"}).status_code == 200
