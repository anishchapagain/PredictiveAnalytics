"""Tests for the /api/v1/meta/* endpoints, against the known synthetic fixture (see
conftest.py) -- every expected value here was computed directly from the fixture and
double-checked before being hardcoded, not guessed.
"""

from fastapi.testclient import TestClient


def test_sending_countries(client: TestClient):
    res = client.get("/api/v1/meta/sending-countries")
    assert res.status_code == 200
    assert res.json() == ["AUSTRALIA", "UNITED ARAB EMIRATES"]  # alphabetically sorted


def test_receiver_countries(client: TestClient):
    res = client.get("/api/v1/meta/receiver-countries")
    assert res.status_code == 200
    assert res.json() == ["NEPAL"]


def test_agents(client: TestClient):
    res = client.get("/api/v1/meta/agents")
    assert res.status_code == 200
    assert res.json() == ["TEST AGENT ONE", "TEST AGENT TWO"]


def test_corridors_lists_only_pairs_actually_observed(client: TestClient):
    res = client.get("/api/v1/meta/corridors")
    assert res.status_code == 200
    pairs = res.json()
    assert len(pairs) == 2  # not the full 2x1 cross-product-plus-noise -- exactly what's real
    assert {"sending_country": "AUSTRALIA", "receiver_country": "NEPAL"} in pairs
    assert {"sending_country": "UNITED ARAB EMIRATES", "receiver_country": "NEPAL"} in pairs


def test_statuses(client: TestClient):
    res = client.get("/api/v1/meta/statuses")
    assert res.status_code == 200
    assert res.json() == ["Cancel", "Payment"]


def test_dataset_summary_matches_the_fixture_exactly(client: TestClient):
    res = client.get("/api/v1/meta/summary")
    assert res.status_code == 200
    body = res.json()
    assert body["total_rows"] == 240
    assert body["date_range"] == {"start": "2024-01-01", "end": "2024-04-29"}
    assert body["distinct_counts"] == {
        "sending_countries": 2,
        "receiver_countries": 1,
        "agents": 2,
        "transaction_methods": 2,
        "statuses": 2,
        "sending_currencies": 2,
        "payout_currencies": 1,
    }
