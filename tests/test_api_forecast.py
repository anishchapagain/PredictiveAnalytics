"""Tests for POST /api/v1/forecast/run and POST /api/v1/forecast/baseline-suggestion.

Deliberately does NOT compare yhat_lower/yhat_upper across two separate API calls with
different settings -- Prophet's own confidence-interval sampling isn't seeded, so those
values vary slightly call to call even with identical inputs (found the hard way while
writing the user manual's worked examples; see notes.txt 2026-08-19). Where a formula
needs checking (safety_stock = yhat_upper * (1 + safety_buffer_pct)), it's checked
*within* one response instead, which is fully deterministic regardless of Prophet's
sampling.
"""

import datetime

import pytest
from fastapi.testclient import TestClient

WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _run(client: TestClient, **overrides):
    body = {"horizon_days": 7, "include_charts": False, **overrides}
    return client.post("/api/v1/forecast/run", json=body)


def test_run_receiver_country_success_shape(client: TestClient):
    res = _run(client, dimension="receiver_country", receiver_country="NEPAL")
    assert res.status_code == 200
    body = res.json()

    assert body["meta"]["dimension"] == "receiver_country"
    assert body["meta"]["history_days"] == 120
    for key in ("horizon_days", "total_shortfall_usd", "avg_daily_requirement_usd",
                "safety_stock_ratio", "peak_demand_date", "peak_demand_shortfall_usd",
                "days_with_shortfall", "baseline_funding_level_usd"):
        assert key in body["summary"]

    forecast = body["forecast"]
    assert len(forecast) == 7
    dates = [datetime.date.fromisoformat(row["ds"]) for row in forecast]
    assert dates == sorted(dates)  # strictly chronological
    assert len(set(dates)) == 7  # no duplicate/skipped days

    for row in forecast:
        expected_weekday = WEEKDAY_ABBR[datetime.date.fromisoformat(row["ds"]).weekday()]
        assert row["weekday"] == expected_weekday, (
            f"weekday mismatch for {row['ds']}: got {row['weekday']!r}, "
            f"real calendar day is {expected_weekday!r}"
        )
        assert row["daily_shortfall"] >= 0
        assert row["yhat_lower"] <= row["yhat"] <= row["yhat_upper"]

    cumulative = [row["cumulative_shortfall"] for row in forecast]
    assert cumulative == sorted(cumulative)  # non-decreasing running total


def test_run_corridor_is_narrower_than_receiver_country(client: TestClient):
    """UAE->NEPAL alone (the fixture's thinner corridor, ~300/day base) should forecast
    noticeably lower volume than NEPAL as a whole (both corridors combined, ~1300+/day)
    -- confirms Dimension actually changes which rows get aggregated, not just a label.
    """
    combined = _run(client, dimension="receiver_country", receiver_country="NEPAL").json()
    corridor = _run(
        client, dimension="corridor",
        sending_country="UNITED ARAB EMIRATES", receiver_country="NEPAL",
    ).json()

    assert (corridor["summary"]["avg_daily_requirement_usd"]
            < combined["summary"]["avg_daily_requirement_usd"])


@pytest.mark.parametrize(
    ("body", "expected_sending", "expected_receiving"),
    [
        ({"dimension": "receiver_country", "receiver_country": "NEPAL"}, None, "NP"),
        (
            {"dimension": "corridor", "sending_country": "UNITED ARAB EMIRATES",
             "receiver_country": "NEPAL"},
            "AE", "NP",
        ),
        ({"dimension": "agent", "agent_name": "TEST AGENT ONE"}, None, None),
    ],
)
def test_run_holiday_resolution_matches_dimension(
    client: TestClient, body, expected_sending, expected_receiving
):
    """The one place Dimension changes more than "which rows get combined": which
    country's public holidays feed into the model at all, since sending_country/
    receiver_country are read directly off the request, not the dimension-filtered
    values. Verified by hand against the live API while writing the user manual's
    Section 8 -- this locks that finding in as a regression test.
    """
    res = _run(client, **body)
    assert res.status_code == 200
    meta = res.json()["meta"]
    assert meta["sending_country_code_resolved"] == expected_sending
    assert meta["receiver_country_code_resolved"] == expected_receiving


def test_run_safety_stock_formula_holds(client: TestClient):
    res = _run(
        client, dimension="receiver_country", receiver_country="NEPAL",
        safety_buffer_pct=0.20,
    )
    assert res.status_code == 200
    for row in res.json()["forecast"]:
        assert row["safety_stock"] == pytest.approx(row["yhat_upper"] * 1.20, rel=1e-6)


def test_run_missing_required_filter_is_422(client: TestClient):
    # corridor requires both sending_country and receiver_country (DimensionFilters'
    # own model_validator) -- this is a Pydantic-level 422, before the engine ever runs.
    res = _run(client, dimension="corridor", receiver_country="NEPAL")
    assert res.status_code == 422


def test_run_unmatched_filters_is_422(client: TestClient):
    # A syntactically valid request that matches zero rows -- the engine's own
    # ValueError, mapped to 422 by _run_or_raise (a caller-input problem, not a server
    # error).
    res = _run(client, dimension="receiver_country", receiver_country="NOWHERE AT ALL")
    assert res.status_code == 422
    assert "No transactions match" in res.json()["detail"]


@pytest.mark.parametrize("horizon_days", [0, 366])
def test_run_horizon_days_out_of_range_is_422(client: TestClient, horizon_days):
    res = _run(
        client, dimension="receiver_country", receiver_country="NEPAL",
        horizon_days=horizon_days,
    )
    assert res.status_code == 422


# --- baseline-suggestion ------------------------------------------------------------

def _suggest(client: TestClient, **overrides):
    body = {"dimension": "receiver_country", "receiver_country": "NEPAL", **overrides}
    return client.post("/api/v1/forecast/baseline-suggestion", json=body)


def test_suggest_baseline_average_matches_known_value(client: TestClient):
    res = _suggest(client, method="average", window_days=30)
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "method": "average",
        "window_days_requested": 30,
        "window_days_used": 30,
        "value": pytest.approx(1385.7553333333335),
        "window_start": "2024-03-31",
        "window_end": "2024-04-29",
    }


def test_suggest_baseline_window_clamps_to_available_history(client: TestClient):
    # 365 is the field's own max (BaselineSuggestionRequest.window_days, le=365) --
    # still well beyond the fixture's 120 days of history, which is the point: the
    # clamp is on *available data*, not a magic number, so this is exactly as valid a
    # way to trigger it as a hypothetical larger value would be.
    res = _suggest(client, method="average", window_days=365)
    assert res.status_code == 200
    body = res.json()
    assert body["window_days_requested"] == 365
    assert body["window_days_used"] == 120  # the fixture's full history, not 365
    assert body["window_start"] == "2024-01-01"
    assert body["value"] == pytest.approx(1307.1129166666665)


@pytest.mark.parametrize("method", ["average", "weighted_average", "median"])
def test_suggest_baseline_every_method_succeeds(client: TestClient, method):
    res = _suggest(client, method=method, window_days=14)
    assert res.status_code == 200
    assert res.json()["value"] > 0


def test_suggest_baseline_unknown_method_is_422(client: TestClient):
    res = client.post(
        "/api/v1/forecast/baseline-suggestion",
        json={"dimension": "receiver_country", "receiver_country": "NEPAL",
              "method": "not_a_real_method"},
    )
    assert res.status_code == 422


# --- evaluate ------------------------------------------------------------------------

def _evaluate(client: TestClient, **overrides):
    body = {"dimension": "receiver_country", "receiver_country": "NEPAL", "eval_days": 14,
            **overrides}
    return client.post("/api/v1/forecast/evaluate", json=body)


def test_evaluate_success_shape(client: TestClient):
    res = _evaluate(client, safety_buffer_pct=0.15)
    assert res.status_code == 200
    body = res.json()

    assert body["meta"]["dimension"] == "receiver_country"
    assert body["eval_days"] == 14
    assert body["train_days"] == 120 - 14
    assert len(body["days"]) == 14

    dates = [datetime.date.fromisoformat(d["ds"]) for d in body["days"]]
    assert dates == sorted(dates)
    assert len(set(dates)) == 14

    for key in ("mape_pct", "rmse", "interval_coverage_pct", "safety_stock_coverage_pct",
                "naive_baseline", "prophet_beats_naive_on_rmse", "prophet_beats_naive_on_mape"):
        assert key in body

    for day in body["days"]:
        # Both sides independently rounded to 2dp in the response -- see engine.py's
        # note above evaluate_accuracy's days.append() -- so allow a cent of slack.
        assert day["safety_stock"] == pytest.approx(day["yhat_upper"] * 1.15, abs=0.02)


def test_evaluate_holiday_resolution_matches_dimension(client: TestClient):
    # Same by-Dimension holiday resolution as /run -- exercised here too since /evaluate
    # builds its own ForecastConfig/HolidayConfig independently.
    res = _evaluate(
        client, dimension="corridor", sending_country="UNITED ARAB EMIRATES",
        receiver_country="NEPAL",
    )
    assert res.status_code == 200
    meta = res.json()["meta"]
    assert meta["sending_country_code_resolved"] == "AE"
    assert meta["receiver_country_code_resolved"] == "NP"


def test_evaluate_not_enough_history_is_422(client: TestClient):
    # Fixture has 120 days; eval_days=180 (max the field allows for a receiver_country
    # spanning both corridors) needs 180+14=194 -- more than the fixture has.
    res = _evaluate(client, eval_days=180)
    assert res.status_code == 422
    assert "Not enough history to evaluate" in res.json()["detail"]


def test_evaluate_missing_required_filter_is_422(client: TestClient):
    res = _evaluate(client, dimension="corridor", receiver_country="NEPAL")
    assert res.status_code == 422


@pytest.mark.parametrize("eval_days", [6, 181])
def test_evaluate_eval_days_out_of_range_is_422(client: TestClient, eval_days):
    res = _evaluate(client, eval_days=eval_days)
    assert res.status_code == 422


# --- LightGBM comparison column (opt-in) ----------------------------------------------
# The invariant these protect: enabling LightGBM must add a column and change NOTHING
# else. Every liquidity number stays Prophet-derived, so a caller can turn the
# comparison on without silently changing the funding plan they are about to act on.

def test_lightgbm_absent_by_default(client: TestClient):
    res = _run(client, dimension="receiver_country", receiver_country="NEPAL")
    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["lightgbm_included"] is False
    assert all(row.get("yhat_lightgbm") is None for row in body["forecast"])


def test_lightgbm_adds_a_column_when_requested(client: TestClient):
    res = _run(
        client, dimension="receiver_country", receiver_country="NEPAL",
        include_lightgbm=True,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["lightgbm_included"] is True
    for row in body["forecast"]:
        assert row["yhat_lightgbm"] is not None
        assert row["yhat_lightgbm"] >= 0  # volume can't be negative


def test_lightgbm_does_not_change_any_funding_number(client: TestClient):
    """The whole point of the opt-in split: safety_stock, shortfall and the summary are
    Prophet-derived whether or not the comparison ran.

    Compares yhat rather than the interval-derived columns, because Prophet's interval
    sampling isn't seeded and yhat_upper varies slightly call to call (see this module's
    docstring). yhat itself is deterministic, so any drift here would be a real leak of
    LightGBM into the Prophet path.
    """
    without = _run(client, dimension="receiver_country", receiver_country="NEPAL").json()
    with_lgbm = _run(
        client, dimension="receiver_country", receiver_country="NEPAL",
        include_lightgbm=True,
    ).json()

    assert with_lgbm["summary"]["avg_daily_requirement_usd"] == pytest.approx(
        without["summary"]["avg_daily_requirement_usd"]
    )
    for a, b in zip(without["forecast"], with_lgbm["forecast"], strict=True):
        assert a["ds"] == b["ds"]
        assert b["yhat"] == pytest.approx(a["yhat"])


def test_lightgbm_config_is_accepted_and_validated(client: TestClient):
    ok = _run(
        client, dimension="receiver_country", receiver_country="NEPAL",
        include_lightgbm=True,
        lightgbm={"num_boost_round": 50, "learning_rate": 0.1, "num_leaves": 8,
                  "max_depth": 3, "min_data_in_leaf": 5},
    )
    assert ok.status_code == 200
    assert ok.json()["meta"]["lightgbm_included"] is True

    # out-of-range values are rejected by Pydantic before the engine ever runs
    for bad in ({"num_boost_round": 0}, {"learning_rate": 0}, {"num_leaves": 1},
                {"feature_fraction": 0}, {"lambda_l2": -1}):
        res = _run(
            client, dimension="receiver_country", receiver_country="NEPAL",
            include_lightgbm=True, lightgbm=bad,
        )
        assert res.status_code == 422, f"expected 422 for lightgbm={bad}"


def test_lightgbm_is_reproducible_for_the_same_seed(client: TestClient):
    body = dict(dimension="receiver_country", receiver_country="NEPAL",
                include_lightgbm=True, lightgbm={"random_state": 7})
    first = _run(client, **body).json()["forecast"]
    second = _run(client, **body).json()["forecast"]
    for a, b in zip(first, second, strict=True):
        assert a["yhat_lightgbm"] == pytest.approx(b["yhat_lightgbm"])


def test_evaluate_ignores_lightgbm_and_stays_prophet_only(client: TestClient):
    """/evaluate backtests the calibrated band, which is Prophet's. An unknown field
    must not break it, and it must not grow a LightGBM column."""
    res = _evaluate(client, dimension="receiver_country", receiver_country="NEPAL",
                    include_lightgbm=True)
    assert res.status_code == 200
    assert "yhat_lightgbm" not in res.json()["days"][0]


def test_empty_include_statuses_is_422_not_a_silent_all_statuses_forecast(client: TestClient):
    """Regression for a pre-deployment QA finding: [] returned 200 and quietly counted
    cancelled/failed/blocked/OFAC transactions as funded volume. Reachable from the
    dashboard by unchecking every status box, so it had a real user path."""
    res = _run(client, dimension="receiver_country", receiver_country="NEPAL",
               include_statuses=[])
    assert res.status_code == 422
    # and the normal single-status case still works
    ok = _run(client, dimension="receiver_country", receiver_country="NEPAL",
              include_statuses=["Payment"])
    assert ok.status_code == 200
