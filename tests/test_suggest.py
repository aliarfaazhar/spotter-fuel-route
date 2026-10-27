from tests.conftest import make_place


def test_prefix_ranked_by_population_and_offline(client, db):
    make_place("Chicago", "IL", 41.9, -87.6, population=2_700_000)
    make_place("Chicopee", "MA", 42.1, -72.6, population=55_000)
    make_place("Chico", "CA", 39.7, -121.8, population=100_000)
    make_place("Denver", "CO", 39.7, -105.0, population=700_000)
    resp = client.get("/api/places", {"q": "chic"})
    assert resp.status_code == 200
    assert resp.json()["results"] == ["Chicago, IL", "Chico, CA", "Chicopee, MA"]


def test_normalised_names_and_state_filter(client, db):
    make_place("St. Louis", "MO", 38.6, -90.2, population=300_000, key="saintlouis")
    make_place("Saint Louis Park", "MN", 44.9, -93.3, population=50_000, key="saintlouispark")
    assert client.get("/api/places", {"q": "st lou"}).json()["results"] == ["St. Louis, MO", "Saint Louis Park, MN"]
    assert client.get("/api/places", {"q": "saint louis, mn"}).json()["results"] == ["Saint Louis Park, MN"]


def test_too_short_is_empty(client, db):
    assert client.get("/api/places", {"q": "c"}).json()["results"] == []
