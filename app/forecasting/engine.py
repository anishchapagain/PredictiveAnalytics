"""The `TreasuryEngine`: corridor/agent volume forecasting + liquidity-risk simulation.

One engine instance is configured for one forecast run (`ForecastConfig` +
`LiquidityConfig`); it operates on a plain `pd.DataFrame` handed to it (from
`DataRepository.load()`, filtered by whoever calls it) rather than owning a data source
itself, so it stays trivially testable and decoupled from CSV-vs-Postgres.

Typical usage (see `app/api/routes_forecast.py` for the async/API wrapper)::

    engine = TreasuryEngine(forecast_config, liquidity_config)
    result = engine.run(df, sending_country="UNITED ARAB EMIRATES", receiver_country="NEPAL")
    result.summary            # -> dict of headline metrics
    result.horizon            # -> per-day forecast + liquidity numbers for the horizon
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from prophet import Prophet

from app.core.exceptions import DataSourceError, ForecastingError
from app.data.schema import REQUIRED_COLUMNS
from app.forecasting import charts, gbdt
from app.forecasting.config import ForecastConfig, LiquidityConfig
from app.forecasting.holidays import build_combined_holidays

logger = logging.getLogger(__name__)


def _log_column_diff(stage: str, before_cols: list[str], after_cols: list[str]) -> None:
    """Logs one feature-engineering boundary's effect on the schema: how many columns
    went in, how many came out, and exactly which were added/removed -- so the pipeline's
    actual FE steps (daily aggregation in prepare_data, Prophet's own trend/seasonality/
    holiday decomposition in train_forecast, the liquidity columns in simulate_liquidity)
    are visible from the logs alone rather than only inferable by reading this file.
    """
    added = [c for c in after_cols if c not in before_cols]
    removed = [c for c in before_cols if c not in after_cols]
    logger.info(
        "[columns] %s: %d -> %d columns | added=%s removed=%s",
        stage, len(before_cols), len(after_cols), added, removed,
    )


@dataclass
class ForecastResult:
    """Everything `TreasuryEngine.run()` produces, bundled in one object: the fitted
    model itself (for a caller that wants to inspect it further), the full and
    horizon-only forecast frames, the headline summary dict, and the optional
    server-rendered trend chart.
    """

    model: Prophet
    full_forecast: pd.DataFrame  # history + horizon, Prophet's raw output
    horizon: pd.DataFrame  # horizon rows only, with liquidity columns added
    # True when an opt-in LightGBM comparison column was requested *and* produced. False
    # when it wasn't asked for, or was asked for and failed//was skipped -- the caller
    # can tell the difference from this flag rather than by probing for the column.
    lightgbm_included: bool
    summary: dict
    trend_decomposition: dict | None  # Plotly figure JSON ({"data": [...], "layout": {...}}),
    # or None if include_charts=False or rendering failed. The funding-gap chart has no
    # server-side counterpart at all -- it's built entirely client-side from `horizon`
    # (already in the API response), so there's nothing to render or store here for it.


class TreasuryEngine:
    """Runs one forecast: `prepare_data()` -> `train_forecast()` ->
    `simulate_liquidity()` -> `summarize()`, or all four at once via `run()`. See the
    module docstring above for why it's decoupled from FastAPI/the data source, and a
    full usage example.
    """

    def __init__(
        self,
        forecast_config: ForecastConfig | None = None,
        liquidity_config: LiquidityConfig | None = None,
    ):
        self.forecast_config = forecast_config or ForecastConfig()
        self.liquidity_config = liquidity_config or LiquidityConfig()

    @staticmethod
    def _validate_input_schema(df: pd.DataFrame) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise DataSourceError(f"Input data is missing required columns: {missing}")
        if not pd.api.types.is_datetime64_any_dtype(df["TRN_Date"]):
            raise DataSourceError(
                "TRN_Date column is not a datetime dtype -- was the data loaded without "
                "parse_dates? (see app/data/schema.py:DATE_COLUMNS)"
            )
        if not pd.api.types.is_numeric_dtype(df["Transaction_Amount_USD"]):
            raise DataSourceError("Transaction_Amount_USD column is not numeric")

    # ------------------------------------------------------------------ #
    # 1. Data preparation
    # ------------------------------------------------------------------ #
    def prepare_data(
        self,
        df: pd.DataFrame,
        *,
        sending_country: str | None = None,
        receiver_country: str | None = None,
        agent_name: str | None = None,
    ) -> pd.DataFrame:
        """Filter to one corridor/agent, keep only "real" funded volume (per
        `include_statuses`), aggregate to a daily volume series, and reindex to a
        continuous daily frequency so gaps become explicit zero-volume days rather than
        silently disappearing from Prophet's view of the calendar.

        Raises `DataSourceError` if `df` doesn't have the expected shape at all (defense
        in depth -- `CSVDataRepository` already checks this, but `run()`/`prepare_data()`
        can also be called directly with an arbitrary DataFrame, e.g. in tests or a future
        Postgres path). Raises `ValueError` if the *filters* just don't match anything --
        that's a caller-input problem, not a data problem.
        """
        self._validate_input_schema(df)

        filtered = df
        if sending_country:
            filtered = filtered[filtered["Sending_Country"].str.upper() == sending_country.upper()]
        if receiver_country:
            filtered = filtered[
                filtered["Receiver_Country"].str.upper() == receiver_country.upper()
            ]
        if agent_name:
            filtered = filtered[filtered["Agent_Name"].str.upper() == agent_name.upper()]
        if not self.forecast_config.include_statuses:
            # An empty tuple used to fall through this filter entirely, which silently
            # meant "count every status as funded volume" -- the opposite of what a caller
            # clearing the list intends, and financially wrong: on NEPAL it pulled in
            # $2.46M of cancelled/blocked/OFAC-held transactions that never moved money
            # and inflated the daily requirement by 3.5%. Fail loudly instead. To include
            # everything, name the statuses explicitly (GET /api/v1/meta/statuses).
            raise ValueError(
                "include_statuses must name at least one transstatus value; an empty "
                "selection is not treated as 'no filter'."
            )
        filtered = filtered[filtered["transstatus"].isin(self.forecast_config.include_statuses)]

        if filtered.empty:
            raise ValueError(
                "No transactions match the given filters "
                f"(sending_country={sending_country!r}, receiver_country={receiver_country!r}, "
                f"agent_name={agent_name!r}, "
                f"include_statuses={self.forecast_config.include_statuses})"
            )

        daily = filtered.groupby(filtered["TRN_Date"].dt.floor("D"))["Transaction_Amount_USD"].sum()
        full_index = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
        daily = daily.reindex(full_index, fill_value=0.0)

        prepared = daily.reset_index()
        prepared.columns = ["ds", "y"]

        _log_column_diff(
            "prepare_data (raw transaction rows -> daily ds/y series)",
            list(df.columns), list(prepared.columns),
        )

        n_days = len(prepared)
        if n_days < 14:
            logger.warning(
                "Only %d days of history after filtering -- Prophet's estimates (especially "
                "weekly/yearly seasonality) will be unreliable at this length.", n_days,
            )
        return prepared

    # ------------------------------------------------------------------ #
    # 1b. Baseline-funding-level suggestion (a helper for the caller to fill in
    #     baseline_funding_level -- not part of the forecast itself, no Prophet
    #     involved). Reuses prepare_data() so it sees exactly the same
    #     corridor/status filtering the real forecast would.
    # ------------------------------------------------------------------ #
    _BASELINE_METHODS = ("average", "weighted_average", "median")

    def suggest_baseline(
        self,
        df: pd.DataFrame,
        *,
        method: str,
        window_days: int,
        sending_country: str | None = None,
        receiver_country: str | None = None,
        agent_name: str | None = None,
    ) -> dict:
        """Suggests a `baseline_funding_level` from trailing historical volume, the way
        a treasury manager would eyeball "what have we typically needed lately" before
        typing a number in. Anchored to *this filter's own* last historical date, same
        convention as the forecast horizon (see prepare_data/train_forecast) -- not
        "today", which may not even be in the data.

        - "average": plain mean over the window.
        - "weighted_average": recency-weighted (EWMA, center of mass ~ window length) --
          recent days count more than older ones in the same window.
        - "median": robust to a single large one-off day; often a better fit than the
          mean for thin/spiky corridors (see the "sparse/spiky" caveat in app/README.md).

        Raises `ValueError` for an unknown method or filters matching no data (via
        `prepare_data`) -- both are caller-input problems, 422-appropriate.
        """
        if method not in self._BASELINE_METHODS:
            raise ValueError(
                f"Unknown baseline suggestion method {method!r}; expected one of "
                f"{self._BASELINE_METHODS}"
            )

        prepared = self.prepare_data(
            df, sending_country=sending_country, receiver_country=receiver_country,
            agent_name=agent_name,
        )
        window = prepared.tail(window_days)
        series = window["y"]

        if method == "average":
            value = series.mean()
        elif method == "weighted_average":
            value = series.ewm(span=len(series), adjust=True).mean().iloc[-1]
        else:  # median
            value = series.median()

        return {
            "method": method,
            "window_days_requested": window_days,
            "window_days_used": len(window),
            "value": float(value),
            "window_start": window["ds"].iloc[0].date().isoformat(),
            "window_end": window["ds"].iloc[-1].date().isoformat(),
        }

    # ------------------------------------------------------------------ #
    # 2. Model training / forecasting
    # ------------------------------------------------------------------ #
    def train_forecast(
        self, prepared_df: pd.DataFrame, *, horizon_days: int | None = None,
    ) -> tuple[Prophet, pd.DataFrame]:
        """Raises `ValueError` if there's not enough history for Prophet to fit at all
        (a data-quantity problem -- 422-appropriate) and `ForecastingError` for anything
        else that goes wrong inside Prophet (a modeling/internal failure -- 500).

        `horizon_days` overrides `self.forecast_config.horizon_days` for just this call,
        without mutating the engine's own config. The one caller that needs this is
        `evaluate_accuracy()`: it re-fits on a truncated history and has to project
        forward exactly as many days as it held out for testing, which is very unlikely
        to equal whatever `horizon_days` the *original* forecast request asked for.
        """
        if len(prepared_df) < 2:
            # Prophet itself raises a hard-to-parse internal error below this size; give
            # a clear message while it's still obviously the caller's (thin) filter.
            raise ValueError(
                f"Not enough history to fit a forecast: only {len(prepared_df)} day(s) of "
                "data after filtering (need at least 2)."
            )

        cfg = self.forecast_config
        days_to_forecast = cfg.horizon_days if horizon_days is None else horizon_days
        horizon_end = prepared_df["ds"].max() + pd.Timedelta(days=days_to_forecast)
        holidays_df = build_combined_holidays(cfg.holidays, prepared_df["ds"].min(), horizon_end)
        if holidays_df is not None:
            logger.info(
                "[columns] holiday/salary-week regressors built (not merged into the main "
                "frame -- passed separately to Prophet): %d row(s), columns=%s, events=%s",
                len(holidays_df), list(holidays_df.columns),
                sorted(holidays_df["holiday"].unique().tolist()),
            )

        # Model Initialization
        model = Prophet(
            daily_seasonality=cfg.seasonality.daily,
            weekly_seasonality=cfg.seasonality.weekly,
            yearly_seasonality=cfg.seasonality.yearly,
            changepoint_prior_scale=cfg.changepoint_prior_scale,
            interval_width=cfg.interval_width,
            growth=cfg.growth,
            seasonality_mode=cfg.seasonality_mode,
            holidays=holidays_df,
        )
        logger.info(
            "Fitting Prophet on %d days of history (horizon=%d days, holidays=%s)",
            len(prepared_df), days_to_forecast,
            "none" if holidays_df is None else f"{len(holidays_df)} events",
        )
        try:
            model.fit(prepared_df)
            future = model.make_future_dataframe(periods=days_to_forecast)
            forecast = model.predict(future)
        except Exception as exc:
            # Prophet/cmdstanpy failures (optimizer non-convergence, the bundled CmdStan
            # binary misbehaving, etc.) aren't the caller's fault and aren't actionable
            # from the error message alone -- log the full traceback server-side and
            # surface a clean, generic failure to the API layer.
            logger.exception(
                "Prophet fit/predict failed (history=%d days, horizon=%d days)",
                len(prepared_df), days_to_forecast,
            )
            raise ForecastingError("Forecast model failed to fit/predict") from exc

        _log_column_diff(
            "train_forecast (Prophet input ds/y -> full forecast output, incl. trend/"
            "seasonality/holiday decomposition)",
            list(prepared_df.columns), list(forecast.columns),
        )

        # Volume can't be negative; Prophet's additive model can dip below zero when the
        # series has many true zero-volume days.
        for col in ("yhat", "yhat_lower", "yhat_upper"):
            forecast[col] = forecast[col].clip(lower=0)
        return model, forecast

    # ------------------------------------------------------------------ #
    # 3. Liquidity risk simulation
    # ------------------------------------------------------------------ #
    def simulate_liquidity(self, forecast: pd.DataFrame) -> pd.DataFrame:
        """Adds, for each day in the forecast horizon:
          - safety_stock: risk-adjusted funding requirement (yhat_upper + configurable buffer)
          - daily_shortfall: how much the baseline funding level falls short that day
          - cumulative_shortfall: running total across the horizon
          - liquidity_to_volume_ratio: baseline / expected volume, to compare corridors
            of very different sizes on a like-for-like basis
        """
        horizon = forecast.tail(self.forecast_config.horizon_days).copy().reset_index(drop=True)
        before_cols = list(horizon.columns)

        buffer_multiplier = 1 + self.liquidity_config.safety_buffer_pct
        baseline = self.liquidity_config.baseline_funding_level

        horizon["safety_stock"] = horizon["yhat_upper"] * buffer_multiplier
        horizon["daily_shortfall"] = (horizon["safety_stock"] - baseline).clip(lower=0)
        horizon["cumulative_shortfall"] = horizon["daily_shortfall"].cumsum()
        horizon["liquidity_to_volume_ratio"] = np.where(
            horizon["yhat"] > 0, baseline / horizon["yhat"], np.nan
        )

        _log_column_diff(
            "simulate_liquidity (forecast horizon -> liquidity-augmented horizon)",
            before_cols, list(horizon.columns),
        )
        return horizon

    # ------------------------------------------------------------------ #
    # 3b. Accuracy evaluation (backtest) -- a caller-triggered check on whether the
    #     forecast is trustworthy, not part of the main run() pipeline (it fits Prophet
    #     a second time, so it costs roughly what one extra forecast run costs).
    # ------------------------------------------------------------------ #
    _NAIVE_BASELINE_WINDOW_DAYS = 30  # matches suggest_baseline()'s own "average" spirit

    def evaluate_accuracy(
        self,
        df: pd.DataFrame,
        *,
        eval_days: int,
        sending_country: str | None = None,
        receiver_country: str | None = None,
        agent_name: str | None = None,
    ) -> dict:
        """Backtests the current config via a single train/test holdout: hide the last
        `eval_days` days of real history, fit on everything before that, forecast
        exactly the hidden window, then compare the forecast against what actually
        happened. This is a much cheaper stand-in for Prophet's own rolling-origin
        `cross_validation()` (which refits repeatedly and can take minutes on a single
        corridor) -- one extra fit, appropriate for an interactive "Evaluate accuracy"
        button rather than a batch job. See `app/README.md`'s "Known caveats" for why a
        single holdout window is itself a caveat on thin/spiky corridors, same as fitting
        is.

        Reports, for the held-out window:
          - MAPE (mean absolute percentage error), computed only over days with nonzero
            actual volume -- percentage error is undefined at actual=0, which real
            corridors do have (see prepare_data's zero-fill on reindex). Days excluded
            for this reason are reported separately, not silently dropped.
          - RMSE, which has no such blind spot and is always computed over every day.
          - interval_coverage_pct: the fraction of days the actual volume landed inside
            Prophet's own [yhat_lower, yhat_upper] band -- tests whether the configured
            `interval_width` is an honest statement of uncertainty, not just a number.
          - safety_stock_coverage_pct: the fraction of days the actual volume was <= the
            safety_stock this config would have funded -- the more directly actionable
            number, since it answers "would the funding plan have been enough" rather
            than "was Prophet's own stated uncertainty honest."
          - A naive baseline (flat prediction = mean of the last
            `_NAIVE_BASELINE_WINDOW_DAYS` training days, i.e. what `suggest_baseline
            (method="average")` would have suggested) for the same window, with its own
            MAPE/RMSE, so a caller can see whether Prophet is actually earning its keep
            over "just fund the recent average every day."

        Raises `ValueError` if there isn't enough history to both train on and hold out
        `eval_days` of it (caller-input problem, 422-appropriate) -- same exception type
        `prepare_data`/`train_forecast` use for the equivalent thin-history case.
        """
        prepared = self.prepare_data(
            df, sending_country=sending_country, receiver_country=receiver_country,
            agent_name=agent_name,
        )

        # same "seasonality unreliable below this" threshold prepare_data() warns at
        min_train_days = 14
        min_required = eval_days + min_train_days
        if len(prepared) < min_required:
            raise ValueError(
                f"Not enough history to evaluate: {len(prepared)} day(s) available after "
                f"filtering, need at least {min_required} ({eval_days} held out for "
                f"testing + {min_train_days} to train on)."
            )

        train = prepared.iloc[:-eval_days].reset_index(drop=True)
        test = prepared.iloc[-eval_days:].reset_index(drop=True)

        _, forecast = self.train_forecast(train, horizon_days=eval_days)
        predicted = forecast.tail(eval_days).reset_index(drop=True)

        actual = test["y"].to_numpy()
        yhat = predicted["yhat"].to_numpy()
        yhat_lower = predicted["yhat_lower"].to_numpy()
        yhat_upper = predicted["yhat_upper"].to_numpy()

        buffer_multiplier = 1 + self.liquidity_config.safety_buffer_pct
        safety_stock = yhat_upper * buffer_multiplier

        naive_window = train["y"].tail(self._NAIVE_BASELINE_WINDOW_DAYS)
        naive_value = float(naive_window.mean())
        naive_pred = np.full(eval_days, naive_value)

        nonzero = actual > 0
        zero_days = int((~nonzero).sum())

        def _mape(pred: np.ndarray) -> float | None:
            if not nonzero.any():
                return None
            return float(np.mean(np.abs(actual[nonzero] - pred[nonzero]) / actual[nonzero]) * 100)

        def _rmse(pred: np.ndarray) -> float:
            return float(np.sqrt(np.mean((actual - pred) ** 2)))

        prophet_mape = _mape(yhat)
        prophet_rmse = _rmse(yhat)
        naive_mape = _mape(naive_pred)
        naive_rmse = _rmse(naive_pred)

        days = []
        for i in range(eval_days):
            # Round first, then compare on the rounded values -- so within_interval/
            # within_safety_stock always agree with what a reader sees in the response.
            # Comparing on the unrounded floats first can disagree with the rounded
            # display at the boundary (e.g. actual=204.998 vs yhat_upper=204.997 both
            # round to 205.00, which would otherwise show "outside the interval" next to
            # two equal-looking numbers).
            actual_r = round(float(actual[i]), 2)
            yhat_lower_r = round(float(yhat_lower[i]), 2)
            yhat_upper_r = round(float(yhat_upper[i]), 2)
            safety_stock_r = round(float(safety_stock[i]), 2)
            days.append({
                "ds": test["ds"].iloc[i].date().isoformat(),
                "actual": actual_r,
                "yhat": round(float(yhat[i]), 2),
                "yhat_lower": yhat_lower_r,
                "yhat_upper": yhat_upper_r,
                "safety_stock": safety_stock_r,
                "naive_prediction": round(naive_value, 2),
                "within_interval": yhat_lower_r <= actual_r <= yhat_upper_r,
                "within_safety_stock": actual_r <= safety_stock_r,
            })

        # Derived from the same rounded per-day flags built above, not a separate
        # unrounded computation -- keeps these headline percentages consistent with
        # what `days` shows (see the rounding note above `days.append`).
        interval_coverage_pct = (
            sum(d["within_interval"] for d in days) / eval_days * 100
        )
        safety_stock_coverage_pct = (
            sum(d["within_safety_stock"] for d in days) / eval_days * 100
        )

        return {
            "eval_days": eval_days,
            "train_days": len(train),
            "window_start": test["ds"].iloc[0].date().isoformat(),
            "window_end": test["ds"].iloc[-1].date().isoformat(),
            "zero_actual_days_excluded_from_mape": zero_days,
            "mape_pct": round(prophet_mape, 2) if prophet_mape is not None else None,
            "rmse": round(prophet_rmse, 2),
            "interval_width_configured": self.forecast_config.interval_width,
            "interval_coverage_pct": round(interval_coverage_pct, 2),
            "safety_stock_coverage_pct": round(safety_stock_coverage_pct, 2),
            "naive_baseline": {
                "method": "average",
                "window_days": len(naive_window),
                "value": round(naive_value, 2),
                "mape_pct": round(naive_mape, 2) if naive_mape is not None else None,
                "rmse": round(naive_rmse, 2),
            },
            "prophet_beats_naive_on_rmse": prophet_rmse <= naive_rmse,
            "prophet_beats_naive_on_mape": (
                None if prophet_mape is None or naive_mape is None else prophet_mape <= naive_mape
            ),
            "days": days,
        }

    # ------------------------------------------------------------------ #
    # 4. Summary
    # ------------------------------------------------------------------ #
    def summarize(self, horizon: pd.DataFrame) -> dict:
        if horizon.empty:
            # Shouldn't happen (horizon_days is validated >= 1 by the API schema, and
            # train_forecast always predicts at least that many rows) -- guarded anyway
            # since idxmax() on an empty series raises an opaque ValueError otherwise.
            raise ForecastingError("Forecast horizon is empty -- nothing to summarize")

        peak_idx = horizon["daily_shortfall"].idxmax()
        peak_row = horizon.loc[peak_idx]
        mean_yhat = horizon["yhat"].mean()

        return {
            "horizon_days": len(horizon),
            "total_shortfall_usd": round(float(horizon["daily_shortfall"].sum()), 2),
            "avg_daily_requirement_usd": round(float(mean_yhat), 2),
            "safety_stock_ratio": (
                round(float(horizon["safety_stock"].mean() / mean_yhat), 4)
                if mean_yhat > 0
                else None
            ),
            "peak_demand_date": peak_row["ds"].date().isoformat(),
            "peak_demand_shortfall_usd": round(float(peak_row["daily_shortfall"]), 2),
            "days_with_shortfall": int((horizon["daily_shortfall"] > 0).sum()),
            "baseline_funding_level_usd": self.liquidity_config.baseline_funding_level,
        }

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def run(
        self,
        df: pd.DataFrame,
        *,
        sending_country: str | None = None,
        receiver_country: str | None = None,
        agent_name: str | None = None,
        include_charts: bool = True,
    ) -> ForecastResult:
        prepared = self.prepare_data(
            df,
            sending_country=sending_country,
            receiver_country=receiver_country,
            agent_name=agent_name,
        )
        model, forecast = self.train_forecast(prepared)
        horizon = self.simulate_liquidity(forecast)
        summary = self.summarize(horizon)

        # Optional LightGBM comparison column. Deliberately computed *after*
        # simulate_liquidity() and never fed into it: every funding number stays
        # Prophet-derived (see LightGBMConfig's docstring for the coverage evidence).
        # Fails soft for the same reason chart rendering does -- a second opinion
        # failing to compute must not cost the caller the forecast they asked for.
        lightgbm_included = False
        if self.forecast_config.include_lightgbm:
            before_cols = list(horizon.columns)
            try:
                predictions = gbdt.forecast_lightgbm(prepared, self.forecast_config)
            except Exception:
                logger.exception(
                    "LightGBM comparison failed -- returning Prophet-only forecast"
                )
                predictions = None
            if predictions is not None and len(predictions) == len(horizon):
                horizon["yhat_lightgbm"] = predictions
                lightgbm_included = True
                _log_column_diff(
                    "run (LightGBM comparison column added)",
                    before_cols, list(horizon.columns),
                )
            elif predictions is not None:
                # Defensive: a length mismatch would silently misalign dates.
                logger.error(
                    "LightGBM returned %d values for a %d-day horizon -- dropping the "
                    "comparison column rather than misaligning it",
                    len(predictions), len(horizon),
                )

        trend_decomposition: dict | None = None
        if include_charts:
            # A chart failing to render is unfortunate but shouldn't cost the caller the
            # (more important) numeric forecast -- log and return None instead of
            # failing the whole request.
            try:
                trend_decomposition = charts.render_trend_decomposition(model, forecast)
            except Exception:
                logger.exception("Failed to render trend_decomposition chart -- omitting it")

        return ForecastResult(
            model=model,
            full_forecast=forecast,
            horizon=horizon,
            lightgbm_included=lightgbm_included,
            summary=summary,
            trend_decomposition=trend_decomposition,
        )
