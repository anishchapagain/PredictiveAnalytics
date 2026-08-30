"""Builds Prophet-format holiday calendars for a corridor.

Three ingredients get combined into one dataframe passed as `Prophet(holidays=...)`:
  1. Sending-country public holidays (e.g. UAE) -- affects when senders initiate transfers.
  2. Receiving-country public holidays (e.g. Nepal) -- affects payout demand.
  3. A synthetic "salary week" event every month -- remittance volume reliably spikes around
     payday, which public-holiday calendars don't capture.

Caveat worth keeping in mind when reading forecasts: Prophet needs several occurrences of a
holiday to estimate its effect well. With ~2.5 years of history, rare holidays only occur
2-3 times, so their estimated effect will be noisy -- treat holiday effects as directional,
not precise.
"""

from __future__ import annotations

import calendar
import logging

import holidays as holidays_lib
import pandas as pd
import pycountry

from app.forecasting.config import HolidayConfig

logger = logging.getLogger(__name__)

# A handful of dataset country names pycountry's fuzzy search gets wrong or can't resolve
# at all; extend this as new corridors surface bad matches.
_COUNTRY_NAME_OVERRIDES: dict[str, str] = {
    "SOUTH KOREA": "KR",
    "HONG KONG": "HK",
    "MACAU": "MO",
    "RUSSIA": "RU",
    "VIETNAM": "VN",
    "IVORY COAST": "CI",
    "LAOS": "LA",
}


def resolve_country_code(country_name: str | None) -> str | None:
    """Best-effort dataset country name -> ISO 3166-1 alpha-2 code. Returns None (rather
    than raising) when it can't confidently resolve one -- the caller should just skip
    that side's holiday calendar and log a warning, not fail the whole forecast.
    """
    if not country_name or not isinstance(country_name, str):
        return None
    key = country_name.strip().upper()
    if not key:
        return None
    if key in _COUNTRY_NAME_OVERRIDES:
        return _COUNTRY_NAME_OVERRIDES[key]
    try:
        match = pycountry.countries.search_fuzzy(country_name)
        return match[0].alpha_2
    except LookupError:
        logger.warning(
            "Could not resolve country code for %r -- skipping its holidays", country_name
        )
        return None
    except Exception:
        # This function's whole contract is "never raise, worst case skip holidays" --
        # pycountry's fuzzy search does regex work internally that could misbehave on a
        # pathological input we haven't seen; that's still not worth failing the forecast
        # over, but it *is* worth a loud log since it's an unexpected failure mode.
        logger.exception("Unexpected error resolving country code for %r", country_name)
        return None


def _country_holidays_df(
    country_code: str, years: range, label: str, lower_window: int, upper_window: int
) -> pd.DataFrame:
    empty = pd.DataFrame(columns=["holiday", "ds", "lower_window", "upper_window"])
    try:
        calendar_ = holidays_lib.country_holidays(country_code, years=years)
    except NotImplementedError:
        logger.warning("holidays library has no calendar for country code %r", country_code)
        return empty
    except Exception:
        # Same fail-soft contract as resolve_country_code: a holidays-library edge case
        # (e.g. a bad year range) should cost this one side's holidays, not the forecast.
        logger.exception("Unexpected error building holidays for country code %r", country_code)
        return empty

    rows = [
        {
            "holiday": f"{label}_{country_code}",
            "ds": pd.Timestamp(date),
            "lower_window": lower_window,
            "upper_window": upper_window,
        }
        for date in calendar_
    ]
    return pd.DataFrame(rows)


def _salary_week_df(
    start: pd.Timestamp, end: pd.Timestamp, pre_days: int, post_days: int
) -> pd.DataFrame:
    """One event per calendar month in [start, end], pivoted on month-end, extended
    `pre_days` before and `post_days` after -- approximates the end-of-month payroll
    cycle without needing to know each corridor's actual salary calendar.
    """
    months = pd.period_range(start.to_period("M"), end.to_period("M"), freq="M")
    rows = []
    for period in months:
        last_day = calendar.monthrange(period.year, period.month)[1]
        month_end = pd.Timestamp(year=period.year, month=period.month, day=last_day)
        rows.append(
            {
                "holiday": "salary_week",
                "ds": month_end,
                "lower_window": -abs(pre_days),
                "upper_window": abs(post_days),
            }
        )
    return pd.DataFrame(rows)


def build_combined_holidays(
    config: HolidayConfig, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame | None:
    """Returns a Prophet-ready holidays dataframe, or None if there's nothing to add
    (no resolvable country codes and salary week disabled) so the caller can pass
    `holidays=None` and skip that regressor entirely.
    """
    years = range(start.year, end.year + 1)
    frames: list[pd.DataFrame] = []

    if config.sending_country_code:
        frames.append(
            _country_holidays_df(
                config.sending_country_code, years, "sending_holiday",
                config.holiday_lower_window, config.holiday_upper_window,
            )
        )
    if config.receiver_country_code:
        frames.append(
            _country_holidays_df(
                config.receiver_country_code, years, "receiver_holiday",
                config.holiday_lower_window, config.holiday_upper_window,
            )
        )
    if config.include_salary_week:
        frames.append(
            _salary_week_df(start, end, config.salary_week_pre_days, config.salary_week_post_days)
        )

    frames = [f for f in frames if not f.empty]
    if not frames:
        return None

    combined = pd.concat(frames, ignore_index=True)
    # Same holiday name + same date can appear twice if a re-run overlaps ranges; Prophet
    # doesn't need duplicate rows for the same (holiday, ds).
    combined = combined.drop_duplicates(subset=["holiday", "ds"]).reset_index(drop=True)
    return combined
