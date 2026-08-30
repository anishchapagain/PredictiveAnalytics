"""Application-wide settings.

Loaded from environment variables (prefix ``TREASURY_``) or a ``.env`` file, with sane
defaults so the app runs out of the box against the repo-root CSV. When the data backend
moves to PostgreSQL, add the connection settings here and switch
``Settings.data_backend`` to ``"postgres"`` -- see `app/data/repository.py`.
"""

import logging
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TREASURY_", env_file=".env", extra="ignore")

    # --- Data source ---
    # Literal (rather than plain str) so an invalid TREASURY_DATA_BACKEND fails fast with a
    # clear pydantic ValidationError at process startup, instead of only surfacing as a
    # NotImplementedError on the first request that happens to hit get_repository().
    data_backend: Literal["csv", "postgres"] = "csv"  # "postgres" not yet implemented
    csv_path: Path = REPO_ROOT / "Business_Report.csv"

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Forecasting defaults (overridable per-request via the API) ---
    default_horizon_days: int = 30
    max_horizon_days: int = 365
    default_include_statuses: tuple[str, ...] = ("Payment",)


try:
    settings = Settings()
except Exception:
    # Fail loudly and immediately -- a broken .env should stop the app from starting at
    # all, not surface as a confusing error the first time a request touches Settings.
    logging.getLogger(__name__).exception(
        "Failed to load application settings from .env/environment"
    )
    raise
