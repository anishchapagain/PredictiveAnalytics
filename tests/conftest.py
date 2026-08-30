"""Shared pytest fixtures.

The one deliberate design decision here: tests never touch the real
Business_Report.csv (233MB, gitignored -- see .gitignore/CLAUDE.md). A fresh clone or a
CI runner won't have that file at all, so depending on it would make the suite pass only
on this machine. Instead, `fixture_df` builds a small, fully deterministic synthetic
dataset shaped like the real one (same columns/dtypes `TreasuryEngine`/the API routes
actually touch), and `client`/`app_test_client` wire it in via FastAPI's own
`dependency_overrides` on `get_repository` -- the app under test never knows the
difference.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_repository
from app.data.repository import DataRepository
from app.main import app

# --- Fixture dataset shape -------------------------------------------------------
# 120 consecutive days (fixed calendar dates, not relative to "today" -- so a test run
# next year still sees exactly the same data), two sending countries into one receiving
# country (covers both receiver_country and corridor dimension tests from the same
# fixture), two agents, and a fixed, seeded mild weekly wave in the amounts so Prophet
# has *something* non-trivial to fit without the numbers being pure noise.
#
# Real country names deliberately (not "TESTLANDIA"-style placeholders): holiday
# resolution (resolve_country_code(), via pycountry's fuzzy search) genuinely needs a
# name it can match to something real, or every dimension would resolve to the same
# "no match found" None and the by-dimension holiday-resolution behavior (see
# app/README.md / the user manual's Section 8) couldn't be distinguished from a broken
# lookup in a test. Reuses the same corridor business_data_send.py (the pre-app/
# prototype) was built around, so it's a real, already-known-good corridor.
FIXTURE_DAYS = 120
FIXTURE_START = "2024-01-01"
SENDING_A = "AUSTRALIA"
SENDING_B = "UNITED ARAB EMIRATES"
RECEIVER = "NEPAL"
AGENT_A = "TEST AGENT ONE"
AGENT_B = "TEST AGENT TWO"


class FakeRepository(DataRepository):
    """`DataRepository.load()` returning a fixed in-memory frame -- the test
    equivalent of `CSVDataRepository`, with no file I/O and no caching to worry about.
    """

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def load(self) -> pd.DataFrame:
        return self._df.copy()


def _build_fixture_df() -> pd.DataFrame:
    rng = np.random.default_rng(42)  # fixed seed -- deterministic across runs
    dates = pd.date_range(FIXTURE_START, periods=FIXTURE_DAYS, freq="D")

    rows = []
    for i, d in enumerate(dates):
        # Corridor A: SENDLANDIA -> TESTLAND. The bulk of the data, real trend + weekly
        # wave + small deterministic noise so it's not perfectly linear/flat.
        base = 1000 + i * 2  # mild upward trend
        weekly = 150 * np.sin(2 * np.pi * d.dayofweek / 7)
        noise = rng.normal(0, 20)
        amount_a = max(round(base + weekly + noise, 2), 10.0)
        # ~1 in 10 days include a non-Payment status row too, at a fixed cadence (not
        # random) so status-filtering tests have an exact, predictable count to assert.
        status_a = "Cancel" if i % 10 == 0 else "Payment"
        rows.append({
            "Control_No": f"IPAY{i:010d}A",
            "TRN_Date": d,
            "Agent_Name": AGENT_A,
            "Transaction_Method": "Test Bank",
            "Payment_Type": "Bank Transfer",
            "Sending_Country": SENDING_A,
            "Sending_Country_Currency": "USD",
            "Receiver_Country": RECEIVER,
            "Payout_Currency": "TST",
            "Transaction_Amount_USD": amount_a,
            "transstatus": status_a,
            "Paid_Date": d + pd.Timedelta(hours=6),
            "Turn_Around_Time_Hours": 6.0,
        })

        # Corridor B: OTHERLANDIA -> TESTLAND. A thinner, separate corridor -- lets
        # corridor-dimension tests isolate one sending country and lets
        # receiver_country-dimension tests exercise real cross-corridor aggregation.
        weekly_b = 5 * np.sin(2 * np.pi * d.dayofweek / 7)
        amount_b = max(round(300 + weekly_b + rng.normal(0, 8), 2), 5.0)
        rows.append({
            "Control_No": f"IPAY{i:010d}B",
            "TRN_Date": d,
            "Agent_Name": AGENT_B,
            "Transaction_Method": "Test Wallet",
            "Payment_Type": "Wallet",
            "Sending_Country": SENDING_B,
            "Sending_Country_Currency": "EUR",
            "Receiver_Country": RECEIVER,
            "Payout_Currency": "TST",
            "Transaction_Amount_USD": amount_b,
            "transstatus": "Payment",
            "Paid_Date": d + pd.Timedelta(hours=3),
            "Turn_Around_Time_Hours": 3.0,
        })

    df = pd.DataFrame(rows)
    df["TRN_Date"] = pd.to_datetime(df["TRN_Date"])
    df["Paid_Date"] = pd.to_datetime(df["Paid_Date"])
    for col in ("Payment_Type", "Sending_Country", "Sending_Country_Currency",
                "Receiver_Country", "Payout_Currency", "transstatus"):
        df[col] = df[col].astype("category")
    return df


@pytest.fixture(scope="session")
def fixture_df() -> pd.DataFrame:
    """The synthetic dataset itself, for tests that want to assert against it directly
    (e.g. exact counts) without going through the API/engine at all."""
    return _build_fixture_df()


@pytest.fixture
def client(fixture_df) -> TestClient:
    """A TestClient wired to the fixture dataset instead of the real CSV, via FastAPI's
    dependency_overrides -- restored after each test so tests can't leak overrides into
    each other."""
    app.dependency_overrides[get_repository] = lambda: FakeRepository(fixture_df)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
