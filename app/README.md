# Global Treasury Forecasting & Liquidity Engine

FastAPI service that forecasts remittance transaction volume (receiver country, corridor, or
agent) with Prophet, and turns that forecast into a liquidity-risk simulation: risk-adjusted
safety stock, funding shortfalls, and a liquidity-to-volume ratio for comparing corridors of
very different sizes on the same footing.

## Where this fits

This is `PredictiveAnalytics`'s **Phase 1** deliverable: a deterministic, API-driven platform, no
LLM/agent involved (see `../notes.txt` for the two-phase plan and why the phasing was chosen).
Phase 2 will put an agentic/LLM layer on top of this API rather than replace it -- everything
here is designed to also work as tool calls for that later agent, not just as a human-facing API.

Logic here generalizes a pre-existing single-corridor prototype (`business_data_send.py`, a
Colab export hard-coded to AUSTRALIA -> BHUTAN) to any receiver country / corridor / agent,
driven by the PROJECT Treasury Forecasting Flow business specification: forecast adjustment
factors and daily-monitoring logic (holidays, salary-week effects, safety stock as
risk-adjusted funding).

Neither the prototype nor the specification document is committed to this repository -- both
are kept locally, so the references above are deliberately plain text rather than links.

Related docs: `../CLAUDE.md` (repo-wide guidance + `Business_Report.csv` schema),
`../notes.txt` (project plan/phasing/open questions), `../eda/README.md` (data profiling).

## Quick start

```bash
# from the repo root
pip install -r requirements.txt
uvicorn app.main:app --reload
```

- **Dashboard** (pick a corridor/country/agent, see the forecast + charts): <http://127.0.0.1:8000/dashboard/>
- Interactive docs (try requests in the browser): <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/health>

Configuration is read from `../​.env` at startup (see "Configuration" below) -- edit it and
restart the server to change defaults; nothing needs to be recompiled/rebuilt.

### Example request

```bash
curl -X POST http://127.0.0.1:8000/api/v1/forecast/run \
  -H "Content-Type: application/json" \
  -d '{
        "dimension": "receiver_country",
        "receiver_country": "NEPAL",
        "horizon_days": 10,
        "baseline_funding_level": 50000
      }'
```

Response shape (trimmed; real output from a verified run against the actual dataset):

```json
{
  "meta": {
    "dimension": "receiver_country",
    "receiver_country": "NEPAL",
    "receiver_country_code_resolved": "NP",
    "history_days": 943,
    "include_statuses": ["Payment"]
  },
  "summary": {
    "horizon_days": 10,
    "total_shortfall_usd": 2536604.7,
    "avg_daily_requirement_usd": 183238.39,
    "safety_stock_ratio": 1.6572,
    "peak_demand_date": "2026-08-02",
    "peak_demand_shortfall_usd": 321034.12,
    "days_with_shortfall": 10,
    "baseline_funding_level_usd": 50000.0
  },
  "forecast": [
    {"ds": "2026-08-01", "weekday": "Sat", "yhat": 216404.65, "yhat_lower": 103119.0,
     "yhat_upper": 334633.9, "safety_stock": 334633.9, "daily_shortfall": 284633.9,
     "cumulative_shortfall": 284633.9, "liquidity_to_volume_ratio": 0.231}
  ],
  "trend_decomposition": {"data": ["<Plotly traces>"], "layout": {"...": "..."}}
}
```

## Configuration (`.env`)

Loaded by `app/core/config.py` (`pydantic-settings`, prefix `TREASURY_`). See `../.env` for the
shipped file with inline comments; summary:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TREASURY_DATA_BACKEND` | `csv` | `csv` (implemented) or `postgres` (placeholder, not implemented -- see "Not yet built"). |
| `TREASURY_CSV_PATH` | `<repo root>/Business_Report.csv` | Left unset by default so the path stays correct regardless of the working directory the server is launched from. |
| `TREASURY_LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `TREASURY_DEFAULT_HORIZON_DAYS` | `30` | Fallback when a request omits `horizon_days`. |
| `TREASURY_MAX_HORIZON_DAYS` | `365` | Upper bound enforced on `horizon_days`. |
| `TREASURY_DEFAULT_INCLUDE_STATUSES` | `["Payment"]` | JSON list; which `transstatus` values count as real, funded volume. |

Everything forecast-related is *also* overridable per-request in the API body
(`ForecastRequest` in `app/api/schemas.py`) -- the `.env`/`Settings` values are just fallbacks,
not the only way to configure a run. That's deliberate: env config sets the operational
defaults, request config lets each call (human or, later, agent) override anything.

### Logging

Set up once at startup (`setup_logging()`, called from `app/main.py`) and used everywhere else
via the standard `logging.getLogger(__name__)` -- nothing below `app/main.py` configures
logging itself. Every log line uses one format,
`%(asctime)s | %(levelname)-8s | %(name)s | %(message)s`, and goes to **two** places at once:

- **Console** (stderr) -- as before, so `uvicorn --reload` output is unchanged.
- **`logs/app.log`** -- new. A `logging.handlers.TimedRotatingFileHandler` rotating at local
  midnight: today's log is always the plain `logs/app.log`; at rollover it's renamed to
  `logs/app.log.<yesterday's date>` and immediately gzip-compressed to `.gz` (a custom
  `rotator`/`namer` pair -- the stdlib class doesn't compress on its own), so only *today's*
  file is ever sitting uncompressed on disk. `backupCount=7` keeps a week of compressed history
  before the handler deletes the oldest automatically -- confirmed with the user rather than
  assumed, since that's a real (if minor) data-retention decision, not just a formatting choice.
  Verified empirically (not assumed) that `backupCount`-based auto-delete still correctly
  recognizes files renamed by the custom `namer`, since older Python versions have a known
  gotcha there.
  Rotation is checked lazily on the next log call after midnight (standard library behavior,
  not a background timer) -- on an idle server the actual rollover happens whenever the first
  request after midnight comes in, not necessarily at 00:00:00.
- Log levels follow their usual meaning throughout: `INFO` for normal request/response and
  data-load events, `WARNING` for a caller's bad input the app recovered from (e.g. an
  unrecognized country when resolving holidays, a request that fails the engine's own
  validation), `ERROR`/`exception` for infrastructure failures and truly unexpected exceptions
  (full traceback captured either way). Third-party loggers (`cmdstanpy`, `prophet`,
  `matplotlib`) are held to `WARNING` unless `TREASURY_LOG_LEVEL=DEBUG`, since Prophet's own
  fit/predict logging is otherwise very noisy at `INFO`.

## Architecture

### Request flow

```text
HTTP request
  -> app/api/routes_forecast.py   (Pydantic validation, dimension check)
  -> asyncio.to_thread(...)       (hands off to a worker thread -- see "Why asyncio.to_thread")
       -> app/data/repository.py  (DataRepository.load(): CSV today, cached by mtime)
       -> app/forecasting/engine.py (TreasuryEngine: pure pandas/Prophet, no FastAPI import)
            -> app/forecasting/holidays.py (country-code resolution + Prophet holidays df)
            -> app/forecasting/charts.py   (Prophet -> Plotly figure JSON; see "Dashboard")
  <- JSON response (ForecastResponse)
```

The engine has zero knowledge of FastAPI, and the data layer has zero knowledge of Prophet --
each layer only depends on the one below it, which is what makes each independently testable
(and is why the engine could be smoke-tested directly against the CSV, without the API, during
development).

### Module map

```text
app/
  core/
    config.py           Settings (reads .env, prefix TREASURY_)
    logging_config.py    One-time logging setup; quiets noisy third-party loggers
    exceptions.py        DataSourceError / ForecastingError -- see "Exception handling" below
  data/
    schema.py             Shared dtypes/column constants (also used by eda/)
    repository.py         DataRepository (ABC) -> CSVDataRepository, PostgresDataRepository (stub)
  forecasting/
    config.py             ForecastConfig / SeasonalityConfig / HolidayConfig / LiquidityConfig
                           / LightGBMConfig
    holidays.py            pycountry name resolution + Prophet holidays dataframe builder
    engine.py              TreasuryEngine: prepare_data / train_forecast / simulate_liquidity /
                           summarize / run / evaluate_accuracy (backtest, see below)
    features.py            ~61 engineered features (calendar / payday / holiday / lag /
                           rolling / momentum) for LightGBM only -- Prophet needs none of them
    gbdt.py                Optional LightGBM comparison forecast, recursive multi-step;
                           logs the engineered feature matrix by name, grouped by family
    charts.py              Renders Prophet's trend decomposition as Plotly figure JSON
                           (no image rendering at all -- see "Dashboard" below)
  api/
    schemas.py             Pydantic request/response models (mirrors forecasting/config.py)
    deps.py                FastAPI dependency providers (repository singleton)
    routes_forecast.py     POST /api/v1/forecast/run, /baseline-suggestion, /evaluate
    routes_meta.py         GET /api/v1/meta/* (dropdown data for the dashboard/an agent)
  static/dashboard/
    index.html              Plain HTML/JS dashboard -- see "Dashboard" below
    plotly-cartesian.min.js  Self-hosted Plotly.js (cartesian bundle, ~1.4MB) -- no CDN
  main.py                  FastAPI app, request-logging middleware, dashboard mount, /health
```

### The engine (`app/forecasting/engine.py`)

`TreasuryEngine` is configured once (`ForecastConfig` + `LiquidityConfig`) and then operates on
whatever `pd.DataFrame` it's given -- it doesn't own a data source, which is what keeps it
decoupled from CSV-vs-Postgres and trivially callable outside the API (as a library, in a
notebook, in a test):

- **`prepare_data()`** -- filters to a corridor/receiver-country/agent, keeps only statuses that
  represent real funded volume (`include_statuses`, default `("Payment",)` -- see `CLAUDE.md` on
  why `transstatus` has 11 real values, not 3), aggregates to daily volume, and reindexes to a
  continuous daily calendar so gaps become explicit zero-volume days rather than disappearing
  from Prophet's view of the calendar.
- **`train_forecast()`** -- builds the combined holidays dataframe (see below) and fits Prophet
  with the configured seasonality/changepoint/interval-width settings; clips negative
  yhat/yhat_lower/yhat_upper to zero (volume can't be negative).
- **`simulate_liquidity()`** -- adds `safety_stock` (`yhat_upper` x optional buffer),
  `daily_shortfall`/`cumulative_shortfall` against a caller-supplied `baseline_funding_level`,
  and `liquidity_to_volume_ratio` (baseline / expected volume, for cross-corridor comparison).
- **`summarize()`** -- the headline JSON metrics: total shortfall, avg daily requirement, safety
  stock ratio, peak demand day and its shortfall, days with any shortfall.
- **`run()`** -- orchestrates all of the above end-to-end and (optionally) renders charts.
- **`evaluate_accuracy()`** -- backtests the current config via a single train/test holdout:
  hides the most recent `eval_days` of real history, fits on everything before that, forecasts
  exactly the hidden window, and compares against what actually happened. Reports MAPE (over
  non-zero-actual days only -- percentage error is undefined at `actual=0`, which real thin
  corridors genuinely have), RMSE (over every day, no such blind spot), `interval_coverage_pct`
  (does actual land inside `[yhat_lower, yhat_upper]` as often as `interval_width` claims it
  should), `safety_stock_coverage_pct` (the more actionable number: would the actual funding
  recommendation, buffer included, have been enough), and a naive-baseline comparison (a flat
  prediction -- the trailing mean, same as `suggest_baseline(method="average")` -- over the same
  window) so a caller can see whether Prophet earns its keep over "just fund the recent average
  every day." A second Prophet fit -- roughly the cost of one extra `run()` -- so this is a
  separate, caller-triggered check (`POST /api/v1/forecast/evaluate`), not part of the main
  pipeline. Deliberately a single holdout rather than Prophet's own rolling-origin
  `cross_validation()`/`performance_metrics()` (`prophet.diagnostics`): that refits repeatedly
  and can take minutes on one corridor, appropriate for an offline batch job, not an interactive
  dashboard button. `train_forecast()` gained an optional `horizon_days` override (defaults to
  `self.forecast_config.horizon_days` when omitted) so this can request a forecast whose length
  matches the held-out window rather than whatever horizon the caller's main config specifies.

**Holidays** (`holidays.py`): three ingredients combine into one Prophet holidays dataframe --
sending-country public holidays, receiving-country public holidays (both resolved from the
dataset's free-text country names like `"UNITED ARAB EMIRATES"` to ISO alpha-2 codes via
`pycountry.countries.search_fuzzy`, with a small manual override table for names it gets wrong),
and a synthetic monthly "salary week" event (pivoted on month-end, padded by configurable
pre/post days) modeling the payroll-driven remittance spike that no public-holiday calendar
captures. Country resolution fails soft: an unresolvable name just skips that side's holidays
(logged as a warning) rather than failing the whole forecast.

**Why `asyncio.to_thread`, not native async**: Prophet's `fit`/`predict` are synchronous,
CPU-bound calls -- there's no async Prophet API to await. The route handler stays `async def`
and the server stays responsive under concurrent requests by running the actual engine work
(including the CSV load) in a worker thread, rather than blocking the single event loop for
the several seconds a fit can take.

**Why Plotly, not matplotlib, for `charts.py`**: the trend-decomposition chart used to be a
matplotlib PNG (requiring a `threading.Lock` around rendering, since `pyplot`'s global state
isn't thread-safe -- a real constraint given forecast requests run in a thread pool). Switched
to `prophet.plot.plot_components_plotly`, returned as plain JSON
(`json.loads(fig.to_json())` -- see the docstring in `charts.py` for why not
`to_plotly_json()` directly) and rendered client-side with `Plotly.newPlot()`. No image
rendering happens server-side at all now, so there's nothing shared/mutable to lock around.
The funding-gap chart has no server-side rendering step of any kind -- see "Dashboard" below.

### Data layer (`app/data/`)

`DataRepository` (`repository.py`) is the only contract the engine/API depend on --
`load() -> pd.DataFrame`. `CSVDataRepository` reads `Business_Report.csv` and checks the file's
mtime on every call, only re-parsing the 233 MB file when it's actually changed, so appending
new daily rows is picked up on the next request without restarting the service, without paying
the full parse cost on every single call either. `PostgresDataRepository` is a stub that raises
`NotImplementedError` -- DB schema/connection details are still an open question (`notes.txt`);
once decided, implement `load()` there and flip `TREASURY_DATA_BACKEND=postgres`, and nothing
in `forecasting/` or `api/` needs to change.

### API (`app/api/`)

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/forecast/run` | The main endpoint. `dimension` is one of `receiver_country`, `corridor`, `agent`; the rest of the body configures the forecast (see `ForecastRequest` for every field). Setting `include_lightgbm: true` adds an opt-in `yhat_lightgbm` comparison column (tuned via the `lightgbm` object) and sets `meta.lightgbm_included`; it changes no funding number -- see "Known caveats". |
| `POST /api/v1/forecast/baseline-suggestion` | Suggests `baseline_funding_level` from trailing historical volume for the same filters -- not a forecast, no Prophet. `method` is one of `average`, `weighted_average` (EWMA), `median`; see `BaselineSuggestionRequest`. |
| `POST /api/v1/forecast/evaluate` | Backtests forecast accuracy for the given filters/config via a single train/test holdout -- `eval_days` most recent days hidden, fit on the rest, compared against what actually happened. Returns MAPE, RMSE, interval coverage, safety-stock coverage, and a naive-baseline comparison; see `EvaluationRequest`/`EvaluationResponse` and `TreasuryEngine.evaluate_accuracy` above. A second Prophet fit -- a deliberate, caller-triggered check, not run automatically with `/run`. |
| `GET /api/v1/meta/sending-countries` | Distinct `Sending_Country` values in the data. |
| `GET /api/v1/meta/receiver-countries` | Distinct `Receiver_Country` values. |
| `GET /api/v1/meta/agents` | Distinct `Agent_Name` values. |
| `GET /api/v1/meta/corridors` | Distinct (sending, receiver) pairs actually observed -- not the full cross-product. |
| `GET /api/v1/meta/statuses` | Distinct `transstatus` values (all 11, not just Payment/Cancel/Block). |
| `GET /api/v1/meta/summary` | Dataset-wide overview -- total row count, `TRN_Date` min/max, and distinct-value counts for every key dimension column. A live, always-current counterpart to this README's own schema table (see `CLAUDE.md`); confirms what's actually loaded without re-running the EDA report. |
| `GET /` | API orientation -- links to `/docs`, `/redoc`, `/dashboard/`, `/health`, `/ready`. This service has no landing page of its own otherwise. |
| `GET /health` | Liveness only -- the process is up and answering HTTP. Never touches the data source, so it stays meaningful even if that's unavailable. |
| `GET /ready` | Readiness -- actually attempts to load the configured data source; 503 (not 200) if that fails, distinct from `/health`'s liveness-only check. Useful for a load balancer/orchestrator that should hold traffic back until this returns 200. |

The `meta` endpoints exist so a dashboard (or, later, an agent) can populate filters from the
real data instead of hardcoding country/agent lists that will drift as the dataset grows.
`/`, `/health`, and `/ready` are grouped under their own `system` OpenAPI tag, separate from
`forecast`/`meta`, since none of them touch forecasting or dataset filters.

**OpenAPI completeness.** Every request/response field across every endpoint now carries a
`description` (previously several -- `ForecastRequest.horizon_days`, `ForecastMeta.*`,
`ForecastPoint`'s numeric fields, etc. -- had none), request AND response models both carry
worked examples (`model_config["json_schema_extra"]["examples"]`), and every route has an
explicit `operation_id` (FastAPI auto-generates one either way, but the auto-generated form is
a verbose `function_name_path_method` string -- explicit ones are what a codegen tool or another
reader actually wants). `ForecastResponse.summary` was previously a bare, untyped `dict` --
correct on the wire but opaque in the OpenAPI schema itself (`{"type": "object"}`, no
properties); it's now the fully-typed `ForecastSummary` model, so its six fields show up
properly documented in `/docs`/`/openapi.json` instead of only being knowable by reading
`engine.py`'s `summarize()`. The app itself now also declares a `summary` (short, separate from
the longer `description`) and a `servers` entry. Deliberately **not** added: `contact`/
`license_info`/`terms_of_service` -- these are business/legal decisions for an internal tool
with no public license or support contact decided yet (see `notes.txt`'s open questions), not
something to invent unprompted; and no `securitySchemes`, since there genuinely is no auth in
this phase (see "Not yet built" below) and a document claiming one would be actively misleading.
Validated against the live `/openapi.json` with `openapi-spec-validator`, same as every prior
pass.

### Dashboard (`app/static/dashboard/`)

A plain HTML/JS page (no framework) at `/dashboard/`, served same-origin by this same FastAPI
app via a `StaticFiles` mount in `main.py` -- no separate server, no CORS configuration needed.
It's a thin client over the API above: on load it populates its filter dropdowns from the
`meta` endpoints (including corridor-aware filtering -- picking a sending country narrows the
receiver dropdown to corridors that actually exist), and `POST`s to `/api/v1/forecast/run` to
render the summary stat tiles, both charts, and a collapsible day-by-day data table. Built for a
non-technical audience (Treasury Management), so every input and output metric carries an inline
"i" tooltip plus a full glossary section, both generated from one `GLOSSARY` object in the script
so the two can't drift apart.

**Both charts are real interactive Plotly charts**, not static images -- zoom (drag a box,
double-click to reset), pan, autoscale, and a "download as PNG" button, all from Plotly's own
modebar, plus a native unified hover tooltip (`hovermode: "x unified"` -- one tooltip listing
every series at the hovered x). They're laid out one per row (`.charts-grid` is a single-column
grid), each using the full width of the page, rather than side-by-side -- more room for a wide
date range to plot against, and `responsive: true` in `PLOTLY_CONFIG` keeps both correctly sized
if the window is resized afterward. Plotly.js is self-hosted
(`app/static/dashboard/plotly-cartesian.min.js`, the "cartesian" partial bundle -- ~1.4MB,
covers the scatter/line/area traces both charts actually use, versus ~4.9MB for the full
bundle), not loaded from a CDN, consistent with keeping this dashboard usable on a corporate
network with no external calls.

- **Funding-gap chart**: built entirely client-side from `data.forecast` (`buildFundingGapFigure`
  in the script) -- every value it plots (`yhat`, `yhat_lower/upper`, `safety_stock`,
  `daily_shortfall`, the baseline) was already in the API response, so this needed no backend
  change at all.
- **Trend-decomposition chart**: the backend renders it (`app/forecasting/charts.py`, via
  Prophet's own `plot_components_plotly`) and returns the figure as plain JSON
  (`ForecastResponse.trend_decomposition`); the dashboard just calls
  `Plotly.newPlot(el, trend_decomposition.data, trend_decomposition.layout)`. This *did* need a
  backend change -- Prophet's per-day trend/weekly/yearly/holiday values aren't otherwise
  reachable -- and replaces what used to be a static matplotlib PNG.
- **Theming**: both charts read the page's current CSS custom properties
  (`getThemeColors()`) so their background/grid/font match light or dark mode; the
  server-rendered trend-decomposition figure gets the same treatment patched on
  client-side (`themeServerFigureLayout()`), since the backend can't know the caller's
  color scheme at render time. Trace-level colors (Prophet's own line colors) are left as
  Prophet's defaults rather than re-themed, to avoid assuming things about its internal
  figure structure.
- **Trade-off worth knowing**: the previous hand-rolled SVG version had a bespoke keyboard
  interaction (arrow keys + an `aria-live` region speaking each day's values). Plotly's own
  keyboard/screen-reader support isn't as tailored. The day-by-day data table below the charts
  remains the full accessible fallback either way -- every value either chart shows is also in
  that table.

**`baseline_funding_level` has a "Suggest" auto-fill**, next to the field itself, because it's
a plain user-typed number with no calculated default -- see the "Baseline funding level" caveat
below. Planned before building (see `notes.txt`): a small dedicated endpoint,
`POST /api/v1/forecast/baseline-suggestion`, reusing `TreasuryEngine.prepare_data()` on real
historical transactions (not Prophet's fitted history -- more accurate, and usable before a
forecast has even been run, since the button lives in the main form). Anchored to *that
filter's own* last historical date, the same convention the forecast horizon itself uses.
Three methods, designed as a small registry so a future one is just a new entry, not a
redesign: `average` (mean), `weighted_average` (EWMA -- recent days count more within the
window), `median` (resistant to a single large one-off day -- worth defaulting to for a
corridor flagged as thin/spiky). The window (default 30 days) is adjustable in the UI, and
clamps gracefully with a visible note when less history exists than requested.

**"Evaluate forecast accuracy (backtest)"** is its own collapsible card, below the main
results, wired to `POST /api/v1/forecast/evaluate`. Unlike the funding-gap/trend-decomposition
charts it doesn't require a forecast to have been run first -- like "Suggest," it builds its
request straight from whatever dimension/filters/advanced settings are currently selected in
the form (`buildEvalRequestBody()`, sharing a `buildModelConfigFields()` helper with
`buildRequestBody()` so both requests reflect identical model settings, not two copies that can
drift). Its own results panel: five stat tiles (MAPE, RMSE, interval coverage, safety-stock
coverage, and a "vs. naive baseline" badge -- `good`/`warning`/`critical` by how far short of
the configured `interval_width` coverage actually fell, the same three-tier badge convention as
the shortfall badge above, factored into a shared `makeBadge()` helper), and an "actual vs.
forecast" Plotly chart over the held-out window built the same way as the funding-gap chart
(zero-width fill trace for the shaded interval band, `hovermode: "x unified"`, themed via
`getThemeColors()`) -- plus a red marker on any day actual volume fell outside the model's own
expected range. A caveat line under the results states plainly that a single held-out window is
itself a small sample, most relevant on a thin/spiky corridor -- see "Known caveats" below.

Things worth knowing if you touch it:

- **The `<form>` has `novalidate`.** A prior version relied on native HTML5 constraints
  (`min`/`step` on the number inputs) and hit a real bug: the default `interval_width` (0.8)
  didn't satisfy its own `step="0.05"` constraint, and because that field lives inside a
  collapsed `<details>` ("Advanced settings"), the browser couldn't focus it to report the
  error -- so it silently blocked the entire form submission with no visible error at all.
  Caught by actually driving the page with Playwright, not by testing the API in isolation.
  The fix: `novalidate`, and let the backend's Pydantic schema be the single source of truth
  for validation (it already returns a clean, visible error either way).
- **The LightGBM parameters therefore validate in JS, not via `min`/`max` attributes.** They
  sit inside *two* nested collapsed `<details>` ("Advanced settings" -> "LightGBM advanced
  settings"), which is precisely the situation that produced the silent-block bug above, only
  one level deeper. `LGBM_RULES` in the dashboard JS mirrors `LightGBMIn`'s Pydantic
  constraints field-for-field; each input validates on `input`/`blur` with an inline message,
  and `validateLightGBM()` **opens both enclosing `<details>` and focuses the offending field**
  before reporting -- so a bad value can never block submission invisibly. The backend still
  validates independently; this only means the user is told which field is wrong instead of
  reading a 422. The two constraint sets are duplicated by necessity (browser and server share
  no schema here) and must be kept in sync -- same category of deliberate duplication as the
  ruff/flake8 line-length setting.
- **The form is grouped into titled sections, not one flex row.** It used to be a single
  `.field-row` holding dimension, filters, horizon *and* the baseline block. Because that row
  used `align-items: flex-end` and the baseline field is ~3 rows taller than a plain select
  (it carries the auto-fill controls and a result line), the baseline stranded itself at the
  top-right with its label floating out of line with everything else. Now: **What to
  forecast** -> **Funding baseline** -> *Advanced settings* (Model / Seasonality & events /
  Statuses / Second model) -> the run button on its own ruled line. `align-items: start`
  everywhere, and `max-width` caps on fields so a lone visible dropdown can't stretch to the
  full card width.
- **Fieldset legends separate groups; don't add a `border-top` to a `<fieldset>`.** A
  `<legend>` renders above its fieldset's padding box, so a top border draws straight through
  the label -- it did exactly that to "Second model". Groups are separated with margin
  instead.
- **The LightGBM toggle is one clickable `<label>` wrapping title + description.** The
  description was previously a `<p>` indented 22px and capped at 70ch inside a full-width
  block, so it hugged the left edge with a large empty gutter and read as cornered. Wrapping
  it in a bordered, hoverable control gives the text a container, makes the whole block a
  click target, and keeps the measure readable (~88ch) rather than stretching prose across
  1000px+.
- **The data table always emits every cell, including the hidden LightGBM one.** Omitting a
  `<td>` to "hide" a column silently misaligns the whole row: with LightGBM off, the body had
  nine cells against a ten-column header, so every value from that column rightwards rendered
  under the wrong heading -- "Safety stock" displayed `daily_shortfall`, and Liquidity/volume
  came out empty. Two causes, both fixed: the header cell carried `class="lgbm-col hidden"`
  but **no generic `.hidden` rule exists in this stylesheet** (every one is scoped to a
  component class, e.g. `.field.hidden`), so it never hid; and the matching `<td>` was
  conditionally omitted. Now the cell is always rendered and `.lgbm-col.hidden` hides both
  halves together, after the rows are built so the toggle reaches the body too. If you add
  another optional column, render it always and hide it with CSS -- never vary the cell count.
- **Charts are plotted only after their container is visible.** `renderResults()` removes
  `hidden` from `#results` as its *first* action, before any `Plotly.newPlot()`. Plotly
  measures the container at plot time, and a `display: none` container measures zero, so it
  silently fell back to its built-in 700px default -- the funding-gap chart sat at ~65% of
  the card width at any window size. `responsive: true` does not rescue this; it only
  re-fits on window resize, which never fires just because an element became visible.
- **`themeServerFigureLayout()` strips the server figure's baked-in `width`.** Prophet's
  `plot_components_plotly` writes `width: 900` into the layout JSON, which pins the trend
  chart to 900px regardless of its container. The themer deletes it and sets
  `autosize: true`; `height` is deliberately kept, since that's a real vertical sizing
  choice for a four-panel stack rather than a horizontal constraint.
- **Chart legends sit in the top margin, not below the plot.** Both the funding-gap and
  evaluation charts previously used `legend: { y: -0.25 }` against a 70px bottom margin --
  roughly 100px below the plot at those heights, i.e. inside the clipped region. They now
  use `yanchor: "bottom", y: 1.02` with the top margin sized to hold them, which also frees
  the bottom margin for the x-axis title.
- **Page order puts decisions first and reference last**: filters -> stat tiles ->
  funding-gap chart -> day-by-day table -> accuracy backtest -> trend decomposition ->
  glossary. The trend decomposition and glossary moved to the foot deliberately: the first
  is interpretive (nobody funds a corridor from Prophet's seasonality panels) and the second
  is read once then rarely again, so neither should sit between the operator and the numbers.
- **Enabling the LightGBM comparison reveals a collapsed disclosure, not eleven inputs.**
  Tuning is a rare need, so checking the box shows a "LightGBM advanced settings" summary
  (badged with the parameter count); opening that reveals the parameters, grouped into
  Boosting / Tree shape / Sampling / Regularisation & reproducibility with a "Reset to
  defaults" button and a hint showing how many values differ from the defaults. Unchecking
  re-collapses it and clears any error styling, so re-enabling always starts clean.
- **`growth: "logistic"` is deliberately not offered** in the dashboard's dropdown (only
  `linear`/`flat`) -- confirmed by testing directly that Prophet's logistic growth mode
  requires a `cap` column the engine doesn't currently set, so it fails every time. It's still
  reachable via the raw API/`/docs` (and will return a clean `ForecastingError` → 500, not
  crash), but there's no reason to offer a UI option that always fails.
- **Info-icon tooltips render through a shared portal (`#info-tooltip-portal`), not CSS
  `:hover`.** An earlier version used pure CSS (`position: absolute` + `:hover`), which broke
  in two different places found by actually hovering them, not by reading the CSS: table
  headers (`position: sticky` inside a scrolling container -- a tooltip opening upward had
  nowhere to go) and stat tiles (`.stat-grid` needs `overflow: hidden` for its rounded-corner/
  hairline-gap look, which also clips anything trying to render outside it). Rather than
  special-case a third context when the next one turns up, `showInfoTooltip()`/
  `hideInfoTooltip()` position one shared `position: fixed` element via
  `getBoundingClientRect()`, immune to any ancestor's `overflow` -- flips to open downward
  automatically when there's no room above the viewport.
- **Plotly legend labels are terse on purpose** (`Forecast`, `Safety stock`, `Baseline`,
  `Range`, not the fuller names used in the caption/glossary/tooltip). Found by actually
  running it: the fuller names wrapped the legend to two rows and got clipped by Plotly's own
  allotted legend height. The figcaption and glossary still carry the full explanation --
  the legend only needs to key each trace, not re-explain it.
- **The data table's `Weekday` column (Mon..Sun) is computed server-side**
  (`ForecastPoint.weekday`, from `ds.dt.dayofweek` against a fixed abbreviation tuple in
  `routes_forecast.py`), not derived client-side from the `ds` date string. Two reasons: a
  locale-independent lookup instead of pandas' own `%a` strftime directive (which would follow
  whatever locale the server happens to run under), and it sidesteps a real JS foot-gun --
  `new Date("2026-08-20").getDay()` parses the date as UTC midnight but reports the day-of-week
  in the *browser's local* timezone, which silently shifts by a day for viewers west of UTC.
  Letting pandas compute it once, correctly, means the dashboard just renders the string.

## Code standards

### PEP8 / linting

Enforced with `ruff` and `flake8` (config in `../pyproject.toml` and `../.flake8` respectively,
kept in sync deliberately -- see below). Both pass clean:

```bash
pip install -r ../requirements-dev.txt
ruff check app/
flake8 app/
```

Line length is capped at 100 (not PEP8's original 79 -- PEP8 itself endorses raising it for
teams that prefer that, and 100 comfortably fits this codebase's descriptive
variable/function names without constant wrapping). One thing worth knowing about the config:

- Ruff's `B008` (bugbear) flags `Depends(...)` as a function call in an argument default --
  that's the correct, required FastAPI dependency-injection pattern, not the mutable-default
  bug B008 exists for. `pyproject.toml` allowlists `fastapi.Depends` explicitly rather than
  suppressing the rule everywhere or rewriting correct FastAPI code to dodge a false positive.

### OpenAPI

FastAPI generates the schema from the route/Pydantic definitions; it's been validated (not just
assumed correct) with `openapi-spec-validator` against the live `/openapi.json`. Every route has
a `summary`, every non-2xx status code it can actually return is documented via `responses=`
(backed by a real `ErrorResponse` schema, not an untyped `dict`), and `ForecastRequest` carries a
worked example so `/docs`'s "Try it out" starts from a request that's known to work.

```bash
uvicorn app.main:app &
python -c "
import httpx, json
from openapi_spec_validator import validate
spec = httpx.get('http://127.0.0.1:8000/openapi.json').json()
validate(spec)
print('valid')
"
```

### Exception handling

Every failure mode has a deliberate home, not a blanket `try/except Exception: pass`:

| Exception | Raised where | HTTP status | Meaning |
| --- | --- | --- | --- |
| `ValueError` | `TreasuryEngine.prepare_data`/`train_forecast`, `ForecastRequest` validators | 422 | Caller's input problem: filters matched nothing, or matched too little history to fit. |
| `DataSourceError` (`core/exceptions.py`) | `CSVDataRepository.load`, `TreasuryEngine`'s input-schema check | 503 | The data itself couldn't be read/is malformed -- likely transient, worth retrying. |
| `ForecastingError` (`core/exceptions.py`) | `TreasuryEngine.train_forecast`/`summarize` | 500 | Prophet failed for a reason that isn't the caller's filters. |
| anything else | any route | 500 | Last-resort catch-all (see below) -- should never happen, logged loudly if it does. |

Layers of defense, outside-in:

1. Every route (`routes_forecast.py`, `routes_meta.py`) catches the specific exceptions above
   plus a final bare `except Exception` that logs the full traceback (`logger.exception`) and
   returns a generic, non-leaky `{"detail": "Internal server error"}` -- a client never sees
   internal exception text it didn't already get from a deliberate, typed error.
2. `get_repository()` (`api/deps.py`) runs as a FastAPI dependency *before* a route body's own
   try/except even executes, so it raises `HTTPException` directly rather than a plain
   exception that would bypass that route's error handling.
3. `app/main.py` adds a global `@app.exception_handler(Exception)` plus a try/except in the
   logging middleware itself, as a safety net for failures outside any route body (a bug in a
   dependency, in Starlette, etc.) -- belt-and-suspenders, not the primary defense.
4. Two spots fail *soft* by design, logging and degrading gracefully instead of erroring:
   `holidays.py` (an unresolvable country name or a `holidays`-library edge case just skips
   that side's holidays) and `engine.run()`'s chart rendering (one chart failing doesn't cost
   the caller the numeric forecast, which matters more).
5. `Settings` (`core/config.py`) uses `Literal` types for `data_backend`/`log_level` so a typo'd
   `.env` value fails immediately at process startup with a clear `pydantic.ValidationError`,
   instead of surfacing confusingly on whatever request happens to touch it first.

## Known caveats (real, not glossed over)

- **Horizon is anchored to that filter's last date, not "today."** A corridor/agent whose data
  goes quiet before the dataset's overall end date still forecasts forward from *its own* last
  transaction -- confirmed by running an agent whose history stopped in early 2024, which
  produced a forecast starting right after that, not near the present.
- **`baseline_funding_level` is a plain user input, not calculated by the engine.** It flows
  straight through to `daily_shortfall = max(0, safety_stock - baseline)` untouched -- Prophet
  never sees it. `business_data_send.py`, the prototype this generalizes, *did* auto-calculate
  it (average of the last 30 historical days); that auto-calculation was deliberately not
  carried into `TreasuryEngine` so callers can test "what if we funded X" scenarios rather than
  only ever seeing "what we funded last month." The dashboard's "Suggest" button (see below)
  restores that calculation as an opt-in helper instead, with a choice of methods.
- **Sparse/spiky corridors produce large-looking safety stock relative to the mean.** When a
  filtered series has many zero-volume days punctuated by occasional large ones, Prophet's
  uncertainty interval (and therefore `yhat_upper`-based safety stock) is proportionally wide.
  That's a real property of thin corridors, not a bug -- treat a very high safety-stock ratio as
  a signal the corridor is thin/volatile, not a modeling error. Fixing the additive-mode defect
  below doesn't eliminate this: re-checked on the same AUSTRALIA example, `safety_stock_ratio`
  moved from 2.47x to 2.66x under multiplicative mode -- this corridor's genuine
  weekday/weekend volatility, not the clipping artifact, is what drives the ratio.
- **Fixed: additive seasonality could silently floor a real, nonzero corridor's forecast to
  $0.00.**

  Found via `technical.md`'s worked example for Receiver Country Australia, and confirmed
  against real history rather than just the decomposition math: that corridor's weekends
  average $50,674/day and are nonzero on 96.6% of 268 weekends across 2.5 years -- not a thin
  corridor. Under Prophet's own additive-seasonality default, a large negative weekly effect
  and a large negative yearly effect landed on the same calendar days and stacked on top of
  trend, summing below zero and getting silently floored to a zero-dollar prediction --
  indistinguishable in the API response from a genuine "no volume expected" forecast.

  Fix: `ForecastConfig.seasonality_mode` now defaults to `"multiplicative"` instead of
  Prophet's own `"additive"` default. Seasonal and holiday effects scale with the trend level
  rather than being flat dollar offsets, so they can no longer independently drag the sum past
  -100% of trend the way additive offsets could. Re-running the same corridor and horizon under
  the new default produced no more exact-zero days, and the model's own fitted weekend effect
  bracketed the real historical weekend-to-weekday ratio closely -- evidence this produces a
  materially more honest forecast, not just a differently-shaped one. The field stays
  overridable per request for a caller who wants to compare against the old behavior. See
  `technical.md` section 10 for the full before/after worked example with real numbers from
  both modes.
- **The LightGBM comparison column is a second opinion, never the funding basis.** Enabling
  `include_lightgbm` adds `yhat_lightgbm` and nothing else -- `safety_stock`,
  `daily_shortfall`, `cumulative_shortfall` and every value in `summary` stay Prophet-derived,
  and `tests/test_api_forecast.py` asserts that enabling it leaves `yhat` and the summary
  unchanged. That split is measured, not cautious-by-default. Across 36 rolling-origin
  backtests (12 entities x 3 windows, all three dimensions):
  - a LightGBM **quantile band** covered 90.3% of held-out days against Prophet's 96.8% at
    the same nominal width -- it under-funds roughly one day in ten, the one failure a
    prefunding plan cannot absorb;
  - on **point accuracy** LightGBM won only 10/24 head-to-head runs, and its errors are
    heavy-tailed rather than uniformly worse (median comparable, mean far worse) -- on
    `HONG KONG -> INDIA` it posted 376-463% WAPE against Prophet's 142-151%, consistently
    across all three windows. Gradient-boosted trees are piecewise-constant and cannot
    extrapolate, so once a recursive forecast drifts it can stay in the wrong regime for the
    rest of the horizon.
  Read a large Prophet/LightGBM divergence as *"this corridor is uncertain, go run
  `POST /api/v1/forecast/evaluate`"*, not as a reason to prefer either number.
- **LightGBM degrades further into the horizon than Prophet does.** It is lag-based, so a
  multi-day forecast has to be produced recursively -- each prediction becomes the next day's
  `lag_1d` and error compounds. A 3-day LightGBM column is a much stronger claim than a
  30-day one. Prophet has no such property: it is a function of the timestamp, so every
  horizon day is predicted independently.
- **Holiday effects are noisy with ~2.5 years of history.** Prophet needs several occurrences of
  a holiday to estimate its effect well; rare holidays only recur 2-3 times in this dataset, so
  read holiday effects as directional, not precise.
- **Validated against, not copied from, `business_data_send.py`.** Running this engine on the
  same AUSTRALIA -> BHUTAN corridor/baseline the prototype used lands within ~1-3% of its
  reported shortfall and safety-stock ratio, despite this engine also modeling AU/BT holidays
  the prototype didn't -- a reasonable cross-check, not an exact-match test.
- **`growth="logistic"` doesn't work.** Confirmed directly: Prophet requires a `cap` column for
  logistic growth, which `TreasuryEngine` never sets. It fails cleanly (`ForecastingError` →
  500), not silently, but it's a real functional gap, not just an unexposed option -- the
  dashboard excludes it from its dropdown for exactly this reason.
- **A single evaluation holdout is itself only a sample, most of all on a thin/spiky corridor.**
  `evaluate_accuracy()`'s MAPE/coverage numbers describe *one* held-out window, not the model's
  true long-run accuracy -- the same sparse/spiky-corridor caveat above applies here too, just
  measured on the test side instead of the funding side. A single bad (or lucky) window can look
  worse (or better) than the model actually is on that corridor. Prophet's own rolling-origin
  `cross_validation()` would average over many windows and give a steadier answer, at a cost
  (repeated refitting) that ruled it out for an interactive endpoint -- see `evaluate_accuracy()`
  above. Worth remembering when a low coverage number shows up on a corridor with little history.

## Not yet built

- **PostgreSQL backend** -- interface is ready (`DataRepository`), implementation isn't; DB
  choice is still open (`notes.txt`).
- **Auth** -- none yet; still an open question in `notes.txt`.
- **Caller/actor field in logs** -- `notes.txt`'s Phase 1 design constraints called for request
  logs to carry a caller/actor identity (human vs., later, agent) so Phase 2 wouldn't need to
  replumb logging. The current logging middleware (`app/main.py`) logs method/path/status/
  duration but not caller identity yet -- worth adding before Phase 2, not yet done.
- **Permission/access-control model** -- also flagged in `notes.txt` as worth sketching early;
  nothing exists yet beyond "the API is open."
