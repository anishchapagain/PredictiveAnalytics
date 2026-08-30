---
name: treasury-dashboard
description: Use when creating, editing, styling, or debugging anything under app/static/dashboard/ (the Treasury Management dashboard) -- UI component conventions (stat tiles, info tooltips, glossary, charts, forms, badges) and the treasury/forecasting domain concepts (safety stock, baseline funding, shortfall, liquidity ratio, forecast methods, accuracy evaluation/backtesting) established in this project. Also relevant when touching app/forecasting/charts.py or ForecastResponse/BaselineSuggestionResponse/EvaluationResponse, since the dashboard is a thin client over those.
---

# Treasury dashboard

The dashboard (`app/static/dashboard/index.html`) is a single self-contained HTML/JS file, no
build step, served same-origin by the FastAPI app (`StaticFiles` mount in `app/main.py`). Its
audience is the **Treasury Management team** -- not engineers -- so user-friendliness is the
standing priority: every input and output carries an explanation, nothing is jargon-only.

Before making changes: skim `app/README.md`'s "Dashboard" section for full architectural detail
and the reasoning behind each decision below. This skill is the condensed, task-oriented version;
that doc is the source of truth when they'd ever disagree.

## UI component conventions

**Design tokens.** Colors are CSS custom properties on `:root`, redefined under
`@media (prefers-color-scheme: dark)` -- `--page`, `--surface`, `--text-primary/secondary/muted`,
`--hairline`, `--border-ring`, `--accent` (blue), `--series-2` (orange, the second categorical
hue), `--status-good/warning/critical`. Never hardcode a hex color in new CSS -- add a token if
one doesn't exist yet, so light/dark stay in sync automatically.

**Cards** (`.card`): the base container -- surface background, hairline border, rounded corners,
consistent padding. Every top-level section (form, glossary, results, data table) is one.

**Stat tiles** (`.stat-grid` / `.stat-tile`, built via the `statTile(label, value, sub,
glossaryKey)` JS helper): label + big value + optional smaller sub-line + an info icon. The grid
needs `overflow: hidden` for its rounded-corner/hairline-gap look between tiles -- see the
tooltip-portal note below for why that matters.

**Info-icon tooltips -- always use the portal, never CSS `:hover`.** Every field label, stat-tile
label, and table header carries a `<span class="info-slot" data-term="KEY">`, wired up by
`wireInfoIcons()` into a real `.info` icon. Hovering/focusing it calls `showInfoTooltip(iconEl,
text)`, which positions ONE shared `#info-tooltip-portal` element (appended to `<body>`, `position:
fixed`) via `getBoundingClientRect()` -- flips to open downward automatically when there's no room
above the viewport. **Do not** go back to a CSS-only `position: absolute` + `:hover` tooltip
attached inside the triggering element: that broke twice already (sticky table headers clipped by
their scroll container; stat tiles clipped by `.stat-grid`'s own required `overflow: hidden`) and
the portal fixes the whole bug class at once, not just those two spots. Any new context with an
`overflow: hidden`/`auto` ancestor will hit the same bug if it ever goes back to CSS-only.

**Glossary**: one JS object, `GLOSSARY = { key: [term, definition] }`, is the single source of
truth for every tooltip's text AND the full "What do these numbers mean?" section
(`renderGlossary()`). Adding a new field/metric means adding one `GLOSSARY` entry and pointing an
`info-slot`/`statTile(...)` call at its key -- never write the same explanation in two places.

**Badges** (`.badge-good` / `.badge-warning` / `.badge-critical`, built via the shared
`makeBadge(status, text)` JS helper): status color + icon (a dot) + text label together, never
color alone -- e.g. the "Days with shortfall" tile's "Shortfall on N of M days" vs. "Fully
covered", or the evaluate-accuracy card's coverage/vs.-naive-baseline badges (three-tier --
`good`/`warning`/`critical` by how far a coverage percentage falls short of its target, see
`coverageBadgeStatus()`). Adding a new badge means calling `makeBadge()`, not hand-rolling the
dot+label markup again.

**Charts -- Plotly, self-hosted, not a CDN.** `plotly-cartesian.min.js` (the "cartesian" partial
bundle, ~1.4MB -- confirmed both charts only ever emit `scatter`-type traces, so this is enough;
don't reach for the full ~4.9MB bundle without a real reason) is loaded via a plain `<script>` tag,
no CDN, so the page needs no external network access. Both charts:
- Read the page's *current* theme via `getThemeColors()` (pulls resolved values off
  `getComputedStyle(document.documentElement)`) so they always match light/dark mode --
  never hardcode chart colors separately from the CSS tokens above.
- Get zoom (drag a box, double-click to reset), pan, autoscale, and "download as PNG" for free
  from Plotly's modebar, plus a native `hovermode: "x unified"` tooltip (one tooltip, every
  series, at the hovered x) -- don't hand-roll a crosshair/tooltip/legend again; Plotly already
  does this correctly.
- Keep legend labels terse (`Forecast`, `Safety stock`, `Baseline`, `Range`, not the fuller
  names used in captions/glossary/tooltips) -- longer labels wrap the legend to two rows and get
  clipped by Plotly's own allotted legend height. Full names belong in the figcaption/glossary,
  not the legend.
- The funding-gap chart (`buildFundingGapFigure`) is built entirely client-side from
  `data.forecast` -- no backend involvement. The trend-decomposition chart is rendered
  server-side (`app/forecasting/charts.py`, via Prophet's `plot_components_plotly`) and sent as
  plain JSON (`ForecastResponse.trend_decomposition`); patch its `layout` client-side
  (`themeServerFigureLayout()`) for theme colors rather than re-theming individual traces.

**Forms**: the form has `novalidate`. Native HTML5 `min`/`step`/`required` constraints on inputs
inside a *collapsed* `<details>` ("Advanced settings") can silently block submission with zero
visible error -- the browser can't focus/report an invalid field it can't see. The backend's
Pydantic schema is the real validation source of truth; it already returns a clean, visible 422
either way, so don't re-add native constraint validation as the enforcement mechanism.

## Treasury/forecasting domain concepts

(Full plain-language versions of all of these live in the dashboard's own `GLOSSARY` object --
read that for the exact wording already shown to users.)

- **Dimension**: what's being forecast -- `receiver_country` (all corridors into one country
  combined), `corridor` (one sending -> receiving pair), or `agent` (one payout agent across every
  corridor it serves).
- **`transstatus` has 11 real values, not 3.** Only statuses in `include_statuses` (default just
  `"Payment"`) count as real funded volume -- cancelled/failed/expired/compliance-held
  transactions never moved money. See `CLAUDE.md` for the full list.
- **Safety stock**: the risk-adjusted funding recommendation for a day -- Prophet's `yhat_upper`
  plus an optional `safety_buffer_pct` on top. **Safety stock ratio**: safety stock / expected
  volume (`yhat`) -- e.g. 1.6x. A *very* high ratio (well above ~2x) signals a thin/spiky corridor,
  not a modeling bug.
- **Baseline funding level is a plain user input, is NOT calculated by the engine.** It's compared
  against safety stock to compute shortfall (`daily_shortfall = max(0, safety_stock - baseline)`);
  Prophet never sees it. The prior single-corridor prototype (`business_data_send.py`) DID
  auto-calculate it (last-30-days average) -- that was deliberately not carried into
  `TreasuryEngine` so callers can test "what if we funded X" scenarios.
- **Baseline auto-fill** (`POST /api/v1/forecast/baseline-suggestion`, the "Suggest" button next
  to the field): reuses `TreasuryEngine.prepare_data()` on real historical actuals (not Prophet's
  fitted history), anchored to *that filter's own* last historical date (same convention the
  forecast horizon itself uses), clamps gracefully with a visible note if less history exists than
  the requested window. Three methods, each a small registry entry (adding a fourth is one new
  entry, not a redesign): `average` (mean), `weighted_average` (EWMA -- recent days count more),
  `median` (resistant to a single large one-off day -- often the better default for a thin/spiky
  corridor).
- **Shortfall / cumulative shortfall / peak demand day**: `daily_shortfall` is that day's gap
  between baseline and safety stock; `cumulative_shortfall` is the running total across the
  horizon; "peak demand day" is the single day with the largest `daily_shortfall`.
- **Liquidity-to-volume ratio**: `baseline / yhat` for a given day -- lets you compare how
  well-funded very different corridors are on the same scale. Shown as `—` when `yhat` is
  ~zero (ratio isn't meaningful there).
- **Growth**: `linear` (trend can keep rising/falling) or `flat` (no trend, seasonality/holidays
  only) -- **never `logistic`**. Confirmed directly: Prophet's logistic growth requires a `cap`
  column the engine doesn't set, so it always fails. It's deliberately excluded from the
  dashboard's dropdown (still technically reachable via the raw API, where it fails cleanly with
  a `ForecastingError` -> 500, not a crash) -- don't re-add it to the UI without also fixing that.
- **Interval width**: how wide the `yhat_lower`/`yhat_upper` band is (0.8 = the model expects the
  actual value inside that range ~80% of the time).
- **Salary week**: a synthetic recurring end-of-month "payday" event fed into Prophet as a
  holiday, since remittance volume spikes around payday regardless of public holidays.
- **Horizon is anchored to that filter's own last date, not "today."** A corridor/agent whose data
  goes quiet early still forecasts forward from its own last transaction.
- **Accuracy evaluation / backtest** (`POST /api/v1/forecast/evaluate`, the "Evaluate forecast
  accuracy" card): hides the most recent `eval_days` of real history, fits on everything before
  that, forecasts the hidden window, compares against what actually happened. A second Prophet
  fit -- a deliberate on-demand check, not run automatically with every forecast. Reports MAPE
  (excludes zero-actual test days -- undefined there, and real thin corridors do have true
  zero-volume days), RMSE (covers every day), `interval_coverage_pct` (actual vs.
  `[yhat_lower, yhat_upper]`, checked against `interval_width`), `safety_stock_coverage_pct` (the
  more actionable one -- actual vs. what would actually have been funded, buffer included), and a
  naive-baseline comparison (flat trailing average, same method as
  `suggest_baseline(method="average")`) so a caller can see whether Prophet is earning its keep.
  **A single held-out window is itself only a sample** -- same underlying issue as the
  sparse/spiky-corridor caveat above, just on the test side; don't over-read one backtest on a
  thin corridor as the model's definitive accuracy.

## Verification practice

This dashboard has caught real bugs (native HTML5 validation silently blocking submission,
tooltip clipping, Plotly legend wrapping) that only surfaced by **actually driving it in a
browser**, never by reading the code or a static screenshot alone. Don't skip this step for a UI
change here:

- **Playwright**, `p.chromium.launch()` with no `executable_path` override -- the `playwright`
  Python package is in `.venv`, but the browser binary itself is a separate download
  (`python -m playwright install chromium`) cached under
  `C:\Users\Acer\AppData\Local\ms-playwright\` and versioned by Playwright's own revision number
  (changes across `playwright` package upgrades) -- don't hardcode a specific revision path in a
  script; the plain default launch call resolves whatever's currently installed.
- Check `page.on("console", ...)`/`page.on("pageerror", ...)` on every check, not just a
  screenshot -- several real bugs threw no visible error but did log a console message.
- For a "does X actually work" question (not just "does it look right"), assert on real state --
  e.g. read `el._fullLayout.xaxis.range` before/after a drag gesture to confirm zoom actually
  changed the view, not just that a screenshot looks plausible.
- Check both `color_scheme="light"` and `color_scheme="dark"` (Playwright's `new_page(...,
  color_scheme=...)`) for anything chart- or color-related.
- Lint after any Python change: `ruff check app/` and `flake8 app/` (config in
  `../pyproject.toml`/`../.flake8`, kept in sync) -- both must stay clean.

## Where things live

- `app/static/dashboard/index.html` -- the whole dashboard (HTML + CSS + JS in one file).
- `app/static/dashboard/plotly-cartesian.min.js` -- self-hosted Plotly.js.
- `app/api/routes_forecast.py`, `app/api/schemas.py` -- `POST /forecast/run`,
  `POST /forecast/baseline-suggestion`, `POST /forecast/evaluate`, and their request/response
  models.
- `app/forecasting/engine.py` -- `TreasuryEngine` (`prepare_data`, `train_forecast`,
  `simulate_liquidity`, `summarize`, `suggest_baseline`, `evaluate_accuracy`, `run`).
- `app/forecasting/charts.py` -- renders Prophet's trend decomposition as Plotly figure JSON.
- `app/README.md` -- full architecture, request flow, and the reasoning behind every decision
  summarized above.
- `notes.txt` -- project phasing and a dated history of what was built/found/fixed and why.
