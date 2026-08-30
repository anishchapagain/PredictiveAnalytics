"""Forecast + baseline-suggestion endpoints.

Prophet's `fit`/`predict` are synchronous, CPU-bound calls -- there is no native async
Prophet API. Routes stay `async def` and responsive under concurrent load by running the
actual engine work in a worker thread via `asyncio.to_thread`, rather than blocking the
event loop for the several seconds a fit can take.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_repository
from app.api.schemas import (
    BaselineSuggestionRequest,
    BaselineSuggestionResponse,
    DimensionFilters,
    ErrorResponse,
    EvaluationRequest,
    EvaluationResponse,
    ForecastMeta,
    ForecastRequest,
    ForecastResponse,
)
from app.core.exceptions import DataSourceError, ForecastingError
from app.data.repository import DataRepository
from app.forecasting.config import (
    ForecastConfig,
    HolidayConfig,
    LightGBMConfig,
    LiquidityConfig,
    SeasonalityConfig,
)
from app.forecasting.engine import TreasuryEngine
from app.forecasting.holidays import resolve_country_code

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/forecast", tags=["forecast"])

_ERROR_RESPONSES = {
    422: {"model": ErrorResponse, "description": "Invalid request body or no matching data"},
    500: {"model": ErrorResponse, "description": "Internal error"},
    503: {"model": ErrorResponse, "description": "Data source unavailable/unreadable"},
}

# Fixed (locale-independent) abbreviations -- pandas' own %a strftime directive would
# follow the server's locale, which could silently swap these for another language.
# Monday=0 .. Sunday=6 matches pandas' Timestamp.dayofweek convention.
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


async def _run_or_raise[T](fn: Callable[..., T], *args) -> T:
    """Runs a blocking call in a worker thread, translating this app's exception
    hierarchy into the matching `HTTPException` -- shared by every route below so each
    one doesn't repeat the same try/except block.
    """
    try:
        return await asyncio.to_thread(fn, *args)
    except ValueError as exc:
        # Caller's filters/request didn't match anything or aren't enough to compute
        # from -- their fault, and the message says exactly why.
        logger.warning("Request failed validation: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DataSourceError as exc:
        # The underlying data (CSV today, a DB later) couldn't be read -- infrastructure
        # problem, not the caller's. 503 (service unavailable), not 500, since it's likely
        # transient (file being rewritten, disk hiccup) and retryable.
        logger.error("Request failed -- data source unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Data source unavailable") from exc
    except ForecastingError as exc:
        # The model itself failed for a reason that isn't the caller's filters (Prophet
        # internal error) -- genuine 500, full traceback already logged where it was raised.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception:
        # Last-resort safety net: never let an unanticipated exception escape as a bare,
        # unlogged 500 with a leaked traceback. Log full details server-side, tell the
        # caller nothing more than that something went wrong.
        logger.exception("Unhandled error while processing request")
        raise HTTPException(status_code=500, detail="Internal server error") from None


def _resolve_dimension_filters(
    request: DimensionFilters,
) -> tuple[str | None, str | None, str | None]:
    """(sending_country, receiver_country, agent_name) to actually filter on, given
    which dimension the request is grouped by -- shared by forecast and
    baseline-suggestion requests so "only the filters relevant to this dimension apply"
    lives in exactly one place.
    """
    sending = request.sending_country if request.dimension == "corridor" else None
    receiver = request.receiver_country if request.dimension != "agent" else None
    agent = request.agent_name if request.dimension == "agent" else None
    return sending, receiver, agent


def _run_engine_sync(request: ForecastRequest, df: pd.DataFrame) -> ForecastResponse:
    sending_code = (
        request.holidays.sending_country_code or resolve_country_code(request.sending_country)
    )
    receiver_code = (
        request.holidays.receiver_country_code or resolve_country_code(request.receiver_country)
    )

    logger.info("Sending Code: %s (%s) - Receiver Code: %s (%s)",
                sending_code, request.sending_country,
                receiver_code, request.receiver_country)

    forecast_config = ForecastConfig(
        horizon_days=request.horizon_days,
        seasonality=SeasonalityConfig(**request.seasonality.model_dump()),
        holidays=HolidayConfig(
            sending_country_code=sending_code,
            receiver_country_code=receiver_code,
            holiday_lower_window=request.holidays.holiday_lower_window,
            holiday_upper_window=request.holidays.holiday_upper_window,
            include_salary_week=request.holidays.include_salary_week,
            salary_week_pre_days=request.holidays.salary_week_pre_days,
            salary_week_post_days=request.holidays.salary_week_post_days,
        ),
        changepoint_prior_scale=request.changepoint_prior_scale,
        interval_width=request.interval_width,
        growth=request.growth,
        seasonality_mode=request.seasonality_mode,
        include_lightgbm=request.include_lightgbm,
        lightgbm=LightGBMConfig(**request.lightgbm.model_dump()),
        include_statuses=tuple(request.include_statuses),
    )
    liquidity_config = LiquidityConfig(
        baseline_funding_level=request.baseline_funding_level,
        safety_buffer_pct=request.safety_buffer_pct,
    )

    engine = TreasuryEngine(forecast_config, liquidity_config)
    sending, receiver, agent = _resolve_dimension_filters(request)

    result = engine.run(
        df,
        sending_country=sending,
        receiver_country=receiver,
        agent_name=agent,
        include_charts=request.include_charts,
    )

    history_days = int(result.full_forecast.shape[0] - request.horizon_days)

    # Only select the LightGBM column when it was actually produced -- requesting it and
    # having it skip (thin history) or fail must still return a well-formed response.
    forecast_columns = [
        "ds", "yhat", "yhat_lower", "yhat_upper", "safety_stock", "daily_shortfall",
        "cumulative_shortfall", "liquidity_to_volume_ratio",
    ]
    if result.lightgbm_included:
        forecast_columns.append("yhat_lightgbm")

    return ForecastResponse(
        meta=ForecastMeta(
            dimension=request.dimension,
            sending_country=request.sending_country,
            receiver_country=request.receiver_country,
            agent_name=request.agent_name,
            sending_country_code_resolved=sending_code,
            receiver_country_code_resolved=receiver_code,
            history_days=history_days,
            include_statuses=list(request.include_statuses),
            lightgbm_included=result.lightgbm_included,
        ),
        summary=result.summary,
        forecast=result.horizon[forecast_columns].assign(
            # weekday must be computed before ds is overwritten below -- pandas' assign()
            # evaluates kwargs in order, so listing it first still sees the datetime ds.
            weekday=lambda d: d["ds"].dt.dayofweek.map(dict(enumerate(_WEEKDAY_ABBR))),
            ds=lambda d: d["ds"].dt.date.astype(str),
        ).to_dict(orient="records"),
        trend_decomposition=result.trend_decomposition,
    )


def _suggest_baseline_sync(
    request: BaselineSuggestionRequest, df: pd.DataFrame
) -> BaselineSuggestionResponse:
    forecast_config = ForecastConfig(include_statuses=tuple(request.include_statuses))
    engine = TreasuryEngine(forecast_config)
    sending, receiver, agent = _resolve_dimension_filters(request)

    result = engine.suggest_baseline(
        df,
        method=request.method,
        window_days=request.window_days,
        sending_country=sending,
        receiver_country=receiver,
        agent_name=agent,
    )
    return BaselineSuggestionResponse(**result)


def _evaluate_accuracy_sync(request: EvaluationRequest, df: pd.DataFrame) -> EvaluationResponse:
    sending_code = (
        request.holidays.sending_country_code or resolve_country_code(request.sending_country)
    )
    receiver_code = (
        request.holidays.receiver_country_code or resolve_country_code(request.receiver_country)
    )

    forecast_config = ForecastConfig(
        seasonality=SeasonalityConfig(**request.seasonality.model_dump()),
        holidays=HolidayConfig(
            sending_country_code=sending_code,
            receiver_country_code=receiver_code,
            holiday_lower_window=request.holidays.holiday_lower_window,
            holiday_upper_window=request.holidays.holiday_upper_window,
            include_salary_week=request.holidays.include_salary_week,
            salary_week_pre_days=request.holidays.salary_week_pre_days,
            salary_week_post_days=request.holidays.salary_week_post_days,
        ),
        changepoint_prior_scale=request.changepoint_prior_scale,
        interval_width=request.interval_width,
        growth=request.growth,
        seasonality_mode=request.seasonality_mode,
        include_statuses=tuple(request.include_statuses),
    )
    liquidity_config = LiquidityConfig(
        baseline_funding_level=request.baseline_funding_level,
        safety_buffer_pct=request.safety_buffer_pct,
    )

    engine = TreasuryEngine(forecast_config, liquidity_config)
    sending, receiver, agent = _resolve_dimension_filters(request)

    result = engine.evaluate_accuracy(
        df,
        eval_days=request.eval_days,
        sending_country=sending,
        receiver_country=receiver,
        agent_name=agent,
    )

    return EvaluationResponse(
        meta=ForecastMeta(
            dimension=request.dimension,
            sending_country=request.sending_country,
            receiver_country=request.receiver_country,
            agent_name=request.agent_name,
            sending_country_code_resolved=sending_code,
            receiver_country_code_resolved=receiver_code,
            history_days=result["train_days"] + result["eval_days"],
            include_statuses=list(request.include_statuses),
        ),
        **result,
    )


@router.post(
    "/run",
    response_model=ForecastResponse,
    summary="Run a Prophet volume forecast + liquidity-risk simulation",
    operation_id="runForecast",
    responses=_ERROR_RESPONSES,
)
async def run_forecast(
    request: ForecastRequest, repository: DataRepository = Depends(get_repository)
) -> ForecastResponse:
    """Forecast volume for a receiver country, corridor, or agent, and simulate liquidity
    shortfalls against a baseline funding level. See `ForecastRequest` for every
    configurable field; `dimension` picks which of the three the rest of the filters apply
    to.
    """
    started = time.perf_counter()
    logger.info(
        "Forecast request: dimension=%s sending=%s receiver=%s agent=%s horizon=%d",
        request.dimension, request.sending_country, request.receiver_country,
        request.agent_name, request.horizon_days,
    )
    df = await _run_or_raise(repository.load)
    response = await _run_or_raise(_run_engine_sync, request, df)
    logger.info("Forecast request completed in %.2fs", time.perf_counter() - started)
    return response


@router.post(
    "/baseline-suggestion",
    response_model=BaselineSuggestionResponse,
    summary="Suggest a baseline funding level from trailing historical volume",
    operation_id="suggestBaseline",
    responses=_ERROR_RESPONSES,
)
async def suggest_baseline(
    request: BaselineSuggestionRequest, repository: DataRepository = Depends(get_repository)
) -> BaselineSuggestionResponse:
    """A starting point for `ForecastRequest.baseline_funding_level`, not a forecast --
    no Prophet involved. Reuses the same corridor/agent/status filtering
    `POST /run` uses, so "the data this is based on" always matches what the forecast
    itself would see. See `BaselineSuggestionRequest.method` for what each option means.
    """
    logger.info(
        "Baseline suggestion request: dimension=%s sending=%s receiver=%s agent=%s "
        "method=%s window_days=%d",
        request.dimension, request.sending_country, request.receiver_country,
        request.agent_name, request.method, request.window_days,
    )
    df = await _run_or_raise(repository.load)
    return await _run_or_raise(_suggest_baseline_sync, request, df)


@router.post(
    "/evaluate",
    response_model=EvaluationResponse,
    summary="Backtest forecast accuracy via a single train/test holdout",
    operation_id="evaluateForecastAccuracy",
    responses=_ERROR_RESPONSES,
)
async def evaluate_forecast(
    request: EvaluationRequest, repository: DataRepository = Depends(get_repository)
) -> EvaluationResponse:
    """Answers "how much should I trust this forecast" for a given config: hides the most
    recent `eval_days` of real history, fits on everything before that, forecasts the
    hidden window, and compares against what actually happened -- reporting MAPE, RMSE,
    interval coverage, safety-stock coverage, and a naive-baseline comparison. A second
    Prophet fit (roughly the cost of one extra `POST /run`), so this is a separate,
    caller-triggered check rather than something computed on every forecast. See
    `TreasuryEngine.evaluate_accuracy` for the full method and its caveats.
    """
    logger.info(
        "Evaluation request: dimension=%s sending=%s receiver=%s agent=%s eval_days=%d",
        request.dimension, request.sending_country, request.receiver_country,
        request.agent_name, request.eval_days,
    )
    df = await _run_or_raise(repository.load)
    return await _run_or_raise(_evaluate_accuracy_sync, request, df)
