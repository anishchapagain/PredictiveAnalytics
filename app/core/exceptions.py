"""App-specific exception types.

Distinguishing these from bare `ValueError`/third-party exceptions lets the API layer map
failures to the right HTTP status/response consistently, instead of every route re-deciding
what a raw `KeyError` or Prophet internal error should mean to a caller.

`ValueError` is deliberately NOT redefined here: `TreasuryEngine.prepare_data` and
`ForecastRequest`'s validators already raise plain `ValueError` for bad *caller input*
(unknown corridor, missing required field for a dimension) -- that maps to 422 and is fine
as the standard, built-in exception for "the input was invalid."
"""

from __future__ import annotations


class TreasuryEngineError(Exception):
    """Base class for this app's own exceptions (as opposed to a third-party library
    error bubbling up unhandled)."""


class DataSourceError(TreasuryEngineError):
    """The configured data source (CSV file today, a DB later) could not be read --
    missing file, unreadable/corrupt file, unexpected schema. An infrastructure/data
    problem, not the caller's fault -- maps to 503, not 422.
    """


class ForecastingError(TreasuryEngineError):
    """Prophet (or the surrounding engine logic) failed to produce a forecast for a
    reason that isn't the caller's filters being wrong (that's `ValueError` from
    `prepare_data`) -- e.g. the model failed to fit/converge. Maps to 500.
    """
