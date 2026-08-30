"""Request/response models for the forecast API.

Mirrors `app.forecasting.config` field-for-field so a request body can override any
engine knob; `ForecastRequest` adds the API-level concerns (which dimension to group by,
which corridor/agent to filter to) that don't belong in the engine's own config.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.core.config import settings


class SeasonalityIn(BaseModel):
    daily: bool = Field(
        default=False,
        description="Within-a-day patterns. Off by default -- volume is already totalled "
        "per calendar day before Prophet ever sees it, so there is no intra-day signal to fit.",
    )
    weekly: bool = Field(
        default=True, description="Day-of-week effects, e.g. quieter weekends."
    )
    yearly: bool = Field(
        default=True, description="Annual/seasonal patterns, e.g. festival-season spikes."
    )


class HolidayIn(BaseModel):
    sending_country_code: str | None = Field(
        default=None,
        description=(
            'ISO 3166-1 alpha-2, e.g. "AE". Leave unset to resolve automatically from '
            "sending_country."
        ),
    )
    receiver_country_code: str | None = Field(
        default=None, description="Same as above, for the receiving country."
    )
    holiday_lower_window: int = Field(
        default=-1,
        description="Days before each public holiday that still count as part of its effect.",
    )
    holiday_upper_window: int = Field(
        default=1,
        description="Days after each public holiday that still count as part of its effect.",
    )
    include_salary_week: bool = Field(
        default=True,
        description="Adds a synthetic recurring end-of-month event, since remittance volume "
        "spikes around payday regardless of public holidays.",
    )
    salary_week_pre_days: int = Field(
        default=3, description="Days before month-end included in the synthetic salary-week event."
    )
    salary_week_post_days: int = Field(
        default=3, description="Days after month-end included in the synthetic salary-week event."
    )


class LightGBMIn(BaseModel):
    """Tuning for the optional LightGBM comparison forecast.

    Only read when `ForecastRequest.include_lightgbm` is true. Defaults are tuned for a
    single corridor's ~900 daily observations against ~60 engineered features -- a small
    sample, so the tree is deliberately shallow and regularised rather than deep.
    """

    num_boost_round: int = Field(
        default=300, ge=10, le=5000,
        description="Boosting iterations. More rounds fit the history more closely; past "
        "a few hundred on this data size that increasingly means fitting noise.",
    )
    learning_rate: float = Field(
        default=0.05, gt=0, le=1.0,
        description="Step size per boosting round. Lower needs more rounds but usually "
        "generalises better; pair a lower rate with a higher num_boost_round.",
    )
    num_leaves: int = Field(
        default=31, ge=2, le=1024,
        description="Maximum leaves per tree -- the main capacity control in LightGBM. "
        "Keep well below 2^max_depth to avoid overfitting a corridor's short history.",
    )
    max_depth: int = Field(
        default=6, ge=1, le=32,
        description="Maximum tree depth. Caps how many features can interact in one path.",
    )
    min_data_in_leaf: int = Field(
        default=20, ge=1, le=500,
        description="Minimum observations per leaf. Raising it is the bluntest and most "
        "reliable way to stop the model memorising individual spiky days.",
    )
    feature_fraction: float = Field(
        default=0.85, gt=0, le=1.0,
        description="Fraction of features sampled per iteration. Below 1.0 decorrelates "
        "the trees, which matters here because the lag/rolling features overlap heavily.",
    )
    bagging_fraction: float = Field(
        default=0.80, gt=0, le=1.0,
        description="Fraction of rows sampled per iteration. Needs bagging_freq >= 1 to "
        "take effect.",
    )
    bagging_freq: int = Field(
        default=1, ge=0, le=50,
        description="Perform row bagging every N iterations. 0 disables bagging entirely, "
        "which also makes bagging_fraction inert.",
    )
    lambda_l1: float = Field(
        default=0.0, ge=0,
        description="L1 regularisation on leaf weights. Pushes marginal leaves to zero.",
    )
    lambda_l2: float = Field(
        default=1.0, ge=0,
        description="L2 regularisation on leaf weights -- the primary overfitting defence "
        "at this sample size; the non-zero default is deliberate.",
    )
    random_state: int = Field(
        default=42, ge=0,
        description="Seed for feature/row sampling, so two identical requests return an "
        "identical LightGBM column.",
    )


class DimensionFilters(BaseModel):
    """Which corridor/receiver-country/agent a request applies to, and which statuses
    count as real funded volume -- shared between `ForecastRequest` and
    `BaselineSuggestionRequest` so the two never validate this differently.
    """

    dimension: Literal["receiver_country", "corridor", "agent"] = Field(
        description=(
            "receiver_country: aggregate across all corridors into one receiving country. "
            "corridor: one sending_country -> receiver_country pair. "
            "agent: one Agent_Name, across all corridors it serves."
        )
    )
    sending_country: str | None = Field(
        default=None,
        description="Required (with receiver_country) when dimension='corridor'; ignored "
        "otherwise. Must be a value returned by GET /api/v1/meta/sending-countries.",
    )
    receiver_country: str | None = Field(
        default=None,
        description="Required when dimension is 'receiver_country' or 'corridor'; ignored "
        "for 'agent'. Must be a value returned by GET /api/v1/meta/receiver-countries.",
    )
    agent_name: str | None = Field(
        default=None,
        description="Required when dimension='agent'; ignored otherwise. Must be a value "
        "returned by GET /api/v1/meta/agents.",
    )
    include_statuses: list[str] = Field(
        default_factory=lambda: list(settings.default_include_statuses),
        min_length=1,
        description="Which transstatus values count as real, funded volume -- e.g. cancelled/"
        "failed/expired/compliance-held transactions never moved money and are excluded by "
        "default. See GET /api/v1/meta/statuses for every value present in the data. Must "
        "name at least one status: an empty list is rejected rather than treated as "
        "'no filter', because that would silently count cancelled, failed, blocked and "
        "OFAC-held transactions as funded volume.",
    )

    @model_validator(mode="after")
    def _check_dimension_filters(self) -> DimensionFilters:
        if self.dimension == "receiver_country" and not self.receiver_country:
            raise ValueError("receiver_country is required when dimension='receiver_country'")
        if self.dimension == "corridor" and not (self.sending_country and self.receiver_country):
            raise ValueError(
                "sending_country and receiver_country are both required when dimension='corridor'"
            )
        if self.dimension == "agent" and not self.agent_name:
            raise ValueError("agent_name is required when dimension='agent'")
        return self


class ForecastRequest(DimensionFilters):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "dimension": "receiver_country",
                    "receiver_country": "NEPAL",
                    "horizon_days": 10,
                    "baseline_funding_level": 50000,
                }
            ]
        }
    }

    horizon_days: int = Field(
        default=settings.default_horizon_days,
        ge=1,
        le=settings.max_horizon_days,
        description="Days to forecast ahead, starting the day after the selected filter's "
        "own last historical transaction (not necessarily today).",
    )
    seasonality: SeasonalityIn = Field(
        default_factory=SeasonalityIn, description="Which Prophet seasonality components to fit."
    )
    holidays: HolidayIn = Field(
        default_factory=HolidayIn,
        description="Public-holiday and synthetic salary-week configuration fed to Prophet "
        "as holiday effects.",
    )
    changepoint_prior_scale: float = Field(
        default=0.05,
        gt=0,
        description="Prophet's own trend-flexibility knob -- higher allows the trend to bend "
        "more sharply at detected changepoints. Rarely needs changing from the default.",
    )
    interval_width: float = Field(
        default=0.80,
        gt=0,
        lt=1,
        description="Width of the yhat_lower/yhat_upper uncertainty band, e.g. 0.80 = the "
        "model expects the actual value inside that range ~80% of the time. Directly drives "
        "safety_stock (derived from yhat_upper), so a wider interval means a larger, more "
        "cautious funding recommendation.",
    )
    growth: Literal["linear", "logistic", "flat"] = Field(
        default="linear",
        description='"linear": trend can keep rising/falling. "flat": no trend, seasonality/'
        'holidays only. "logistic" is accepted here but always fails with a 500 -- it needs a '
        "capacity ceiling this engine does not set; deliberately not offered in the dashboard's "
        "own dropdown for that reason.",
    )
    seasonality_mode: Literal["additive", "multiplicative"] = Field(
        default="multiplicative",
        description='"multiplicative" (default): seasonal/holiday effects scale with the trend '
        "level, e.g. \"30% below trend on Sundays\" -- the safer choice for volume data, since "
        'it cannot independently drag the sum below zero the way flat dollar offsets can. '
        '"additive": seasonal/holiday effects are flat dollar amounts added to trend, which can '
        "sum negative on ordinary (not just sparse) corridors and get silently floored to $0.00 "
        "-- see app/README.md's Known caveats for a real corridor where this happened.",
    )

    baseline_funding_level: float = Field(
        default=0.0,
        ge=0,
        description="Liquidity actually planned to be funded each day -- a plain input, never "
        "calculated by this engine. Compared against safety_stock to compute shortfall. See "
        "POST /api/v1/forecast/baseline-suggestion for a data-driven starting point.",
    )
    safety_buffer_pct: float = Field(
        default=0.0,
        ge=0,
        description="Extra cushion stacked on top of the model's own risk-adjusted estimate, "
        "as a FRACTION (e.g. 0.10 = +10%, not 10) -- computed as "
        "safety_stock = yhat_upper * (1 + safety_buffer_pct). The dashboard's own 'Safety "
        "buffer %' field shows/accepts whole percent numbers and divides by 100 before "
        "sending here; a direct API caller must send the fraction itself. Affects "
        "safety_stock only, not the forecast (yhat) itself.",
    )

    include_charts: bool = Field(
        default=True,
        description="Whether to render trend_decomposition server-side. The funding-gap chart "
        "always ships regardless -- it's built entirely from `forecast`, no server rendering "
        "needed.",
    )

    include_lightgbm: bool = Field(
        default=False,
        description="Adds a LightGBM comparison forecast as an extra `yhat_lightgbm` column "
        "on every forecast row, built from ~60 engineered lag/rolling/calendar features. "
        "It is a SECOND OPINION ONLY: safety_stock, daily_shortfall, cumulative_shortfall "
        "and every value in `summary` remain Prophet-derived regardless of this flag, "
        "because a LightGBM quantile band measured 90.3% coverage against Prophet's 96.8% "
        "on the same backtests and would under-fund roughly one day in ten. Costs a second "
        "model fit (~1-2s) and, being lag-based, is forecast recursively -- so its accuracy "
        "degrades further into the horizon than Prophet's does.",
    )
    lightgbm: LightGBMIn = Field(
        default_factory=LightGBMIn,
        description="LightGBM tuning. Ignored unless include_lightgbm is true.",
    )


class BaselineSuggestionRequest(DimensionFilters):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {"dimension": "receiver_country", "receiver_country": "NEPAL",
                 "method": "average", "window_days": 30}
            ]
        }
    }

    method: Literal["average", "weighted_average", "median"] = Field(
        default="average",
        description=(
            "average: plain mean. weighted_average: recency-weighted (EWMA) -- recent days "
            "count more than older ones in the window. median: robust to one-off large "
            "transaction days, often a better fit for thin/spiky corridors."
        ),
    )
    window_days: int = Field(
        default=30, ge=1, le=365, description="Size of the trailing window, in days."
    )


class BaselineSuggestionResponse(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "method": "average",
                    "window_days_requested": 30,
                    "window_days_used": 30,
                    "value": 48213.55,
                    "window_start": "2026-07-02",
                    "window_end": "2026-07-31",
                }
            ]
        }
    }

    method: str = Field(description="Which method actually computed value (echoes the request).")
    window_days_requested: int = Field(description="Echoes the request's window_days.")
    window_days_used: int = Field(
        description="Actual days of history used -- less than requested if the filter's "
        "history is shorter than window_days."
    )
    value: float = Field(
        description="The suggested baseline_funding_level, in the same units as "
        "Transaction_Amount_USD (already USD-normalized)."
    )
    window_start: str = Field(description="First calendar date included in the trailing window.")
    window_end: str = Field(description="The filter's own last historical date, not today.")


class ForecastPoint(BaseModel):
    ds: str = Field(description="Calendar date being forecast, YYYY-MM-DD.")
    weekday: str = Field(
        description="Abbreviated weekday name (Mon..Sun) for ds, computed server-side so the "
        "dashboard never has to parse a date string into a day-of-week itself."
    )
    yhat: float = Field(description="Prophet's expected, most-likely volume for this day.")
    yhat_lower: float = Field(description="Lower edge of the interval_width uncertainty band.")
    yhat_upper: float = Field(description="Upper edge of the interval_width uncertainty band.")
    safety_stock: float = Field(
        description="Risk-adjusted funding recommendation for this day: yhat_upper plus any "
        "safety_buffer_pct."
    )
    daily_shortfall: float = Field(
        description="How much baseline_funding_level falls short of safety_stock this day "
        "(zero if already covered)."
    )
    cumulative_shortfall: float = Field(
        description="Running total of daily_shortfall from the start of the horizon through "
        "this day."
    )
    liquidity_to_volume_ratio: float | None = Field(
        default=None,
        description="baseline_funding_level / yhat -- lets very differently-sized corridors be "
        "compared on the same scale. null when yhat is ~zero (the ratio isn't meaningful there).",
    )
    yhat_lightgbm: float | None = Field(
        default=None,
        description="LightGBM's expected volume for this day -- a comparison against `yhat`, "
        "present only when include_lightgbm was true and the model actually fitted. Has no "
        "uncertainty band and drives no funding number: safety_stock and daily_shortfall above "
        "are always Prophet-derived. A large gap between this and `yhat` is a signal to check "
        "the corridor with POST /api/v1/forecast/evaluate, not a reason to prefer either value.",
    )


class ForecastMeta(BaseModel):
    dimension: str = Field(description="Echoes the request's dimension.")
    sending_country: str | None = Field(description="Echoes the request, unresolved.")
    receiver_country: str | None = Field(description="Echoes the request, unresolved.")
    agent_name: str | None = Field(description="Echoes the request, unresolved.")
    sending_country_code_resolved: str | None = Field(
        description="ISO 3166-1 alpha-2 code resolved from sending_country for holiday lookup, "
        "or the explicit holidays.sending_country_code override if one was given."
    )
    receiver_country_code_resolved: str | None = Field(
        description="Same as above, for the receiving country."
    )
    history_days: int = Field(
        description="Days of actual historical data this filter had available (before the "
        "forecast horizon begins)."
    )
    include_statuses: list[str] = Field(description="Echoes the request's include_statuses.")
    lightgbm_included: bool = Field(
        default=False,
        description="Whether a LightGBM comparison column is actually present on the forecast "
        "rows. False when it wasn't requested, and also when it was requested but skipped "
        "(too little history after the lag warm-up) or failed -- in which case the Prophet "
        "forecast is still returned normally. Check this rather than probing for a null "
        "yhat_lightgbm, which cannot distinguish 'not run' from 'ran and predicted zero'.",
    )


class ForecastSummary(BaseModel):
    """The headline numbers shown as stat tiles on the dashboard -- everything here is
    derived from `forecast` (this is a rollup, not additional data)."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "horizon_days": 10,
                    "total_shortfall_usd": 2536604.7,
                    "avg_daily_requirement_usd": 183238.39,
                    "safety_stock_ratio": 1.6572,
                    "peak_demand_date": "2026-08-02",
                    "peak_demand_shortfall_usd": 321034.12,
                    "days_with_shortfall": 10,
                    "baseline_funding_level_usd": 50000.0,
                }
            ]
        }
    }

    horizon_days: int = Field(description="Number of days actually forecast (echoes the request).")
    total_shortfall_usd: float = Field(
        description="Sum of every day's daily_shortfall across the whole horizon."
    )
    avg_daily_requirement_usd: float = Field(
        description="Average of yhat across the horizon -- the typical expected amount, not "
        "the risk-adjusted one."
    )
    safety_stock_ratio: float | None = Field(
        description="Mean safety_stock / avg_daily_requirement_usd, e.g. 1.6x. Very high "
        "(well above ~2x) signals a thin/spiky corridor, not a modeling problem. null when "
        "avg_daily_requirement_usd is ~zero."
    )
    peak_demand_date: str = Field(
        description="The single day in the horizon with the largest daily_shortfall."
    )
    peak_demand_shortfall_usd: float = Field(description="That day's daily_shortfall.")
    days_with_shortfall: int = Field(
        description="How many days in the horizon have daily_shortfall > 0."
    )
    baseline_funding_level_usd: float = Field(description="Echoes the request's baseline input.")


class ForecastResponse(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "meta": {
                        "dimension": "receiver_country",
                        "sending_country": None,
                        "receiver_country": "NEPAL",
                        "agent_name": None,
                        "sending_country_code_resolved": None,
                        "receiver_country_code_resolved": "NP",
                        "history_days": 943,
                        "include_statuses": ["Payment"],
                    },
                    "summary": {
                        "horizon_days": 10,
                        "total_shortfall_usd": 2536604.7,
                        "avg_daily_requirement_usd": 183238.39,
                        "safety_stock_ratio": 1.6572,
                        "peak_demand_date": "2026-08-02",
                        "peak_demand_shortfall_usd": 321034.12,
                        "days_with_shortfall": 10,
                        "baseline_funding_level_usd": 50000.0,
                    },
                    "forecast": [
                        {
                            "ds": "2026-08-01",
                            "weekday": "Sat",
                            "yhat": 216404.65,
                            "yhat_lower": 103119.0,
                            "yhat_upper": 334633.9,
                            "safety_stock": 334633.9,
                            "daily_shortfall": 284633.9,
                            "cumulative_shortfall": 284633.9,
                            "liquidity_to_volume_ratio": 0.231,
                        }
                    ],
                    "trend_decomposition": None,
                }
            ]
        }
    }

    meta: ForecastMeta = Field(description="Echoes and resolves the request's own filters.")
    summary: ForecastSummary = Field(
        description="Headline figures for the whole horizon -- what the dashboard's stat "
        "tiles show. Always Prophet-derived, even when a LightGBM comparison column is "
        "present on the rows below."
    )
    forecast: list[ForecastPoint] = Field(
        description="One entry per day in the forecast horizon, in chronological order."
    )
    trend_decomposition: dict | None = Field(
        default=None,
        description=(
            "Plotly figure JSON ({'data': [...], 'layout': {...}}) for Prophet's trend/"
            "seasonality/holiday decomposition -- render client-side with "
            "Plotly.newPlot(el, trend_decomposition.data, trend_decomposition.layout). "
            "None if include_charts was false, or if rendering failed server-side. The "
            "funding-gap chart has no server-side counterpart -- build it client-side "
            "from `forecast` directly (every value it needs is already there)."
        ),
    )


class EvaluationRequest(DimensionFilters):
    """Backtests the forecast under a given config rather than running it forward --
    same filter fields as `ForecastRequest` (via `DimensionFilters`), plus the model-
    shaping knobs a caller would want to backtest under. Deliberately not a subclass of
    `ForecastRequest`: `horizon_days` there means "days to forecast into the future",
    which isn't what this endpoint does, so redefining eval_days as this model's own
    primary sizing field avoids a field that would otherwise appear in the schema but
    mean something misleading.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"dimension": "receiver_country", "receiver_country": "NEPAL", "eval_days": 14}
            ]
        }
    }

    eval_days: int = Field(
        default=30,
        ge=7,
        le=180,
        description="Size of the held-out test window, in days -- the most recent eval_days "
        "of real history are hidden, the model is fit on everything before that, and the "
        "held-out window is forecast and compared against what actually happened. Match this "
        "to the horizon_days you actually plan to forecast for the most relevant answer to "
        "'how much should I trust that forecast'.",
    )
    seasonality: SeasonalityIn = Field(
        default_factory=SeasonalityIn,
        description="Same meaning as ForecastRequest.seasonality -- the evaluation fits under "
        "the same settings a real forecast would use.",
    )
    holidays: HolidayIn = Field(
        default_factory=HolidayIn, description="Same meaning as ForecastRequest.holidays."
    )
    changepoint_prior_scale: float = Field(
        default=0.05, gt=0, description="Same meaning as ForecastRequest.changepoint_prior_scale."
    )
    interval_width: float = Field(
        default=0.80,
        gt=0,
        lt=1,
        description="Same meaning as ForecastRequest.interval_width -- also what "
        "interval_coverage_pct in the response is checked against.",
    )
    growth: Literal["linear", "logistic", "flat"] = Field(
        default="linear", description="Same meaning as ForecastRequest.growth."
    )
    seasonality_mode: Literal["additive", "multiplicative"] = Field(
        default="multiplicative", description="Same meaning as ForecastRequest.seasonality_mode."
    )
    baseline_funding_level: float = Field(
        default=0.0, ge=0, description="Same meaning as ForecastRequest.baseline_funding_level."
    )
    safety_buffer_pct: float = Field(
        default=0.0,
        ge=0,
        description="Same meaning as ForecastRequest.safety_buffer_pct -- a fraction, e.g. "
        "0.10 = +10%. Drives safety_stock_coverage_pct in the response.",
    )


class EvaluationDayMetric(BaseModel):
    ds: str = Field(description="Calendar date within the held-out window, YYYY-MM-DD.")
    actual: float = Field(description="What actually happened this day (real historical volume).")
    yhat: float = Field(description="What the backtest model predicted for this day.")
    yhat_lower: float = Field(description="Lower edge of the interval_width uncertainty band.")
    yhat_upper: float = Field(description="Upper edge of the interval_width uncertainty band.")
    safety_stock: float = Field(
        description="What safety_stock would have been funded this day under the given config."
    )
    naive_prediction: float = Field(
        description="The naive baseline's flat prediction for this day (same value every day "
        "in the window -- see EvaluationResponse.naive_baseline)."
    )
    within_interval: bool = Field(
        description="Whether actual fell inside [yhat_lower, yhat_upper] this day."
    )
    within_safety_stock: bool = Field(description="Whether actual <= safety_stock this day.")


class NaiveBaselineComparison(BaseModel):
    """A flat prediction (the recent trailing average -- what suggest_baseline(method=
    'average') would suggest) held constant across the whole evaluation window, reported
    alongside Prophet's own numbers so a caller can see whether the model is actually
    earning its keep over 'just fund the recent average every day'."""

    method: Literal["average"] = Field(
        description="Fixed for now -- the trailing mean of the training window."
    )
    window_days: int = Field(description="Days of training history the mean was computed over.")
    value: float = Field(description="The flat value predicted for every day in the eval window.")
    mape_pct: float | None = Field(
        description="Naive baseline's own MAPE over the same window, same zero-actual-day "
        "exclusion as the model's mape_pct. null under the same all-zero-actuals condition."
    )
    rmse: float = Field(description="Naive baseline's own RMSE over the same window.")


class EvaluationResponse(BaseModel):
    """Result of backtesting one config via a single train/test holdout. See
    POST /api/v1/forecast/evaluate's summary for the method, and
    TreasuryEngine.evaluate_accuracy's docstring for the full reasoning."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "meta": {
                        "dimension": "receiver_country",
                        "sending_country": None,
                        "receiver_country": "NEPAL",
                        "agent_name": None,
                        "sending_country_code_resolved": None,
                        "receiver_country_code_resolved": "NP",
                        "history_days": 943,
                        "include_statuses": ["Payment"],
                    },
                    "eval_days": 14,
                    "train_days": 929,
                    "window_start": "2026-07-18",
                    "window_end": "2026-07-31",
                    "zero_actual_days_excluded_from_mape": 0,
                    "mape_pct": 9.83,
                    "rmse": 28173.4,
                    "interval_width_configured": 0.8,
                    "interval_coverage_pct": 85.71,
                    "safety_stock_coverage_pct": 100.0,
                    "naive_baseline": {
                        "method": "average", "window_days": 30, "value": 172004.5,
                        "mape_pct": 13.76, "rmse": 34521.9,
                    },
                    "prophet_beats_naive_on_rmse": True,
                    "prophet_beats_naive_on_mape": True,
                    "days": [],
                }
            ]
        }
    }

    meta: ForecastMeta = Field(description="Echoes and resolves the request's own filters.")
    eval_days: int = Field(description="Echoes the request's eval_days.")
    train_days: int = Field(
        description="Days of history the backtest model actually trained on (everything "
        "before the held-out window)."
    )
    window_start: str = Field(description="First date in the held-out test window.")
    window_end: str = Field(description="Last date in the held-out test window.")
    zero_actual_days_excluded_from_mape: int = Field(
        description="How many days in the window had zero actual volume and were therefore "
        "excluded from mape_pct (percentage error is undefined at actual=0). Included in "
        "rmse and both coverage metrics regardless -- only mape_pct skips them."
    )
    mape_pct: float | None = Field(
        description="Mean absolute percentage error of yhat vs actual, over non-zero-actual "
        "days only. null if every day in the window had zero actual volume."
    )
    rmse: float = Field(
        description="Root mean squared error of yhat vs actual, over every day in the window."
    )
    interval_width_configured: float = Field(description="Echoes the request's interval_width.")
    interval_coverage_pct: float = Field(
        description="Percent of days where actual fell inside [yhat_lower, yhat_upper]. Compare "
        "against interval_width_configured*100 -- well below it means the model's own stated "
        "uncertainty is too narrow to trust as-is."
    )
    safety_stock_coverage_pct: float = Field(
        description="Percent of days where actual <= safety_stock -- the more directly "
        "actionable number: would this config's funding recommendation actually have been "
        "enough. Should be noticeably higher than interval_coverage_pct whenever "
        "safety_buffer_pct > 0."
    )
    naive_baseline: NaiveBaselineComparison = Field(
        description="The trivial 'fund the recent average every day' strawman this "
        "backtest scores Prophet against, so a caller can see whether the model earned "
        "its keep on this filter."
    )
    prophet_beats_naive_on_rmse: bool = Field(
        description="Whether the model's rmse was <= the naive baseline's."
    )
    prophet_beats_naive_on_mape: bool | None = Field(
        description="Whether the model's mape_pct was <= the naive baseline's. null if either "
        "is null (an all-zero-actuals window)."
    )
    days: list[EvaluationDayMetric] = Field(
        description="One entry per day in the held-out window, in chronological order -- "
        "enough detail to plot actual vs. forecast vs. naive baseline."
    )


class DateRange(BaseModel):
    start: str = Field(description="Earliest TRN_Date in the data, YYYY-MM-DD.")
    end: str = Field(description="Latest TRN_Date in the data, YYYY-MM-DD.")


class DatasetDistinctCounts(BaseModel):
    """Cardinality of each key dimension column. See CLAUDE.md's schema notes for why these
    numbers matter (e.g. transstatus has 11 real values, not the 3 a quick sample suggests)."""

    sending_countries: int = Field(description="Distinct Sending_Country values.")
    receiver_countries: int = Field(description="Distinct Receiver_Country values.")
    agents: int = Field(description="Distinct Agent_Name values.")
    transaction_methods: int = Field(description="Distinct Transaction_Method values.")
    statuses: int = Field(description="Distinct transstatus values.")
    sending_currencies: int = Field(description="Distinct Sending_Country_Currency values.")
    payout_currencies: int = Field(description="Distinct Payout_Currency values.")


class DatasetSummary(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "total_rows": 1430655,
                    "date_range": {"start": "2024-01-01", "end": "2026-07-31"},
                    "distinct_counts": {
                        "sending_countries": 24,
                        "receiver_countries": 71,
                        "agents": 102,
                        "transaction_methods": 178,
                        "statuses": 11,
                        "sending_currencies": 13,
                        "payout_currencies": 35,
                    },
                }
            ]
        }
    }

    total_rows: int = Field(description="Total transaction rows currently loaded.")
    date_range: DateRange = Field(
        description="Earliest and latest TRN_Date present in the loaded dataset."
    )
    distinct_counts: DatasetDistinctCounts = Field(
        description="Number of distinct values per key dimension column, for sanity-"
        "checking what is actually loaded without re-running the EDA report."
    )


class ErrorResponse(BaseModel):
    """Shape of every error response this API returns (FastAPI's default `HTTPException`
    body is already `{"detail": ...}` -- this just gives that shape a name so it shows up
    properly in the OpenAPI schema instead of being undocumented).
    """

    model_config = {"json_schema_extra": {"examples": [{"detail": "Data source unavailable"}]}}

    detail: str = Field(description="Human-readable explanation of what went wrong.")


class RootResponse(BaseModel):
    """What GET / returns -- an orientation point for anyone hitting the bare API root,
    since this service has no real landing page of its own."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "Global Treasury Forecasting & Liquidity Engine",
                    "version": "0.1.0",
                    "docs_url": "/docs",
                    "redoc_url": "/redoc",
                    "dashboard_url": "/dashboard/",
                    "health_url": "/health",
                    "ready_url": "/ready",
                }
            ]
        }
    }

    name: str = Field(description="This API's title, matching the OpenAPI document's own.")
    version: str = Field(description="This API's version, matching the OpenAPI document's own.")
    docs_url: str = Field(description="Interactive OpenAPI docs (Swagger UI).")
    redoc_url: str = Field(description="Alternate OpenAPI docs (ReDoc).")
    dashboard_url: str = Field(description="The plain HTML/JS Treasury Management dashboard.")
    health_url: str = Field(description="Liveness check.")
    ready_url: str = Field(description="Readiness check.")


class HealthResponse(BaseModel):
    """Pure liveness -- the process is up and answering HTTP, nothing more. Never touches
    the data source, so it stays fast and dependency-free even if that's unavailable; see
    GET /ready for a check that does look at data-source health."""

    model_config = {"json_schema_extra": {"examples": [{"status": "ok"}]}}

    status: Literal["ok"] = Field(
        description='Always "ok" -- a non-200 response IS the failure signal.'
    )


class ReadyResponse(BaseModel):
    """Readiness -- confirms the configured data source actually loads, not just that the
    process is up. A caller (a load balancer, an orchestrator) should treat a 503 here as
    "don't route traffic yet/still", distinct from GET /health's liveness-only check."""

    model_config = {
        "json_schema_extra": {
            "examples": [{"status": "ok", "data_backend": "csv", "rows_loaded": 1430655}]
        }
    }

    status: Literal["ok"] = Field(description="Always \"ok\" -- a 503 IS the not-ready signal.")
    data_backend: str = Field(description="Echoes Settings.data_backend.")
    rows_loaded: int = Field(description="Row count of the currently loaded data.")
