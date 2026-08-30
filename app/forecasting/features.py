"""Feature engineering for the optional LightGBM forecast.

Prophet needs none of this: it is a pure function of the timestamp (trend + seasonality
+ holiday terms), so `TreasuryEngine.prepare_data()` hands it two columns, `ds` and `y`,
and nothing else. A gradient-boosting model has the opposite requirement -- it has no
notion of time at all and only sees the columns it is given, so every calendar effect
and every piece of "what has volume been doing lately" has to be constructed explicitly.
That is what this module does.

The features fall into two groups, and the split is load-bearing rather than tidiness:

* **Calendar features** depend only on `ds`. They are knowable for any future date, so
  they are computed once for the whole history-plus-horizon frame and never touched
  again (`build_calendar_features`).
* **Lag/rolling features** depend on `y`. Beyond the first horizon day the true `y` does
  not exist yet, so these must be recomputed at every step of the recursive forecast as
  each prediction is appended (`build_lag_features`). Rebuilding only this half per step
  is what keeps a 30-day recursive forecast near a second rather than several.

**Every lag/rolling feature is shifted before use**, so a row can never see its own
target. This is the one invariant in the module that silently produces excellent
backtests and worthless forecasts if broken, and `tests/test_features.py` pins it.

Holiday dates are taken from `app.forecasting.holidays.build_combined_holidays` -- the
same calendar Prophet is given -- rather than a second lookup, so the two models cannot
disagree about when a holiday was.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.forecasting.config import HolidayConfig
from app.forecasting.holidays import build_combined_holidays

logger = logging.getLogger(__name__)

# Lag depths in days. 1-3 capture immediate momentum, 7/14/21/28 capture the weekly
# cycle at increasing distance (28 is four whole weeks, so it aligns with day-of-week),
# 30 approximates the monthly payroll cycle.
LAG_DAYS: tuple[int, ...] = (1, 2, 3, 7, 14, 21, 28, 30)

# Rolling windows for level and dispersion. 90 is included because thin corridors need
# a long window before a mean stops being noise, but it costs 90 days of usable history.
ROLL_WINDOWS: tuple[int, ...] = (7, 14, 30, 90)

# Exponentially-weighted spans -- recency-biased counterparts to the flat means above.
EWMA_SPANS: tuple[int, ...] = (7, 30)


def _cyclical(frame: pd.DataFrame, name: str, values: pd.Series, period: float) -> None:
    """Encode a cyclical quantity as a (sin, cos) pair, in place.

    Raw day-of-week is a lie to a tree: it implies Sunday (6) is six units away from
    Monday (0) when it is adjacent. The sin/cos pair puts the ends of the cycle next to
    each other, so a split can express "weekend" without needing two separate branches.
    """
    angle = 2.0 * np.pi * values.to_numpy(dtype=float) / period
    frame[f"sin_{name}"] = np.sin(angle)
    frame[f"cos_{name}"] = np.cos(angle)


def build_calendar_features(ds: pd.Series, holidays_config: HolidayConfig) -> pd.DataFrame:
    """Date-only features for every row of `ds` -- knowable arbitrarily far ahead.

    Computed once per forecast, not per recursive step, because nothing here depends on
    `y`. Returns a frame indexed positionally to match `ds`.
    """
    ds = pd.to_datetime(pd.Series(ds).reset_index(drop=True))
    out = pd.DataFrame(index=range(len(ds)))

    # --- position in the week/month/year ------------------------------------------
    out["day_of_week"] = ds.dt.dayofweek
    out["day_of_month"] = ds.dt.day
    out["month"] = ds.dt.month
    out["quarter"] = ds.dt.quarter
    out["day_of_year"] = ds.dt.dayofyear
    out["week_of_year"] = ds.dt.isocalendar().week.astype(int)

    # Two weekend conventions: remittance corridors span both, and which one applies is
    # a property of the *sending* market (Gulf corridors historically rest Fri/Sat).
    out["is_weekend_western"] = (ds.dt.dayofweek >= 5).astype(int)
    out["is_weekend_middle_east"] = ds.dt.dayofweek.isin((4, 5)).astype(int)

    out["is_month_start"] = ds.dt.is_month_start.astype(int)
    out["is_month_end"] = ds.dt.is_month_end.astype(int)
    out["days_from_month_start"] = ds.dt.day - 1
    out["days_to_month_end"] = ds.dt.days_in_month - ds.dt.day

    _cyclical(out, "dow", ds.dt.dayofweek, 7.0)
    _cyclical(out, "dom", ds.dt.day, 31.0)
    _cyclical(out, "doy", ds.dt.dayofyear, 365.25)

    # --- payroll cycle --------------------------------------------------------------
    # Remittance volume clusters around payday. Month-end is the dominant pulse; the
    # 14th-16th window is a secondary one in markets that pay twice monthly.
    pre = abs(holidays_config.salary_week_pre_days)
    post = abs(holidays_config.salary_week_post_days)
    days_to_end = out["days_to_month_end"]
    days_from_start = out["days_from_month_start"]
    in_pre = days_to_end <= pre
    in_post = days_from_start < post
    out["is_salary_week"] = (in_pre | in_post).astype(int)
    out["is_salary_window_midmonth"] = ds.dt.day.between(14, 16).astype(int)

    # Signed distance through the payroll window: negative approaching month-end,
    # positive after it. Gives the model a ramp to fit instead of a bare on/off flag.
    phase = np.where(in_pre, -days_to_end, np.where(in_post, days_from_start + 1, 0))
    out["salary_day_phase"] = phase
    span = max(pre + post, 1)
    out["salary_window_intensity"] = np.where(
        out["is_salary_week"] == 1, 1.0 - np.abs(phase) / span, 0.0
    )
    out["days_to_salary_start"] = np.maximum(days_to_end - pre, 0)
    out["days_since_salary_end"] = np.where(in_post, days_from_start + 1, 0)

    # --- public holidays ------------------------------------------------------------
    # Reuses the exact frame handed to Prophet, so the two models cannot disagree about
    # which dates are holidays. Fails soft the same way holidays.py does: an
    # unresolvable country yields all-zero flags rather than an exception.
    sending: set[pd.Timestamp] = set()
    receiver: set[pd.Timestamp] = set()
    if len(ds):
        holidays_df = build_combined_holidays(holidays_config, ds.min(), ds.max())
        if holidays_df is not None and not holidays_df.empty:
            for name, group in holidays_df.groupby("holiday"):
                dates = set(pd.to_datetime(group["ds"]).dt.normalize())
                if str(name).startswith("sending_holiday"):
                    sending |= dates
                elif str(name).startswith("receiver_holiday"):
                    receiver |= dates

    norm = ds.dt.normalize()
    out["is_sending_holiday"] = norm.isin(sending).astype(int)
    out["is_receiver_holiday"] = norm.isin(receiver).astype(int)
    out["is_combined_holiday"] = (
        (out["is_sending_holiday"] == 1) | (out["is_receiver_holiday"] == 1)
    ).astype(int)

    # Senders bring transfers forward ahead of a payout-side closure and the following
    # days are correspondingly quiet -- a shape a single holiday flag cannot express.
    all_holidays = sending | receiver
    rush = np.zeros(len(ds), dtype=int)
    dip = np.zeros(len(ds), dtype=int)
    if all_holidays:
        for offset in (1, 2, 3):
            rush |= (norm + pd.Timedelta(days=offset)).isin(all_holidays).to_numpy()
        for offset in (1, 2):
            dip |= (norm - pd.Timedelta(days=offset)).isin(all_holidays).to_numpy()
    out["is_pre_holiday_rush"] = rush.astype(int)
    out["is_post_holiday_dip"] = dip.astype(int)

    return out


def build_lag_features(y: pd.Series) -> pd.DataFrame:
    """Lag, rolling and momentum features from the volume series.

    Recomputed at each recursive step, because past the first horizon day these depend
    on values the model itself has just predicted.

    Every rolling statistic is computed on `y.shift(1)`, and every lag is at least
    `shift(1)`, so no row can see its own target. Without that shift a backtest looks
    superb and the deployed forecast is worthless -- the failure is silent, which is why
    it is stated here and asserted in the tests.
    """
    y = pd.Series(y).reset_index(drop=True).astype(float)
    prev = y.shift(1)
    out = pd.DataFrame(index=range(len(y)))

    for lag in LAG_DAYS:
        out[f"lag_{lag}d"] = y.shift(lag)

    for window in ROLL_WINDOWS:
        roll = prev.rolling(window, min_periods=2)
        out[f"rolling_mean_{window}d"] = roll.mean()
        out[f"rolling_std_{window}d"] = roll.std()
        if window in (7, 30):
            out[f"rolling_min_{window}d"] = roll.min()
            out[f"rolling_max_{window}d"] = roll.max()
            out[f"rolling_median_{window}d"] = roll.median()

    for span in EWMA_SPANS:
        out[f"ewma_{span}d"] = prev.ewm(span=span, adjust=False).mean()

    # --- momentum and shape ---------------------------------------------------------
    # Ratios are scale-free, which matters because one model is fit per entity and a
    # corridor's absolute level says nothing about whether it is currently accelerating.
    eps = 1e-9
    out["ratio_lag1_to_roll7"] = out["lag_1d"] / (out["rolling_mean_7d"] + eps)
    out["ratio_roll7_to_roll30"] = out["rolling_mean_7d"] / (out["rolling_mean_30d"] + eps)
    out["diff_lag1_lag7"] = out["lag_1d"] - out["lag_7d"]
    out["growth_7d"] = (out["lag_1d"] - out["lag_7d"]) / (out["lag_7d"] + eps)
    out["growth_28d"] = (out["lag_1d"] - out["lag_28d"]) / (out["lag_28d"] + eps)

    # Treasury-shaped risk features: how peaky the recent past was, and where the tail
    # sat. These are the closest thing the feature set has to a volatility signal.
    out["rolling_peak_ratio_7d"] = out["rolling_max_7d"] / (out["rolling_mean_7d"] + eps)
    out["rolling_peak_ratio_30d"] = out["rolling_max_30d"] / (out["rolling_mean_30d"] + eps)
    out["rolling_var_95_30d"] = prev.rolling(30, min_periods=2).quantile(0.95)

    return out


def build_feature_frame(
    ds: pd.Series, y: pd.Series, holidays_config: HolidayConfig
) -> pd.DataFrame:
    """Full feature matrix for a `ds`/`y` series -- calendar and lag halves joined."""
    calendar = build_calendar_features(ds, holidays_config)
    lags = build_lag_features(y)
    return pd.concat([calendar, lags], axis=1)


def feature_columns(holidays_config: HolidayConfig) -> list[str]:
    """The exact column order the model is trained and scored on.

    Derived from the builders themselves against a throwaway series rather than
    maintained as a hand-written list, so adding a feature above cannot silently leave
    it unused at predict time -- the failure mode that a duplicated literal list invites.
    """
    stub_ds = pd.Series(pd.date_range("2024-01-01", periods=8, freq="D"))
    stub_y = pd.Series(np.arange(8, dtype=float))
    return list(build_feature_frame(stub_ds, stub_y, holidays_config).columns)
