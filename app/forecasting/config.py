"""Configuration objects for a forecast run.

Everything a caller might reasonably want to tune lives here, with defaults that make
sense for daily transaction-volume data. The API layer (`app/api/schemas.py`) mirrors
these fields so a request body can override any of them -- that's what "highly
configurable" cashes out to in practice: one engine, one set of knobs, exposed both to
direct Python callers and to the API/dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SeasonalityConfig:
    daily: bool = False
    weekly: bool = True
    yearly: bool = True


@dataclass
class HolidayConfig:
    """Controls the holidays passed into Prophet.

    `sending_country_code` / `receiver_country_code` are ISO 3166-1 alpha-2 codes (e.g.
    "AE", "NP") resolved from the dataset's free-text country names via
    `app.forecasting.holidays.resolve_country_code`. Leave unset to skip a side's public
    holidays entirely rather than guess.

    "Salary week" models the recurring end-of-month payroll spike as its own Prophet
    holiday: one event per month spanning `salary_week_pre_days` before month-end through
    `salary_week_post_days` into the next month.
    """

    sending_country_code: str | None = None
    receiver_country_code: str | None = None
    holiday_lower_window: int = -1
    holiday_upper_window: int = 1

    include_salary_week: bool = True
    salary_week_pre_days: int = 3
    salary_week_post_days: int = 3


@dataclass
class LightGBMConfig:
    """Parameters for the optional LightGBM comparison forecast.

    LightGBM is **opt-in and never authoritative**: it contributes an extra `yhat`
    column so a caller can compare a feature-based gradient-boosting view against
    Prophet's, but every liquidity number (`safety_stock`, `daily_shortfall`,
    `cumulative_shortfall`, and the whole `summarize()` dict) stays Prophet-derived.

    That split is deliberate and measured, not conservatism for its own sake. Across 36
    rolling-origin backtests (12 entities x 3 windows, all three dimensions) a
    LightGBM quantile band covered 90.3% of held-out days against Prophet's 96.8% at
    the same nominal width -- i.e. it under-funds roughly one day in ten, which is the
    one failure mode a prefunding plan cannot absorb. Its *point* forecast, however, is
    genuinely competitive on some entities, which is exactly what a comparison column
    is for. See `app/README.md`'s "Known caveats" for the full evidence.

    Every field below maps to a LightGBM parameter of the same name (except
    `num_boost_round`, which is a `lgb.train()` argument rather than a param).
    """

    # Boosting control.
    num_boost_round: int = 300
    learning_rate: float = 0.05

    # Tree shape. Deliberately shallow: a single corridor has ~900 daily observations,
    # which does not support a deep tree over ~60 features without memorising noise.
    num_leaves: int = 31
    max_depth: int = 6
    min_data_in_leaf: int = 20

    # Stochastic regularisation (feature/row subsampling per iteration).
    feature_fraction: float = 0.85
    bagging_fraction: float = 0.80
    bagging_freq: int = 1

    # Explicit L1/L2 penalties -- the main defence against the small-sample overfitting
    # the tree-shape limits above only partly address.
    lambda_l1: float = 0.0
    lambda_l2: float = 1.0

    # Fixed so two identical requests return identical numbers; Prophet's own interval
    # sampling is not seeded, but the LightGBM column at least should be reproducible.
    random_state: int = 42


@dataclass
class ForecastConfig:
    horizon_days: int = 30
    seasonality: SeasonalityConfig = field(default_factory=SeasonalityConfig)
    holidays: HolidayConfig = field(default_factory=HolidayConfig)

    # Opt-in second opinion. False keeps the request/response and the runtime cost
    # exactly as they were before LightGBM existed.
    include_lightgbm: bool = False
    lightgbm: LightGBMConfig = field(default_factory=LightGBMConfig)

    # Prophet knobs exposed directly since they materially change forecast behavior.
    changepoint_prior_scale: float = 0.05
    interval_width: float = 0.80  # width of yhat_lower/yhat_upper
    growth: str = "linear"  # "linear" | "logistic" | "flat"

    # "multiplicative" (not Prophet's own "additive" default): seasonal/holiday effects
    # scale with the trend level (trend * (1 + seasonal_pct)) instead of being flat
    # dollar offsets added to it. Changed from "additive" after a real defect found via
    # technical.md's worked example (Receiver Country: Australia) -- additive mode let
    # a large negative weekly effect (-$105K) stack with a large negative yearly effect
    # (-$42K) on top of a ~$112K trend, summing below zero on ordinary weekends and
    # getting silently floored to $0.00 by the clip in train_forecast(), even though
    # that corridor's real historical weekend volume averages ~$50K/day (2.5 years of
    # data, 96.6% of weekends nonzero -- not a thin/sparse corridor). Multiplicative
    # mode expresses the same weekly/yearly effects as percentages of trend, which
    # can't independently drag the sum past -100% of trend the way flat dollar
    # offsets can. See app/README.md's "Known caveats" for the full writeup and
    # technical.md §10 for the corrected worked example.
    seasonality_mode: str = "multiplicative"  # "additive" | "multiplicative"

    # Only statuses in this tuple count as real, funded volume (see app/data/schema.py --
    # transstatus has 11 real-world values, not just Payment/Cancel/Block).
    include_statuses: tuple[str, ...] = ("Payment",)


@dataclass
class LiquidityConfig:
    """Parameters for the liquidity-risk / gap-analysis simulation."""

    baseline_funding_level: float = 0.0
    # Extra margin stacked on top of Prophet's own yhat_upper, e.g. 0.10 = +10%.
    safety_buffer_pct: float = 0.0
