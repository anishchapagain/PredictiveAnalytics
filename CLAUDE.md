# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state of this repository

A git repository since 2026-08-27 (`main` branch, root commit `61b9edd`). `.gitignore`
excludes `Business_Report.csv` (233MB — over GitHub's 100MB hard limit) and `.env`
(replaced by a committed `.env.example`); `.gitattributes` normalizes line endings to LF.
Contents:

- `Business_Report.csv` — ~233 MB raw data export (1,430,655 rows), the underlying dataset.
  **Not tracked in git** (see above) — place it at the repo root manually; nothing else
  about its location or the schema below changes.
- `app/` — the Global Treasury Forecasting & Liquidity Engine: a FastAPI service wrapping a
  Prophet-based volume forecaster + liquidity/gap-analysis simulation, plus a plain HTML/JS
  dashboard at `/dashboard/` (`app/static/dashboard/`). See "Architecture" below and
  `app/README.md` for the full breakdown.
- `eda/` — sweetviz-based EDA report generator and output (see "Working with Business_Report.csv").
- `business_data_send.py` — a Colab-exported prototype (single corridor, AUSTRALIA -> BHUTAN) that
  predates `app/` and is where the "safety stock" / "liquidity-to-volume ratio" / "gap analysis"
  terminology originated. Kept as reference, not modified; `app/` generalizes it. Cross-checked
  against it in `app/README.md`'s "Known caveats" section.
- `notes.txt` — project plan / phasing notes (Phase 1: this API-driven engine, no LLM yet; Phase 2:
  add an agentic/LLM layer on top — see that file for the reasoning and open questions).
- `TODO.md` — the actionable checklist version of the same gaps (tests, git init, CI, auth,
  deployment, etc.), grouped by leverage. `notes.txt` is the narrative/decision log behind each
  item; keep both in sync rather than letting one drift.
- `ISEND_Treasury_Forecasting_Flow.pdf` — the business-process spec `app/`'s forecasting logic
  (holidays, safety stock, gap analysis) is built from.
- `technical.md` — developer-facing technical knowledgebase: system-context/component
  diagrams, request-flow sequence diagrams (`/run`, `/evaluate`, `/baseline-suggestion`, the
  shared error-handling flow), and class diagrams for every layer (`DataRepository` hierarchy,
  `ForecastConfig`/`LiquidityConfig`, `TreasuryEngine`, the Pydantic request/response schemas,
  the `TreasuryEngineError` exception hierarchy), plus a fintech-domain glossary (safety stock,
  liquidity-to-volume ratio, MAPE/RMSE/interval coverage, naive baseline). Complements
  `app/README.md` (which has the reasoning/config/endpoint reference tables) rather than
  duplicating it — read `technical.md` for *system shape*, `app/README.md` for *why*. Keep its
  diagrams current the same way `app/README.md`'s own "Architecture" section is kept current
  when code changes.
- `.env` — runtime config for `app/` (read by `app/core/config.py` via `pydantic-settings`,
  prefix `TREASURY_`): data backend, CSV path override, log level, forecast defaults. Every value
  in it is already the built-in default, shipped for visibility/override, not because anything
  requires it. See `app/README.md`'s "Configuration" table for the field list.
- `logs/` — runtime output, not source: `app/core/logging_config.py`'s rotating file handler
  writes here. One active `app.log` per day; rotates at local midnight to a gzip-compressed
  `app.log.<date>.gz`, with 7 days of those kept before auto-delete (`backupCount`). Not yet
  `.gitignore`d since there's no `.gitignore` at all yet (repo isn't initialized as git) — add
  it when that happens.
- `requirements.txt` — repo-root, covers `app/`'s runtime deps (fastapi, prophet, pandas, etc.).
  `eda/requirements.txt` is separate/smaller (just sweetviz) since that's throwaway analysis work.
  `requirements-dev.txt` adds lint/spec-validation tooling (flake8, ruff, openapi-spec-validator)
  on top of the runtime set -- not needed to run the app, only to develop it.
- `pyproject.toml` / `.flake8` — ruff and flake8 config (line-length 100, deliberately kept in
  sync). See `app/README.md`'s "Code standards" section for the one intentional exception
  (ruff's B008 vs. FastAPI's `Depends(...)` pattern).
- `.venv/` — Python 3.14.6. `ydata-profiling` does **not** support this Python version (no release
  targets <3.14); `prophet`/`fastapi`/etc. do, with prebuilt wheels (no compiler toolchain needed).

If asked to set up further tooling (DB, dashboard, auth), treat those as still-open per `notes.txt`
rather than assuming a choice — check with the user before committing to one.

When code changes, prefer updating this file's "Architecture" section rather than leaving it
stale — the schema notes below are the part most likely to stay useful regardless.

## Working style & standing expectations

Patterns this project has actually followed so far, worth continuing rather than re-deriving
each session:

- **Build incrementally, verify end-to-end before moving on.** This repo's own history is the
  evidence: EDA → forecasting engine → PEP8/OpenAPI/exception-handling hardening, each stage
  actually run against the real CSV (or a live server) before the next started, not just
  unit-tested in isolation. Keep doing that rather than batching a big untested change.
- **Confirm the target location before creating new top-level directories, venvs, or config
  files** rather than defaulting somewhere convenient and relocating later.
- **Documentation is part of the deliverable, not an afterthought.** Every module in `app/` has
  a docstring explaining *why*, not just what; when introducing a modeling/statistical
  technique (this repo's is Prophet — seasonality, holiday effects, the liquidity-to-volume
  ratio), explain what it does and why it was chosen alongside the code, the way
  `app/README.md` already does for the existing engine. Extend that pattern to new modules.
- **The code standards below are standing requirements, not a one-time cleanup pass.** Re-run
  `ruff check app/` / `flake8 app/` after touching `app/`, and give any new endpoint the same
  OpenAPI documentation (`summary`, `responses=`) and exception-hierarchy treatment the existing
  ones have — see "Standards actually enforced" below for the specifics.
- **No test suite exists yet.** Verification so far has been direct (live server + real HTTP
  calls, `openapi-spec-validator`, cross-checks against `business_data_send.py`'s numbers), not
  `pytest`. That's a real gap, not a decision to leave things untested forever — flag it if
  asked about test coverage rather than implying tests exist.

## Architecture

`app/` (FastAPI + Prophet) is `iSendAnalytics`'s Phase 1 deliverable — see `notes.txt` for the
two-phase plan (this is the deterministic, no-LLM platform; Phase 2 adds an agentic layer on top
of this same API later, doesn't replace it).

Request flow: `DataRepository.load()` (CSV today, cached by file mtime; Postgres-shaped interface
for later) → `TreasuryEngine.prepare_data/train_forecast/simulate_liquidity/summarize` (pure
pandas/Prophet, no FastAPI dependency) → `api/routes_forecast.py` wraps it for HTTP, running the
blocking Prophet fit in a thread via `asyncio.to_thread` (Prophet has no native async API).
Forecasts by receiver country, corridor, or agent; sending/receiving-country public holidays
(via `pycountry` + the `holidays` package) plus a synthetic monthly "salary week" event feed into
Prophet. Generalizes a pre-existing single-corridor prototype, `business_data_send.py`
(AUSTRALIA -> BHUTAN) — cross-checked against its reported numbers as a sanity test.

A fourth engine method, `TreasuryEngine.evaluate_accuracy()`, backtests that same config via a
single train/test holdout (hide the last `eval_days` of real history, fit on the rest, forecast
the hidden window, compare against what actually happened) — reports MAPE, RMSE, interval
coverage, safety-stock coverage, and a naive-baseline comparison. Exposed as its own endpoint,
`POST /api/v1/forecast/evaluate` (a second Prophet fit, so a deliberate caller-triggered check,
not part of `/run`), and its own "Evaluate forecast accuracy" card in the dashboard. See
`app/README.md`'s "The engine" and "Dashboard" sections for the full reasoning, including why a
single holdout was chosen over Prophet's own (much more expensive) rolling-origin
`cross_validation()`.

Full module map, request-flow diagram, config reference, and known modeling caveats (forecast
horizon anchors to each filter's own last date not "today"; safety stock looks large on
sparse/spiky corridors; holiday effects are noisy with only ~2.5 years of history) are in
`app/README.md` — read that before changing `app/forecasting/`. For the visual system-context/
component diagrams, per-endpoint sequence diagrams, and class diagrams of every layer
(`DataRepository`, `TreasuryEngine`, the Pydantic schemas, the exception hierarchy), see
`technical.md`.

Run it: `pip install -r requirements.txt && uvicorn app.main:app --reload` (docs at `/docs`,
dashboard at `/dashboard/`). Config comes from `.env` at the repo root; forecast parameters are
also overridable per-request.

A plain HTML/JS dashboard (`app/static/dashboard/index.html`, no build step/bundler) is mounted
at `/dashboard/` -- same-origin static page calling the API above via `fetch()`. Chosen
deliberately over Streamlit when asked; if a richer dashboard is wanted later that's a separate
decision, not an assumption to make silently. Its one real dependency is Plotly.js, self-hosted
(`plotly-cartesian.min.js`, not a CDN) for both charts' zoom/pan/hover — see `app/README.md`'s
"Dashboard" section for why, and for two real bugs Playwright-driven verification caught that no
amount of API-level testing would have (a native HTML5 `step`-validation bug, and info-tooltips
getting clipped by clipping ancestors — fixed with a shared portal, not per-case patches).

**Standards actually enforced (verified, not just claimed):** PEP8 via `ruff check app/` +
`flake8 app/` (both clean; config in `pyproject.toml`/`.flake8`); OpenAPI 3.1.0 schema validated
with `openapi-spec-validator` against the live `/openapi.json`. Exception handling has a
deliberate hierarchy in `app/core/exceptions.py` (`DataSourceError` → 503, `ForecastingError` →
500, plain `ValueError` → 422 for bad caller input) plus a global exception handler and
middleware-level safety net in `app/main.py` — see `app/README.md`'s "Code standards" section
for the full mapping and reasoning before adding a new failure mode without a home for it.

Not yet built (see `notes.txt` for the open decisions behind each): PostgreSQL backend, auth, and
— flagged as a real gap against Phase 1's own design constraints — a caller/actor field in the
request logs.

## Working with `Business_Report.csv`

This file is too large to `Read` in one shot (1.4M+ rows, 233 MB) — it will exceed normal tool
limits. Prefer one of:

- Sampling/streaming from the shell (`head`, `awk`, or a small Python script using `csv.DictReader`/
  `pandas.read_csv(..., chunksize=...)`) rather than loading the whole file into an editor or a
  single `Read` call.
- If doing real analysis, use the `.venv` at the repo root (`.venv\Scripts\python.exe` on Windows)
  and install what's needed (`pandas`, etc.) — nothing is pre-installed beyond pip itself.
- The file has some quoted fields containing embedded commas (e.g. in `Agent_Name`); naive
  comma-splitting (plain `awk -F','`) will misalign columns on those rows. Use a real CSV parser.

### Schema

13 columns, one row per remittance transaction:

| Column | Notes |
| --- | --- |
| `Control_No` | Unique transaction ID, format `IPAY##########`. |
| `TRN_Date` | Transaction timestamp. Observed range: 2024-01-01 through 2026-07-31. |
| `Agent_Name` | Sending agent / exchange house (e.g. "Lotus Foreign Exchange Ltd"). |
| `Transaction_Method` | Specific payout channel/bank/portal (e.g. "Yes Bank", "SAMPATH BANK PLC", "Lightnet Payout"). High cardinality — 178 distinct values across the full file. |
| `Payment_Type` | Broad payout category — 3 values: `Bank Transfer` (~85.8%), `Cash Pay` (~14.2%), `Wallet` (28 rows, negligible). |
| `Sending_Country` / `Sending_Country_Currency` | Origin country and its currency code. 24 / 13 distinct values respectively (full file). |
| `Receiver_Country` / `Payout_Currency` | Destination country and payout currency code. 71 / 35 distinct values respectively (full file). |
| `Transaction_Amount_USD` | Transaction amount normalized to USD. |
| `transstatus` | Status — **11 distinct values in the full file, not just 3.** Dominated by `Payment` (~97.5%) and `Cancel` (~1.6%); the long tail (<1% combined) is `PAYOUTPROCESSING`, `FAILED`, `INITIALIZED`, `RETURNED`, `PAYOUTFAIL`, `Block`, `Compliance`, `OFAC`, `EXPIRED` — worth treating as a richer lifecycle (in-flight / failed / expired / compliance-hold), not a simple 3-state flag. |
| `Paid_Date` | Timestamp the payout was completed. |
| `Turn_Around_Time_Hours` | Hours elapsed between `TRN_Date` and `Paid_Date`. |

Sending side is a small set (24 countries, full-file count); receiving side is much broader (71
countries) — consistent with a remittance business sending money out from a handful of corridors
to many destination markets.

**Note on these figures:** earlier drafts of this doc estimated cardinality from a partial sample
(first ~200k rows) and undercounted across the board (e.g. `transstatus` looked like just 3 values,
`Sending_Country`/`Receiver_Country` looked like ~16/~46+). The counts above are exact, from a
full-file scan — confirmed via a sweetviz EDA report (`eda/generate_eda_report.py`, output at
`eda/Business_Report_EDA.html`). Don't infer cardinality/schema facts from a partial read of this
file again — either scan the whole file or reuse that report.
