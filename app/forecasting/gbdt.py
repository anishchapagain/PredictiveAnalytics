"""Optional LightGBM comparison forecast.

This module produces one thing: an extra `yhat_lightgbm` column alongside Prophet's
`yhat`, so a caller can see what a feature-based gradient-boosting model makes of the
same corridor. It deliberately produces **no uncertainty band and no liquidity numbers**
-- `safety_stock`, `daily_shortfall` and everything in `summarize()` stay Prophet-derived
(see `LightGBMConfig`'s docstring for the coverage evidence behind that split).

Two properties of gradient boosting shape everything below:

1. **It has no notion of time.** Every calendar and momentum effect must be supplied as
   a column, which is what `app.forecasting.features` exists for.
2. **It cannot extrapolate.** A tree predicts a constant within each leaf, so it can
   never return a value outside the range it was trained on. On a trending corridor that
   caps the forecast at the historical maximum, and during a recursive forecast a drift
   can lock the model into a wrong regime for the rest of the horizon. This is not a
   tuning problem; it is what trees are. It is the measured reason LightGBM is offered
   as a comparison rather than as the funding basis.

Forecasting is **recursive**: day one is predicted from real history, then that
prediction is appended and day two is predicted from it, and so on. There is no
alternative for a lag-based model asked for a multi-day horizon -- the lags for day five
simply do not exist yet -- but it does mean error compounds across the horizon, so a
30-day LightGBM column is a materially weaker claim than a 3-day one.
"""

from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from app.core.exceptions import ForecastingError
from app.forecasting.config import ForecastConfig
from app.forecasting.features import build_calendar_features, build_lag_features

logger = logging.getLogger(__name__)

# Below this many usable training rows the model is not worth fitting: the lag/rolling
# features need a warm-up before they are populated at all, and what remains after that
# would be fit noise. `forecast_lightgbm` returns None rather than a bad column.
MIN_TRAIN_ROWS = 60


def _lgb_params(config: ForecastConfig) -> dict:
    """Map `LightGBMConfig` onto LightGBM's own parameter names."""
    cfg = config.lightgbm
    return {
        "objective": "regression",  # L2: a central estimate, matching Prophet's yhat
        "metric": "rmse",
        "boosting_type": "gbdt",
        "learning_rate": cfg.learning_rate,
        "num_leaves": cfg.num_leaves,
        "max_depth": cfg.max_depth,
        "min_data_in_leaf": cfg.min_data_in_leaf,
        "feature_fraction": cfg.feature_fraction,
        "bagging_fraction": cfg.bagging_fraction,
        "bagging_freq": cfg.bagging_freq,
        "lambda_l1": cfg.lambda_l1,
        "lambda_l2": cfg.lambda_l2,
        "seed": cfg.random_state,
        "verbose": -1,
        "num_threads": 0,  # let LightGBM size to the host; the call already runs in a
                           # worker thread via asyncio.to_thread, so it is not blocking
                           # the event loop while it does.
    }


def _log_feature_matrix(feature_names: list[str], usable_rows: int, history_rows: int) -> None:
    """Log the engineered feature matrix by name, grouped by family.

    `engine.py`'s `_log_column_diff` names every column at each Prophet-side boundary, on the
    principle that the pipeline's feature-engineering steps should be readable from the logs
    alone rather than only by reading the source. This is the largest expansion in the whole
    pipeline -- two columns of ds/y become ~61 engineered features -- so it gets the same
    treatment, in the same `[columns]` format so both grep together.

    Grouped rather than dumped flat: 61 names on one line is a wall of text, and the grouping
    is the useful part when reading back a run ("were the lags there? did the holiday flags
    resolve?"). The full ordered list follows at DEBUG for anyone reproducing a fit exactly.
    """
    groups: dict[str, list[str]] = {
        "lag": [], "rolling": [], "ewma": [], "momentum": [], "holiday": [],
        "salary": [], "calendar": [],
    }
    for name in feature_names:
        if name.startswith("lag_"):
            groups["lag"].append(name)
        elif name.startswith("rolling_"):
            groups["rolling"].append(name)
        elif name.startswith("ewma_"):
            groups["ewma"].append(name)
        elif name.startswith(("ratio_", "diff_", "growth_")):
            groups["momentum"].append(name)
        elif "holiday" in name:
            groups["holiday"].append(name)
        elif "salary" in name:
            groups["salary"].append(name)
        else:
            groups["calendar"].append(name)

    logger.info(
        "[columns] LightGBM feature engineering (ds/y -> engineered matrix): 2 -> %d columns "
        "| %d of %d history rows usable after lag warm-up",
        len(feature_names), usable_rows, history_rows,
    )
    for family, names in groups.items():
        if names:
            logger.info("[columns]   %-8s (%2d): %s", family, len(names), ", ".join(names))
    logger.debug("[columns] LightGBM full ordered feature list: %s", feature_names)


def forecast_lightgbm(
    prepared_df: pd.DataFrame, config: ForecastConfig, *, horizon_days: int | None = None
) -> np.ndarray | None:
    """Recursive multi-step LightGBM forecast for the horizon.

    Takes the same `ds`/`y` frame Prophet is fit on, so the two models see identical
    history and identical filtering. Returns one non-negative value per horizon day, or
    `None` when there is not enough history to fit (the caller then simply omits the
    comparison column rather than failing the forecast).

    Raises `ForecastingError` only for a genuine model failure, which the caller
    deliberately catches -- a broken comparison column must never cost the caller the
    Prophet forecast they actually asked for.
    """
    # Imported lazily: an opt-in feature should not add LightGBM's import cost to every
    # process that only ever runs Prophet, and a Prophet-only deployment should still
    # start cleanly if the wheel is missing.
    import lightgbm as lgb

    horizon = config.horizon_days if horizon_days is None else horizon_days
    history = prepared_df[["ds", "y"]].copy().reset_index(drop=True)
    history["ds"] = pd.to_datetime(history["ds"])

    # Extend the calendar across the horizon up front. Calendar features never depend on
    # y, so they are built once here for history+horizon and simply indexed into during
    # the recursive loop below -- the single biggest cost saving in this function.
    future_ds = pd.date_range(
        history["ds"].max() + pd.Timedelta(days=1), periods=horizon, freq="D"
    )
    full_ds = pd.concat([history["ds"], pd.Series(future_ds)], ignore_index=True)
    calendar = build_calendar_features(full_ds, config.holidays)

    n_hist = len(history)
    y_values = np.concatenate([history["y"].to_numpy(dtype=float), np.full(horizon, np.nan)])

    # --- fit on history only -------------------------------------------------------
    lag_hist = build_lag_features(pd.Series(y_values[:n_hist]))
    train = pd.concat([calendar.iloc[:n_hist].reset_index(drop=True), lag_hist], axis=1)
    target = pd.Series(y_values[:n_hist])

    usable = train.notna().all(axis=1)
    if int(usable.sum()) < MIN_TRAIN_ROWS:
        logger.warning(
            "LightGBM skipped: only %d usable training rows after lag warm-up "
            "(need >= %d) -- returning Prophet-only forecast",
            int(usable.sum()), MIN_TRAIN_ROWS,
        )
        return None

    feature_names = list(train.columns)
    _log_feature_matrix(feature_names, int(usable.sum()), len(history))

    started = time.perf_counter()
    try:
        model = lgb.train(
            _lgb_params(config),
            lgb.Dataset(train.loc[usable], label=target.loc[usable],
                        feature_name=feature_names),
            num_boost_round=config.lightgbm.num_boost_round,
        )
    except Exception as exc:
        logger.exception(
            "LightGBM fit failed (rows=%d, features=%d)", int(usable.sum()), len(feature_names)
        )
        raise ForecastingError("LightGBM model failed to fit") from exc

    fit_seconds = time.perf_counter() - started
    logger.info(
        "LightGBM fit on %d rows x %d features in %.2fs (rounds=%d, lr=%.3f, leaves=%d)",
        int(usable.sum()), len(feature_names), fit_seconds,
        config.lightgbm.num_boost_round, config.lightgbm.learning_rate,
        config.lightgbm.num_leaves,
    )

    # --- recursive multi-step forecast ---------------------------------------------
    started = time.perf_counter()
    predictions: list[float] = []
    try:
        for step in range(horizon):
            row_idx = n_hist + step
            # Only the lag half is rebuilt each step, and only up to the current row --
            # the calendar half was computed once above.
            lag_now = build_lag_features(pd.Series(y_values[: row_idx + 1])).iloc[[-1]]
            features = pd.concat(
                [calendar.iloc[[row_idx]].reset_index(drop=True),
                 lag_now.reset_index(drop=True)],
                axis=1,
            )[feature_names].fillna(0.0)
            value = float(model.predict(features)[0])
            value = max(0.0, value)  # volume cannot be negative
            predictions.append(value)
            y_values[row_idx] = value  # becomes tomorrow's lag_1d
    except Exception as exc:
        logger.exception("LightGBM recursive prediction failed at step %d", len(predictions))
        raise ForecastingError("LightGBM model failed to predict") from exc

    logger.info(
        "LightGBM recursive forecast: %d days in %.2fs (mean=%.2f, min=%.2f, max=%.2f)",
        horizon, time.perf_counter() - started,
        float(np.mean(predictions)), float(np.min(predictions)), float(np.max(predictions)),
    )
    return np.asarray(predictions, dtype=float)
