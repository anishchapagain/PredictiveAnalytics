# Global Treasury Forecasting & Liquidity Engine

Forecasts cross-border remittance volume and turns it into a daily treasury prefunding plan.

For a payout corridor, receiving country or agent, it answers the question treasury actually
has to act on: **how much cash should we have positioned tomorrow, and where does our current
funding fall short?** A Prophet model forecasts expected daily volume; a liquidity simulation
converts that into risk-adjusted safety stock, a day-by-day funding shortfall against a planned
baseline, and a liquidity-to-volume ratio that lets a $2M/day corridor and a $20K/day one be
compared on the same footing.

A FastAPI service, a plain HTML/JS dashboard, and a Python engine callable directly from a
notebook or a test. No LLM anywhere in it.

---

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

| | |
| --- | --- |
| Dashboard | <http://127.0.0.1:8000/dashboard/> |
| Interactive API docs | <http://127.0.0.1:8000/docs> |
| Liveness / readiness | <http://127.0.0.1:8000/health> &middot; <http://127.0.0.1:8000/ready> |

**You must supply the dataset.** `Business_Report.csv` (~233 MB, 1.4M transactions) is not in
this repository &mdash; it exceeds GitHub's file-size limit and does not belong in git history.
Place it at the repository root, or point `TREASURY_CSV_PATH` at it. The schema is documented in
[`CLAUDE.md`](CLAUDE.md).

> The first request against a cold process parses the whole CSV and takes roughly **35 seconds**.
> `/ready` touches the data source too, so give any readiness probe a timeout well above that or
> warm the cache at startup &mdash; otherwise an orchestrator will kill the container before it
> ever reports ready. Subsequent requests are served from an mtime-keyed cache.

### One request

```bash
curl -X POST http://127.0.0.1:8000/api/v1/forecast/run \
  -H "Content-Type: application/json" \
  -d '{"dimension": "receiver_country",
       "receiver_country": "NEPAL",
       "horizon_days": 10,
       "baseline_funding_level": 50000}'
```

Returns per-day `yhat` with its uncertainty band, `safety_stock`, `daily_shortfall`,
`cumulative_shortfall` and `liquidity_to_volume_ratio`, plus a headline summary (total
shortfall, peak demand day, days short) and an optional Plotly trend decomposition.

---

## What it does

**Three reporting dimensions.** Forecast by `receiver_country` (all corridors into one market),
`corridor` (one sending &rarr; receiving pair), or `agent` (one payout partner across every
market it serves).

**Domain-aware seasonality.** Beyond weekly and yearly patterns, the model is fed public
holidays for *both* the sending and receiving country &mdash; they move volume for different
reasons, one shifting when senders initiate and the other when payouts can clear &mdash; plus a
synthetic monthly salary-week event, because remittance volume spikes around payday and no
public-holiday calendar captures that.

**A funding plan, not just a forecast.** `safety_stock` is the upper edge of the model's own
uncertainty band plus an optional buffer: the amount that must actually be available to avoid a
settlement failure, which is a different number from expected volume and the one treasury funds
against.

**An accuracy check built in.** `POST /api/v1/forecast/evaluate` hides recent history, refits,
and scores the forecast against what really happened &mdash; reporting MAPE, RMSE, interval
coverage, safety-stock coverage and a naive-baseline comparison, so a user can ask *"should I
trust this?"* from the product rather than taking it on faith.

**An optional second opinion.** `include_lightgbm: true` adds a gradient-boosting forecast built
on ~61 engineered lag, rolling and calendar features, as an extra comparison column. It is
deliberately never authoritative &mdash; see below.

---

## Prophet is the funding basis; LightGBM is a comparison

Enabling LightGBM adds a `yhat_lightgbm` column and changes nothing else. Every funding number
&mdash; `safety_stock`, `daily_shortfall`, `cumulative_shortfall`, the whole summary &mdash;
stays Prophet-derived, and the test suite asserts that turning it on leaves them bit-identical.

That split is measured, not caution for its own sake. Across 36 rolling-origin backtests
spanning 12 entities and all three dimensions:

- a LightGBM **quantile band** covered 90.3% of held-out days against Prophet's 96.8% at the
  same nominal width &mdash; it would under-fund roughly one day in ten, the one failure a
  prefunding plan cannot absorb;
- on **point accuracy** LightGBM won only 10 of 24 head-to-head runs, and its errors are
  heavy-tailed rather than uniformly worse. On `HONG KONG -> INDIA` it posted 376&ndash;463%
  WAPE against Prophet's 142&ndash;151%, consistently across every window tested.

Read a large divergence between the two columns as *"this corridor is uncertain, go run the
backtest"* &mdash; not as a reason to prefer either number.

---

## Layout

```text
app/
  api/           FastAPI routes, Pydantic request/response schemas, DI
  forecasting/   Prophet engine, liquidity simulation, holidays, LightGBM, features
  data/          DataRepository -- CSV today, database-shaped for later
  core/          Settings, logging, the exception hierarchy
  static/        The dashboard: plain HTML/JS, no build step, self-hosted Plotly
tests/           68 pytest tests over a synthetic fixture -- CI needs no dataset
eda/             sweetviz profiling generator (its HTML output is not committed)
docs/            User manual, architecture notes, worked walkthrough
```

Each layer depends only on the one beneath it: `api` &rarr; `forecasting` &rarr; `data` &rarr;
`core`. The forecasting engine imports no FastAPI at all and takes a plain DataFrame, which is
what lets it be tested without a server and repointed at a database later without touching a
route.

---

## Documentation

| Read this | For |
| --- | --- |
| [`app/README.md`](app/README.md) | Why each design decision was made, the config reference, the endpoint table, and the honest list of known caveats |
| [`technical.md`](technical.md) | System-context, request-flow and class diagrams, plus a fully worked forecast example with real numbers |
| [`docs/walkthrough.md`](docs/walkthrough.md) | An end-to-end walkthrough of the feature-engineering and evaluation work |
| [`CLAUDE.md`](CLAUDE.md) | Repository conventions and the `Business_Report.csv` schema |
| [`notes.txt`](notes.txt) / [`TODO.md`](TODO.md) | The two-phase plan and the decisions still open |

---

## Development

```bash
pip install -r requirements-dev.txt

pytest tests/ -q          # 68 tests, synthetic fixture, no dataset required
ruff check app/ tests/
flake8 app/ tests/
```

CI runs all three on every push ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

**Configuration** comes from a `.env` file at the repository root, prefix `TREASURY_` &mdash;
see [`.env.example`](.env.example) for every field. Every forecast setting is also overridable
per request, so the environment sets operational defaults rather than hard limits.

---

## Not in this repository

Kept local by design, enforced through `.gitignore`:

| | |
| --- | --- |
| `Business_Report.csv` | 233 MB of real transactions, over GitHub's file-size limit |
| `.env` | see `.env.example` for the field list |
| `*.pdf` | the business process specification |
| `*.ipynb` | exploratory notebooks |
| `eda/*.html` | generated profiling reports built from the real data |
| `business*.py` | the Colab prototype this engine generalizes |

---

## Status

**Phase 1**, and complete: a deterministic, API-driven platform with no LLM or agent layer.
Phase 2 is intended to add an agentic layer *on top of* this same API rather than replace it,
which is why every endpoint is designed to work as a tool call and not only as a human-facing
request.

Not yet built: a PostgreSQL backend (the `DataRepository` contract is ready, the implementation
is not), authentication, and a caller/actor identity in the request logs. See
[`notes.txt`](notes.txt) for the reasoning behind each.
