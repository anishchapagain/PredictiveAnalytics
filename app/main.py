"""FastAPI entrypoint.

Run with:  uvicorn app.main:app --reload
Docs at:   http://127.0.0.1:8000/docs
Dashboard: http://127.0.0.1:8000/dashboard/
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.deps import get_repository
from app.api.routes_forecast import router as forecast_router
from app.api.routes_meta import router as meta_router
from app.api.schemas import ErrorResponse, HealthResponse, ReadyResponse, RootResponse
from app.core.config import settings
from app.core.exceptions import DataSourceError
from app.core.logging_config import setup_logging
from app.data.repository import DataRepository

# Resolved from this file's own location (not the process cwd) -- same reasoning as
# Settings.csv_path: correct regardless of where `uvicorn` is launched from.
_DASHBOARD_DIR = Path(__file__).resolve().parent / "static" / "dashboard"

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Global Treasury Forecasting & Liquidity Engine",
    summary="Prophet-based volume forecasting + liquidity-risk simulation -- Phase 1 of "
    "PredictiveAnalytics.",
    description=(
        "Prophet-based volume forecasting and liquidity-risk simulation for remittance "
        "corridors, receiving countries, and agents. Deterministic, API-driven (no LLM/agent "
        "layer yet -- see notes.txt for the two-phase plan). No authentication is configured "
        "in this phase; do not expose this service directly to an untrusted network.\n\n"
        "A plain HTML/JS dashboard for the Treasury Management team is served at "
        "`/dashboard/`, calling the exact same API documented here."
    ),
    version="0.1.0",
    openapi_tags=[
        {"name": "system", "description": "Liveness/readiness and API orientation -- no data."},
        {"name": "forecast", "description": "Prophet forecast + liquidity-risk simulation."},
        {"name": "meta", "description": "Distinct filter values for dashboards/agents to query."},
    ],
    servers=[
        {
            "url": "/",
            "description": "This same host -- API and dashboard served together, same-origin.",
        }
    ],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Logs every request/response pair with method, path, status code, and duration --
    the one place this app gets that for every route at once, rather than each route
    logging its own timing. An exception that escapes a route's own try/except is
    logged here too, with the same method/path/duration context, before re-raising for
    the global handler below to turn into a response.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # Defense in depth: every route already catches its own exceptions (see
        # routes_forecast.py/routes_meta.py), so reaching here means something outside
        # a route body itself failed (a bug in a dependency, in Starlette, etc.). Log it
        # with full context and duration rather than letting it vanish with no timing/
        # request-path info, then hand off to the global handler below for the response.
        duration_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "%s %s -> unhandled exception (%.1fms)", request.method, request.url.path, duration_ms
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "%s %s -> %d (%.1fms)", request.method, request.url.path, response.status_code, duration_ms
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort safety net so a truly unanticipated error still returns a clean,
    consistent JSON error body (matching `ErrorResponse`/every other error response)
    instead of an ASGI-server-default plaintext/HTML response, with the full traceback
    captured server-side either way.
    """
    logger.exception("Unhandled exception for %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


app.include_router(forecast_router)
app.include_router(meta_router)

# Plain HTML/JS dashboard -- a static page calling the API above via fetch(), served
# same-origin so there's no CORS to configure. See app/static/dashboard/index.html.
app.mount("/dashboard", StaticFiles(directory=_DASHBOARD_DIR, html=True), name="dashboard")


@app.get(
    "/",
    response_model=RootResponse,
    tags=["system"],
    summary="API orientation -- links to docs, dashboard, and health/readiness",
    operation_id="getRoot",
)
async def root() -> RootResponse:
    """This service has no landing page of its own -- just enough to point a caller (or a
    person with a browser) at `/docs`, `/dashboard/`, and the liveness/readiness checks."""
    return RootResponse(
        name=app.title,
        version=app.version,
        docs_url="/docs",
        redoc_url="/redoc",
        dashboard_url="/dashboard/",
        health_url="/health",
        ready_url="/ready",
    )


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Liveness check",
    operation_id="getHealth",
)
async def health() -> HealthResponse:
    """Confirms only that the process is up and answering HTTP -- deliberately never
    touches the data source (see GET /ready for that), so it stays fast and meaningful even
    while the data source itself is unavailable."""
    return HealthResponse(status="ok")


@app.get(
    "/ready",
    response_model=ReadyResponse,
    tags=["system"],
    summary="Readiness check -- confirms the configured data source actually loads",
    operation_id="getReady",
    responses={503: {"model": ErrorResponse, "description": "Data source unavailable/unreadable"}},
)
async def ready(repository: DataRepository = Depends(get_repository)) -> ReadyResponse:
    """Unlike GET /health, this actually attempts to load the configured data source --
    a 200 here means a forecast/meta request would currently succeed at the data-loading
    step; a 503 means the same failure every other endpoint would also hit right now.
    """
    try:
        df = await asyncio.to_thread(repository.load)
    except DataSourceError as exc:
        logger.error("Readiness check failed -- data source unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Data source unavailable") from exc
    return ReadyResponse(status="ok", data_backend=settings.data_backend, rows_loaded=len(df))
