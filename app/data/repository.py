"""Data access layer.

`DataRepository` is the only contract the rest of the app (the forecasting engine, the API
routes) depends on: ``load() -> pd.DataFrame`` with the columns in `schema.REQUIRED_COLUMNS`.
Today that's `CSVDataRepository` reading `Business_Report.csv`. When the project moves to
PostgreSQL (see notes.txt -- DB choice was still open as of this writing), add a
`PostgresDataRepository` implementing the same contract and swap it in via
`get_repository()` / `Settings.data_backend` -- nothing above this layer needs to change.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd

from app.core.exceptions import DataSourceError
from app.data.schema import DATE_COLUMNS, DTYPES, REQUIRED_COLUMNS

logger = logging.getLogger(__name__)


class DataRepository(ABC):
    """Contract every data backend must satisfy."""

    @abstractmethod
    def load(self) -> pd.DataFrame:
        """Return the full transactions table."""
        raise NotImplementedError


class CSVDataRepository(DataRepository):
    """Reads `Business_Report.csv`, caching in memory and reloading only when the file's
    mtime changes -- so appending new daily rows to the CSV is picked up on the next
    request without restarting the service, without re-parsing the 233 MB file on every
    single call either.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._cache: pd.DataFrame | None = None
        self._cached_mtime: float | None = None

    def load(self) -> pd.DataFrame:
        try:
            exists = self.path.exists()
            mtime = self.path.stat().st_mtime if exists else None
        except OSError as exc:
            # e.g. permission denied, a network-mounted path that's gone offline -- not
            # "file doesn't exist", but still can't be read.
            logger.error("Could not stat data file %s: %s", self.path, exc)
            raise DataSourceError(f"Could not access data file: {self.path}") from exc

        if not exists:
            logger.error("Data file not found: %s", self.path)
            raise DataSourceError(f"Data file not found: {self.path}")

        if self._cache is not None and mtime == self._cached_mtime:
            return self._cache

        logger.info("Loading data from %s (cache miss or file changed)", self.path)
        try:
            df = pd.read_csv(self.path, dtype=DTYPES, parse_dates=DATE_COLUMNS)
        except pd.errors.EmptyDataError as exc:
            raise DataSourceError(f"Data file is empty: {self.path}") from exc
        except (pd.errors.ParserError, UnicodeDecodeError) as exc:
            raise DataSourceError(f"Data file is malformed/unreadable: {self.path}") from exc
        except OSError as exc:
            raise DataSourceError(f"Could not read data file: {self.path}") from exc

        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            # Deliberately NOT cached -- a schema mismatch shouldn't get "fixed" by a
            # subsequent successful load silently masking it; every call keeps failing
            # loudly until the file itself is corrected.
            raise DataSourceError(f"CSV is missing required columns: {missing}")

        # Only commit to the cache once the read+validation has fully succeeded, so a
        # transient failure (e.g. read during an in-progress write) leaves the previous
        # good cache in place for other requests, and this same file is simply retried
        # (mtime still won't match _cached_mtime) on the next call.
        self._cache = df
        self._cached_mtime = mtime
        logger.info("Loaded %s rows from %s", f"{len(df):,}", self.path.name)
        return self._cache


class PostgresDataRepository(DataRepository):
    """Placeholder. DB schema/connection details aren't decided yet (see notes.txt open
    questions) -- building this against a nonexistent database would just be guesswork.
    The contract is already fixed by `DataRepository.load()`; once Postgres is chosen,
    implement this with e.g. `asyncpg`/`SQLAlchemy` and swap `Settings.data_backend`.
    """

    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "PostgresDataRepository is not implemented yet -- DB choice is still an open "
            "question (see notes.txt). Use CSVDataRepository for now."
        )

    def load(self) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError
