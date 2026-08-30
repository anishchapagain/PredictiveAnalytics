# AGENTS.md

Instructions for AI coding agents working in this repository, following the
[agents.md](https://agents.md) convention (tool-agnostic; Claude Code reads `CLAUDE.md`
specifically, which carries the same core guidance in more depth). **Keep this file and
`CLAUDE.md` in sync** when either changes — they should never contradict each other.

## What this is

`iSendAnalytics` — Phase 1 of a two-phase plan (see `notes.txt`): a deterministic, API-driven
treasury forecasting/liquidity platform, no LLM/agent involved yet. `app/` is a FastAPI service
wrapping a Prophet-based volume forecaster + liquidity-risk simulation for remittance corridors,
receiving countries, and agents. Phase 2 (later, not started) adds an agentic/LLM layer as a
*client* of this same API, not a replacement for it.

A git repository since 2026-08-27 (`main` branch). `Business_Report.csv` (233MB) and `.env`
are gitignored -- see `.gitignore`/`.env.example`.

## Setup

```bash
pip install -r requirements.txt        # runtime deps (fastapi, prophet, pandas, etc.)
pip install -r requirements-dev.txt    # + lint/spec-validation tooling, for development only
```

`.env` at the repo root already has every config value at its built-in default (see
`app/README.md`'s "Configuration" table) — nothing needs editing to run.

## Run

```bash
uvicorn app.main:app --reload
```

Dashboard: <http://127.0.0.1:8000/dashboard/> · Interactive docs: <http://127.0.0.1:8000/docs> ·
Health check: <http://127.0.0.1:8000/health>

## Lint

```bash
ruff check app/
flake8 app/
```

Both must pass clean (config in `pyproject.toml` / `.flake8`, line-length 100, kept in sync).
No `pytest` suite exists yet — verification so far has been direct (live server + real HTTP
calls, `openapi-spec-validator` against `/openapi.json`) rather than automated tests; say so if
asked about test coverage rather than implying otherwise.

## Project layout

```text
Business_Report.csv   233 MB raw dataset (1,430,655 rows) -- see CLAUDE.md for the full schema
app/                   FastAPI + Prophet engine -- see app/README.md for the full architecture
eda/                   sweetviz EDA report + generator
business_data_send.py  pre-existing single-corridor prototype app/ generalizes (reference only)
notes.txt              phasing plan, decisions made, open questions
.env                   runtime config (pydantic-settings, prefix TREASURY_)
```

## Standing expectations

- Build incrementally and verify end-to-end (real data, a live server) before moving to the
  next piece — don't batch a large untested change.
- Confirm the target location before creating new top-level directories/files rather than
  defaulting somewhere and relocating later.
- Document *why*, not just what — every module has a docstring; a new modeling/statistical
  technique gets a short plain-language explanation alongside the code (see how `app/README.md`
  explains Prophet's seasonality/holiday config).
- Treat the code standards as standing, not a one-off cleanup: PEP8-clean (`ruff`/`flake8`),
  every FastAPI endpoint has OpenAPI `summary`/`responses=` documentation, and failures go
  through the exception hierarchy in `app/core/exceptions.py` (`DataSourceError` → 503,
  `ForecastingError` → 500, `ValueError` → 422) rather than an unhandled/undocumented failure.
- DB backend, auth, and deployment target are still open decisions (`notes.txt`) — don't assume
  a choice on any of them; check first. A basic dashboard exists (plain HTML/JS, see below); a
  richer one (e.g. Streamlit) is still a separate, open decision if ever wanted.
- **Verify by actually driving the thing**, not just testing the API/units in isolation — a real
  bug here (native HTML5 validation silently blocking form submission) only surfaced by
  Playwright-driving the dashboard in a real browser, not from API-level tests passing clean.

## Where to look for more detail

- `CLAUDE.md` — full repo context, dataset schema (verified full-file cardinality, not a
  partial sample), and the reasoning behind each of the points above.
- `app/README.md` — the engine's full architecture, request flow, configuration reference,
  known modeling caveats, and the code-standards mapping in detail.
- `notes.txt` — the two-phase plan, what's been decided, and what's still open.
