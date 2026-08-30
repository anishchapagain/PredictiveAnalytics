"""FastAPI dependency providers."""

import logging
from functools import lru_cache

from fastapi import HTTPException

from app.core.config import settings
from app.data.repository import CSVDataRepository, DataRepository, PostgresDataRepository

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_repository() -> DataRepository:
    """One repository instance for the process lifetime. The CSV repository itself
    handles picking up new data via its own mtime check on every `load()` -- this cache
    is just about not re-instantiating the object, not about caching data.

    Runs as a FastAPI dependency *before* a route's own try/except body, so failures here
    must be turned into an `HTTPException` directly (FastAPI supports raising it from a
    dependency, same as from the route itself) rather than a plain exception that would
    otherwise escape as an unlogged, unhandled 500.
    """
    if settings.data_backend == "csv":
        return CSVDataRepository(settings.csv_path)

    # settings.data_backend is a Literal["csv", "postgres"], so this is the only other
    # reachable value -- a validly configured but not-yet-implemented backend.
    try:
        return PostgresDataRepository()
    except NotImplementedError as exc:
        logger.error("data_backend='postgres' is configured but not implemented: %s", exc)
        raise HTTPException(
            status_code=503, detail="Configured data backend is unavailable"
        ) from exc
