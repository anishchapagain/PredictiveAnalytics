"""Unit tests for the LightGBM feature-engineering pipeline.

The single most important property here is that **no lag or rolling feature may see its
own row's target**. A leak there is silent -- it produces an excellent-looking backtest
and a worthless forecast -- so `test_no_lag_or_rolling_feature_leaks_current_value`
checks it directly against a series constructed so any leak is arithmetically visible,
rather than trusting that the shifts in `features.py` are written correctly.
"""

import numpy as np
import pandas as pd

from app.forecasting.config import HolidayConfig
from app.forecasting.features import (
    LAG_DAYS,
    build_calendar_features,
    build_feature_frame,
    build_lag_features,
    feature_columns,
)

_DS = pd.Series(pd.date_range("2024-01-01", periods=120, freq="D"))


# --- the leak guard -------------------------------------------------------------------

def test_no_lag_or_rolling_feature_leaks_current_value():
    """Every y-derived feature must be computable without knowing today's y.

    Built by giving one day a value so extreme it would dominate any statistic that
    included it: if that day's own row shows the spike in a lag/rolling column, the
    shift is missing.
    """
    y = pd.Series([100.0] * 120)
    spike_at = 60
    y.iloc[spike_at] = 1_000_000.0

    feats = build_lag_features(y)
    row = feats.iloc[spike_at]
    for column, value in row.items():
        if pd.isna(value):
            continue
        assert value < 500_000.0, (
            f"{column} on the spike day carries the spike itself -- the feature is "
            f"reading its own target (value={value})"
        )
    # ...and the day *after* must see it, otherwise the shift went one too far and the
    # features are simply stale rather than non-leaking.
    assert feats["lag_1d"].iloc[spike_at + 1] == 1_000_000.0


def test_lag_columns_are_exactly_shifted():
    y = pd.Series(np.arange(120, dtype=float))
    feats = build_lag_features(y)
    for lag in LAG_DAYS:
        col = feats[f"lag_{lag}d"]
        assert col.iloc[lag] == 0.0          # first defined position holds y[0]
        assert col.iloc[100] == 100.0 - lag  # and stays exactly `lag` behind
        assert col.iloc[:lag].isna().all()   # nothing invented before history starts


def test_rolling_mean_excludes_the_current_day():
    y = pd.Series([10.0] * 10 + [0.0] * 10)
    feats = build_lag_features(y)
    # At index 10 (first zero) the trailing 7-day mean must still be 10.0: the window
    # ends yesterday, so today's zero cannot have entered it yet.
    assert feats["rolling_mean_7d"].iloc[10] == 10.0


# --- calendar features ----------------------------------------------------------------

def test_calendar_features_need_no_target_and_cover_the_future():
    """Calendar features must be computable for dates with no y at all -- that is what
    makes them safe to build once for history+horizon instead of per recursive step."""
    future = pd.Series(pd.date_range("2030-06-01", periods=45, freq="D"))
    cal = build_calendar_features(future, HolidayConfig())
    assert len(cal) == 45
    assert not cal.isna().any().any()


def test_cyclical_encoding_wraps_around_the_week():
    cal = build_calendar_features(_DS, HolidayConfig())
    # Sunday (6) and Monday (0) are adjacent in reality; the sin/cos pair must place
    # them closer together than plain integer day-of-week does.
    by_dow = cal.groupby("day_of_week")[["sin_dow", "cos_dow"]].first()
    sun, mon, wed = by_dow.loc[6].to_numpy(), by_dow.loc[0].to_numpy(), by_dow.loc[2].to_numpy()
    assert np.linalg.norm(sun - mon) < np.linalg.norm(sun - wed)


def test_salary_week_flag_covers_month_end_and_month_start():
    cal = build_calendar_features(_DS, HolidayConfig(salary_week_pre_days=3,
                                                     salary_week_post_days=3))
    frame = pd.DataFrame({"ds": _DS, "flag": cal["is_salary_week"]})
    flagged = set(frame.loc[frame["flag"] == 1, "ds"].dt.strftime("%m-%d"))
    assert "01-29" in flagged and "01-31" in flagged  # run-up to month end
    assert "02-01" in flagged                          # and just after it
    assert "01-15" not in flagged                      # mid-month is not salary week


def test_holiday_flags_are_zero_when_no_country_resolves():
    """Fail-soft contract: an unresolvable country yields zero flags, never an error."""
    cal = build_calendar_features(_DS, HolidayConfig(sending_country_code=None,
                                                     receiver_country_code=None))
    for column in ("is_sending_holiday", "is_receiver_holiday", "is_combined_holiday"):
        assert (cal[column] == 0).all()


def test_receiver_holidays_are_flagged_when_a_country_resolves():
    cal = build_calendar_features(_DS, HolidayConfig(receiver_country_code="US"))
    frame = pd.DataFrame({"ds": _DS, "flag": cal["is_receiver_holiday"]})
    new_year = frame.loc[frame["ds"] == "2024-01-01", "flag"].iloc[0]
    assert new_year == 1
    # The rush window looks forward at the holiday, so 30 Dec/31 Dec-style days before a
    # holiday are flagged; an ordinary mid-January day is not.
    assert frame.loc[frame["ds"] == "2024-01-17", "flag"].iloc[0] == 0


# --- contract between builders and the model ------------------------------------------

def test_feature_columns_matches_what_build_feature_frame_produces():
    """feature_columns() is what gbdt.py trains and scores on; if it drifts from the
    builders, features silently stop being used."""
    frame = build_feature_frame(_DS, pd.Series(np.arange(120, dtype=float)), HolidayConfig())
    assert list(frame.columns) == feature_columns(HolidayConfig())
    assert len(frame.columns) > 40  # the "detailed" feature set, not a token few


def test_feature_frame_rows_align_with_input_dates():
    y = pd.Series(np.arange(120, dtype=float))
    frame = build_feature_frame(_DS, y, HolidayConfig())
    assert len(frame) == len(_DS)
    assert frame.index.tolist() == list(range(len(_DS)))
