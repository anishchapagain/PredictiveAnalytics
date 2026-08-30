"""Unit tests for TreasuryEngine's pure functions, with no HTTP layer and (mostly) no
Prophet involved -- these run in milliseconds and pin down the exact arithmetic in a
way the API-level tests deliberately avoid (Prophet's own confidence-interval sampling
isn't seeded, so cross-call comparisons of yhat_lower/yhat_upper are inherently a
little flaky; see test_api_forecast.py's module docstring). simulate_liquidity() in
particular never touches Prophet at all -- it's plain arithmetic on a DataFrame you
hand it, so it's tested here with fully controlled, hand-picked inputs instead.
"""

import math

import pandas as pd
import pytest

from app.core.exceptions import DataSourceError, ForecastingError
from app.forecasting.config import ForecastConfig, LiquidityConfig
from app.forecasting.engine import TreasuryEngine


def _raw_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["TRN_Date"] = pd.to_datetime(df["TRN_Date"])
    return df


# --- prepare_data --------------------------------------------------------------------

def test_prepare_data_fills_calendar_gaps_with_zero_volume():
    engine = TreasuryEngine()
    raw = _raw_df([
        {"TRN_Date": "2024-01-01", "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 100.0, "transstatus": "Payment"},
        # 2024-01-02 has no rows at all -- must appear as an explicit zero, not vanish.
        {"TRN_Date": "2024-01-03", "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 300.0, "transstatus": "Payment"},
    ])
    prepared = engine.prepare_data(raw, receiver_country="NEPAL")
    assert prepared["ds"].tolist() == list(pd.to_datetime(
        ["2024-01-01", "2024-01-02", "2024-01-03"]
    ))
    assert prepared["y"].tolist() == [100.0, 0.0, 300.0]


def test_prepare_data_only_counts_included_statuses():
    engine = TreasuryEngine(ForecastConfig(include_statuses=("Payment",)))
    raw = _raw_df([
        {"TRN_Date": "2024-01-01", "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 100.0, "transstatus": "Payment"},
        {"TRN_Date": "2024-01-01", "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 999.0, "transstatus": "Cancel"},
    ])
    prepared = engine.prepare_data(raw, receiver_country="NEPAL")
    assert prepared["y"].tolist() == [100.0]  # the Cancel row excluded entirely


def test_prepare_data_raises_valueerror_when_filters_match_nothing():
    engine = TreasuryEngine()
    raw = _raw_df([
        {"TRN_Date": "2024-01-01", "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 100.0, "transstatus": "Payment"},
    ])
    with pytest.raises(ValueError, match="No transactions match"):
        engine.prepare_data(raw, receiver_country="SOMEWHERE ELSE ENTIRELY")


def test_prepare_data_raises_datasourceerror_on_missing_required_column():
    engine = TreasuryEngine()
    raw = _raw_df([
        {"TRN_Date": "2024-01-01", "Sending_Country": "AUSTRALIA",
         "Agent_Name": "A", "Transaction_Amount_USD": 100.0, "transstatus": "Payment"},
        # Receiver_Country is entirely missing -- a data problem, not a filter problem.
    ])
    with pytest.raises(DataSourceError):
        engine.prepare_data(raw)


# --- train_forecast: seasonality_mode -------------------------------------------------
# Regression coverage for a real defect found via technical.md's worked example
# (Receiver Country: Australia, cross-checked against the real Business_Report.csv):
# under Prophet's own "additive" default, a large negative weekly effect and a large
# negative yearly effect can independently land on the same calendar days and stack,
# summing below zero even on a corridor whose weekends are real and consistently
# nonzero (26% of weekday volume, 96.6% of weekends nonzero across 2.5 years of
# history) -- train_forecast()'s clip(lower=0) then silently floors that to yhat=$0.00,
# indistinguishable from a genuine (and wrong) "no volume expected" prediction.
# "multiplicative" (this engine's default as of the fix) expresses seasonal/holiday
# effects as fractions of trend instead of flat dollar offsets, which cannot
# independently drag the sum past -100% of trend the same way.
#
# Reproducing the exact compounding failure needs the real data's specific noise
# structure -- three synthetic attempts at a small deterministic fixture (plain
# weekly-only, noisy weekly+yearly, and a close copy of the real corridor's own
# volume ratio/span) all failed to trigger it, consistent with this file's own
# documented caveat that Prophet's output isn't perfectly reproducible from a small
# synthetic dataset. So this tests the one thing that *is* deterministic and matters
# for the fix: that seasonality_mode actually reaches the Prophet model Prophet fits,
# in both directions, rather than being a config field nothing reads.

def test_forecast_config_defaults_to_multiplicative_seasonality():
    assert ForecastConfig().seasonality_mode == "multiplicative"


def test_train_forecast_passes_seasonality_mode_through_to_prophet():
    engine_default = TreasuryEngine(ForecastConfig(horizon_days=2))
    engine_additive = TreasuryEngine(ForecastConfig(horizon_days=2, seasonality_mode="additive"))
    raw = _raw_df([
        {"TRN_Date": d, "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 100.0, "transstatus": "Payment"}
        for d in pd.date_range("2024-01-01", periods=30, freq="D")
    ])
    prepared = engine_default.prepare_data(raw, receiver_country="NEPAL")

    model_default, _ = engine_default.train_forecast(prepared)
    model_additive, _ = engine_additive.train_forecast(prepared)

    assert model_default.seasonality_mode == "multiplicative"  # this engine's fixed default
    assert model_additive.seasonality_mode == "additive"  # still selectable, e.g. for comparison


# --- simulate_liquidity ---------------------------------------------------------------

def test_simulate_liquidity_formula_and_shortfall():
    engine = TreasuryEngine(
        ForecastConfig(horizon_days=3),
        LiquidityConfig(baseline_funding_level=1000.0, safety_buffer_pct=0.10),
    )
    forecast = pd.DataFrame({
        "ds": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
        "yhat": [800.0, 1200.0, 0.0],
        "yhat_lower": [700.0, 1100.0, 0.0],
        "yhat_upper": [900.0, 1300.0, 100.0],
    })
    horizon = engine.simulate_liquidity(forecast)

    # safety_stock = yhat_upper * (1 + safety_buffer_pct) = yhat_upper * 1.10
    assert horizon["safety_stock"].tolist() == pytest.approx([990.0, 1430.0, 110.0])
    # daily_shortfall = max(0, safety_stock - baseline=1000)
    assert horizon["daily_shortfall"].tolist() == pytest.approx([0.0, 430.0, 0.0])
    assert horizon["cumulative_shortfall"].tolist() == pytest.approx([0.0, 430.0, 430.0])

    ratio = horizon["liquidity_to_volume_ratio"].tolist()
    assert ratio[0] == pytest.approx(1000.0 / 800.0)
    assert ratio[1] == pytest.approx(1000.0 / 1200.0)
    assert math.isnan(ratio[2])  # yhat == 0 -> ratio is undefined, not a divide-by-zero crash


def test_simulate_liquidity_zero_buffer_leaves_safety_stock_equal_to_upper():
    engine = TreasuryEngine(ForecastConfig(horizon_days=1), LiquidityConfig())
    forecast = pd.DataFrame({
        "ds": pd.to_datetime(["2024-01-01"]),
        "yhat": [500.0],
        "yhat_lower": [400.0],
        "yhat_upper": [600.0],
    })
    horizon = engine.simulate_liquidity(forecast)
    assert horizon["safety_stock"].iloc[0] == pytest.approx(600.0)


# --- summarize --------------------------------------------------------------------

def test_summarize_raises_forecastingerror_on_empty_horizon():
    engine = TreasuryEngine()
    empty = pd.DataFrame(columns=["ds", "yhat", "daily_shortfall", "safety_stock"])
    with pytest.raises(ForecastingError):
        engine.summarize(empty)


# --- suggest_baseline --------------------------------------------------------------

def test_suggest_baseline_median_resists_a_spike_that_pulls_the_average_up():
    engine = TreasuryEngine()
    raw = _raw_df([
        {"TRN_Date": d, "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": v, "transstatus": "Payment"}
        for d, v in zip(
            ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
            [100.0, 100.0, 100.0, 100.0, 1000.0],  # one big spike on the last day
            strict=True,
        )
    ])
    avg = engine.suggest_baseline(raw, method="average", window_days=5, receiver_country="NEPAL")
    median = engine.suggest_baseline(raw, method="median", window_days=5, receiver_country="NEPAL")
    assert avg["value"] == pytest.approx((100.0 * 4 + 1000.0) / 5)  # 280.0
    assert median["value"] == pytest.approx(100.0)
    assert median["value"] < avg["value"]


def test_suggest_baseline_unknown_method_raises():
    engine = TreasuryEngine()
    raw = _raw_df([
        {"TRN_Date": "2024-01-01", "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 100.0, "transstatus": "Payment"},
    ])
    with pytest.raises(ValueError, match="Unknown baseline suggestion method"):
        engine.suggest_baseline(raw, method="bogus", window_days=1, receiver_country="NEPAL")


# --- evaluate_accuracy ---------------------------------------------------------------
# Does involve Prophet (it fits a real model on the held-out training slice), so --
# same discipline as test_api_forecast.py -- these check shape/invariants/formulas
# rather than pinning exact yhat-derived numbers, which aren't deterministic call to
# call (see that file's module docstring).

def _daily_raw_df(n_days: int, *, base: float = 200.0, spike_every: int | None = None):
    dates = pd.date_range("2024-01-01", periods=n_days, freq="D")
    rows = []
    for i, d in enumerate(dates):
        amount = base + (i % 7) * 5.0  # a mild weekly wave, nothing exotic
        if spike_every and i % spike_every == 0:
            amount *= 3
        rows.append({
            "TRN_Date": d, "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
            "Agent_Name": "A", "Transaction_Amount_USD": amount, "transstatus": "Payment",
        })
    return _raw_df(rows)


def test_evaluate_accuracy_not_enough_history_raises_valueerror():
    engine = TreasuryEngine()
    raw = _daily_raw_df(20)  # 20 days total; eval_days=14 would need 14+14=28
    with pytest.raises(ValueError, match="Not enough history to evaluate"):
        engine.evaluate_accuracy(raw, eval_days=14, receiver_country="NEPAL")


def test_evaluate_accuracy_shape_and_invariants():
    engine = TreasuryEngine(liquidity_config=LiquidityConfig(safety_buffer_pct=0.10))
    raw = _daily_raw_df(60)
    result = engine.evaluate_accuracy(raw, eval_days=10, receiver_country="NEPAL")

    assert result["eval_days"] == 10
    assert result["train_days"] == 50
    assert len(result["days"]) == 10

    dates = [pd.Timestamp(d["ds"]) for d in result["days"]]
    assert dates == sorted(dates)  # chronological
    assert len(set(dates)) == 10  # no gaps/duplicates

    for day in result["days"]:
        assert day["yhat_lower"] <= day["yhat"] <= day["yhat_upper"]
        # safety_stock = yhat_upper * 1.10, same formula simulate_liquidity() uses --
        # each side is independently rounded to 2dp in the response (see engine.py's
        # note on rounding before comparing), so allow a cent of rounding slack rather
        # than an exact match.
        assert day["safety_stock"] == pytest.approx(day["yhat_upper"] * 1.10, abs=0.02)
        assert day["within_interval"] == (day["yhat_lower"] <= day["actual"] <= day["yhat_upper"])
        assert day["within_safety_stock"] == (day["actual"] <= day["safety_stock"])

    assert 0.0 <= result["interval_coverage_pct"] <= 100.0
    assert 0.0 <= result["safety_stock_coverage_pct"] <= 100.0
    # safety_stock is always >= yhat_upper (buffer is non-negative), so a real funding
    # plan under this config can never cover *less* than raw interval coverage would.
    assert result["safety_stock_coverage_pct"] >= result["interval_coverage_pct"]
    assert result["rmse"] >= 0
    assert result["zero_actual_days_excluded_from_mape"] == 0  # no zero-volume days here
    assert result["mape_pct"] is not None and result["mape_pct"] >= 0


def test_evaluate_accuracy_naive_baseline_is_flat_across_the_window():
    engine = TreasuryEngine()
    raw = _daily_raw_df(60)
    result = engine.evaluate_accuracy(raw, eval_days=10, receiver_country="NEPAL")

    naive_values = {d["naive_prediction"] for d in result["days"]}
    assert naive_values == {result["naive_baseline"]["value"]}  # same flat value every day
    assert result["naive_baseline"]["method"] == "average"
    assert result["naive_baseline"]["rmse"] >= 0


def test_evaluate_accuracy_excludes_zero_actual_days_from_mape_but_not_rmse():
    engine = TreasuryEngine()
    dates = pd.date_range("2024-01-01", periods=40, freq="D")
    rows = [
        {"TRN_Date": d, "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 200.0, "transstatus": "Payment"}
        for d in dates[:-10]
    ]
    # Last 10 (the eval window) alternate real volume / true zero-volume days, so the
    # reindexed daily series has genuine y=0 rows inside the held-out window. The very
    # last day is deliberately kept real (i % 2 == 1, and i=9 is odd) -- prepare_data's
    # reindex() spans only [min(present dates), max(present dates)], so a missing
    # transaction on the *last* day would shrink the window by one day instead of
    # appearing as a zero-volume day inside it.
    for i, d in enumerate(dates[-10:]):
        if i % 2 == 1:
            rows.append({
                "TRN_Date": d, "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
                "Agent_Name": "A", "Transaction_Amount_USD": 200.0, "transstatus": "Payment",
            })
        # even days (i=0,2,4,6,8): no row at all -> reindex() fills y=0.0 for that date
    raw = _raw_df(rows)

    result = engine.evaluate_accuracy(raw, eval_days=10, receiver_country="NEPAL")
    assert result["zero_actual_days_excluded_from_mape"] == 5
    assert result["mape_pct"] is not None  # still computable over the other 5 days
    # rmse always covers every day, zero-actual or not
    assert result["rmse"] >= 0


# --- include_statuses: empty must fail loudly, not mean "everything" ------------------

def test_prepare_data_rejects_empty_include_statuses():
    """An empty selection used to fall straight through the status filter, silently
    counting every transstatus as funded volume -- the opposite of what clearing the list
    means. Found in pre-deployment QA: on NEPAL it pulled in $2.46M of cancelled/blocked/
    OFAC-held transactions and inflated the daily requirement by 3.5%, all behind a 200.
    """
    engine = TreasuryEngine(ForecastConfig(include_statuses=()))
    raw = _raw_df([
        {"TRN_Date": "2024-01-01", "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 100.0, "transstatus": "Payment"},
        {"TRN_Date": "2024-01-01", "Sending_Country": "AUSTRALIA", "Receiver_Country": "NEPAL",
         "Agent_Name": "A", "Transaction_Amount_USD": 999.0, "transstatus": "Cancel"},
    ])
    with pytest.raises(ValueError, match="at least one transstatus"):
        engine.prepare_data(raw, receiver_country="NEPAL")
