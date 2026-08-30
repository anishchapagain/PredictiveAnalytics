"""Centralized logging setup.

Called once at app startup (`app/main.py`). Every module gets its logger via
``logging.getLogger(__name__)`` as usual; this just configures the root handlers/format
so log lines are consistent and include enough context (timestamp, level, logger name)
to trace a request through the engine -- to both the console (as before) and, now, to
a rotating file under `logs/`.

Log files: one active file, `logs/app.log`, written to all day. At local midnight,
`TimedRotatingFileHandler` renames it to `logs/app.log.<yesterday's date>` and starts a
fresh empty `app.log` for the new day; `_gzip_rotator`/`_gzip_namer` below make it
compress that renamed file to `.gz` right away, so only today's log is ever
uncompressed on disk. Rotation is checked lazily on each log call (stdlib behavior,
not a background timer), so it actually happens on the first log line written *after*
midnight, not necessarily at 00:00:00 sharp on an idle server.
"""

import gzip
import logging
import logging.handlers
import shutil
from pathlib import Path

from app.core.config import REPO_ROOT, settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

LOGS_DIR = REPO_ROOT / "logs"

# How many days of compressed logs to keep before the handler deletes the oldest
# automatically -- confirmed with the user (2026-08-26): 7 days. Pass backup_count=
# to setup_logging() to override.
_DEFAULT_BACKUP_COUNT = 7


def _gzip_rotator(source: str, dest: str) -> None:
    """`TimedRotatingFileHandler` calls this once it has renamed the file being
    rotated out to its dated name (`source`) and picked the dated `.gz` name it wants
    for the compressed version (`dest`, from `_gzip_namer` below) -- compress and
    remove the uncompressed original, so yesterday's log never lingers unzipped.
    """
    with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    Path(source).unlink()


def _gzip_namer(default_name: str) -> str:
    """`TimedRotatingFileHandler`'s own rotated name (e.g. `app.log.2026-08-25`) with
    `.gz` appended, since `_gzip_rotator` above always produces a gzip file there.
    Confirmed on this project's Python version that `backupCount`-based auto-delete
    still correctly recognizes and cleans up files renamed this way (a known gotcha
    on some older Python versions -- checked empirically before relying on it).
    """
    return default_name + ".gz"


def setup_logging(level: str | None = None, backup_count: int = _DEFAULT_BACKUP_COUNT) -> None:
    resolved_level = (level or settings.log_level).upper()
    formatter = logging.Formatter(_LOG_FORMAT)

    root = logging.getLogger()
    root.setLevel(resolved_level)
    # Idempotent: if setup_logging() is ever called more than once in the same process
    # (e.g. under a test runner), start from a clean slate rather than stacking a
    # second copy of every handler and duplicating every log line.
    root.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=LOGS_DIR / "app.log",
        when="midnight",
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.rotator = _gzip_rotator
    file_handler.namer = _gzip_namer
    root.addHandler(file_handler)

    # Quiet down noisy third-party loggers unless we're debugging.
    if resolved_level != "DEBUG":
        logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
        logging.getLogger("prophet").setLevel(logging.WARNING)
        logging.getLogger("matplotlib").setLevel(logging.WARNING)
