"""Metadata endpoints -- lets a dashboard populate its filter dropdowns (sending/receiver
countries, agents) without hardcoding anything, and stays correct as new corridors/agents
show up in the data over time.
"""

from __future__ import annotations

import asyncio
import logging

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_repository
from app.api.schemas import DatasetDistinctCounts, DatasetSummary, DateRange, ErrorResponse
from app.core.exceptions import DataSourceError
from app.data.repository import DataRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/meta", tags=["meta"])

_ERROR_RESPONSES = {
    500: {"model": ErrorResponse, "description": "Unexpected internal error"},
    503: {"model": ErrorResponse, "description": "Data source unavailable/unreadable"},
}


async def _load_or_raise(repository: DataRepository) -> pd.DataFrame:
    """Shared error handling for every meta endpoint below -- all of them do nothing but
    load the data and summarize one column, so the failure modes (and how to respond to
    them) are identical; this avoids repeating the same try/except five times.
    """
    try:
        return await asyncio.to_thread(repository.load)
    except DataSourceError as exc:
        logger.error("Meta endpoint failed -- data source unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Data source unavailable") from exc
    except Exception:
        logger.exception("Unhandled error loading data for a meta endpoint")
        raise HTTPException(status_code=500, detail="Internal server error") from None


@router.get(
    "/sending-countries",
    summary="Distinct Sending_Country values in the data",
    operation_id="getSendingCountries",
    responses=_ERROR_RESPONSES,
)
async def sending_countries(repository: DataRepository = Depends(get_repository)) -> list[str]:
    """Every distinct `Sending_Country` value present -- populates the dashboard's Sending
    country dropdown, and is the valid-value set for `sending_country` on both forecast
    endpoints.
    """
    df = await _load_or_raise(repository)
    return sorted(df["Sending_Country"].dropna().unique().tolist())


@router.get(
    "/receiver-countries",
    summary="Distinct Receiver_Country values in the data",
    operation_id="getReceiverCountries",
    responses=_ERROR_RESPONSES,
)
async def receiver_countries(repository: DataRepository = Depends(get_repository)) -> list[str]:
    """Every distinct `Receiver_Country` value present -- populates the dashboard's Receiver
    country dropdown, and is the valid-value set for `receiver_country` on both forecast
    endpoints.
    """
    df = await _load_or_raise(repository)
    return sorted(df["Receiver_Country"].dropna().unique().tolist())


@router.get(
    "/agents",
    summary="Distinct Agent_Name values in the data",
    operation_id="getAgents",
    responses=_ERROR_RESPONSES,
)
async def agents(repository: DataRepository = Depends(get_repository)) -> list[str]:
    """Every distinct `Agent_Name` value present -- populates the dashboard's Agent dropdown,
    and is the valid-value set for `agent_name` on both forecast endpoints.
    """
    df = await _load_or_raise(repository)
    return sorted(df["Agent_Name"].dropna().unique().tolist())


@router.get(
    "/corridors",
    summary="Distinct (sending, receiver) country pairs actually observed",
    operation_id="getCorridors",
    responses=_ERROR_RESPONSES,
)
async def corridors(repository: DataRepository = Depends(get_repository)) -> list[dict[str, str]]:
    """Distinct (Sending_Country, Receiver_Country) pairs actually observed in the data --
    lets a dashboard offer only real corridors rather than the full cross-product (most
    sending/receiver combinations never actually occur).
    """
    df = await _load_or_raise(repository)
    pairs = df[["Sending_Country", "Receiver_Country"]].drop_duplicates().sort_values(
        ["Sending_Country", "Receiver_Country"]
    )
    return [
        {"sending_country": row.Sending_Country, "receiver_country": row.Receiver_Country}
        for row in pairs.itertuples()
    ]


@router.get(
    "/statuses",
    summary="Distinct transstatus values in the data",
    operation_id="getStatuses",
    responses=_ERROR_RESPONSES,
)
async def statuses(repository: DataRepository = Depends(get_repository)) -> list[str]:
    """Every distinct `transstatus` value present -- 11 in the full dataset (see CLAUDE.md),
    not just the 3 a quick sample would suggest. Populates the dashboard's "statuses counted"
    checkboxes, and is the valid-value set for `include_statuses` on both forecast endpoints.
    """
    df = await _load_or_raise(repository)
    return sorted(df["transstatus"].dropna().unique().tolist())


@router.get(
    "/summary",
    response_model=DatasetSummary,
    summary="Dataset-wide overview: row count, date range, and key column cardinalities",
    operation_id="getDatasetSummary",
    responses=_ERROR_RESPONSES,
)
async def dataset_summary(repository: DataRepository = Depends(get_repository)) -> DatasetSummary:
    """A live snapshot of the underlying data's actual shape -- how many rows, what date
    range they span, and how many distinct values each key dimension column has. Lets a
    caller (or a person) confirm what's actually loaded right now without re-running a full
    EDA report or reading static documentation that can drift out of date.
    """
    df = await _load_or_raise(repository)
    return DatasetSummary(
        total_rows=len(df),
        date_range=DateRange(
            start=df["TRN_Date"].min().date().isoformat(),
            end=df["TRN_Date"].max().date().isoformat(),
        ),
        distinct_counts=DatasetDistinctCounts(
            sending_countries=int(df["Sending_Country"].nunique()),
            receiver_countries=int(df["Receiver_Country"].nunique()),
            agents=int(df["Agent_Name"].nunique()),
            transaction_methods=int(df["Transaction_Method"].nunique()),
            statuses=int(df["transstatus"].nunique()),
            sending_currencies=int(df["Sending_Country_Currency"].nunique()),
            payout_currencies=int(df["Payout_Currency"].nunique()),
        ),
    )
