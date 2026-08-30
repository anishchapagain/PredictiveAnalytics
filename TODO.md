# TODO

Action items to raise this project from "well-built Phase 1 prototype" toward
production-ready, grouped by leverage. Each one traces back to a specific, real gap
found during actual review/verification work (not speculative nice-to-haves) — see
`notes.txt` for the fuller reasoning/history behind each and the still-open **decisions**
(DB choice, deployment target, etc.) this list assumes get made along the way. This file
is the actionable checklist; `notes.txt` is the narrative/decision log — keep both in
sync rather than letting one drift.

Nothing here is scheduled — pull items in whatever order actually matters next.

## Tier 1 — highest leverage (do these first; they compound on everything else)

- [x] **Forecast accuracy evaluation (backtest).** Done 2026-08-27 —
  `TreasuryEngine.evaluate_accuracy()` in `app/forecasting/engine.py`, a new
  `POST /api/v1/forecast/evaluate` endpoint (`EvaluationRequest`/`EvaluationResponse` in
  `app/api/schemas.py`), and a new "Evaluate forecast accuracy" card in the dashboard.
  - Method: a single train/test holdout, not Prophet's own rolling-origin
    `cross_validation()`/`performance_metrics()` — that refits repeatedly and can take
    minutes on one corridor; a single holdout costs roughly one extra `/run` (~2s warm
    cache, confirmed against the live server), appropriate for an interactive button.
    `TreasuryEngine.train_forecast()` gained an optional `horizon_days` override
    (backward-compatible, defaults to the config's own value) so the backtest fit can
    project forward exactly as many days as it held out.
  - Reports: MAPE (excludes zero-actual test days — percentage error is undefined there,
    and real thin corridors do have true zero-volume days; the exclusion count is
    reported, not silently dropped), RMSE (covers every day, no such blind spot),
    `interval_coverage_pct` (actual vs. `[yhat_lower, yhat_upper]`, checked against the
    configured `interval_width`), `safety_stock_coverage_pct` (actual vs. the real
    funding recommendation including any buffer — the more directly actionable number),
    and a naive-baseline comparison (flat trailing-average prediction, same method as
    `suggest_baseline(method="average")`) so a caller can see whether Prophet is actually
    earning its keep.
  - Verified live against the real CSV, not just the synthetic fixture: on
    NEPAL/receiver_country with a 14-day holdout, interval coverage came back well below
    the configured 80% target and Prophet lost to the naive baseline on both MAPE and
    RMSE — a real, useful finding (not a bug), and exactly the kind of thing this feature
    exists to surface.
  - 10 new tests (5 engine-level in `tests/test_engine.py`, 5 API-level in
    `tests/test_api_forecast.py`) — 48 total, all passing. Found and fixed one real
    self-consistency bug in the process: `within_interval`/`within_safety_stock` and the
    two coverage percentages were originally computed from unrounded floats while the
    response displays 2-decimal-rounded values, so a boundary case could show e.g.
    "actual: 205.0, upper: 205.0, within_interval: false" — fixed by rounding first, then
    comparing/aggregating on the rounded values, so the response is always internally
    consistent with what it displays. (Also hit, and fixed, two bugs in the *tests*
    themselves — a zero-actual day placed on the very last day of a filter's history
    doesn't reappear as a zero-fill row, since `prepare_data()`'s reindex only spans
    `[min(present dates), max(present dates)]`; and an exact-equality assertion on two
    independently-rounded values needed a cent of tolerance instead.)
  - Dashboard: `buildEvalRequestBody()`/`buildModelConfigFields()` (the latter shared
    with the existing forecast-run request builder, so both reflect identical model
    settings), 5 stat tiles (MAPE, RMSE, interval coverage, safety-stock coverage, vs.
    naive baseline — the last three as 3-tier good/warning/critical badges via a new
    shared `makeBadge()` helper, refactored out of the existing shortfall badge), and an
    actual-vs-forecast Plotly chart over the held-out window (same shaded-interval-band
    technique as the funding-gap chart, red markers on days outside the interval).
    Verified with Playwright in both light and dark themes (zero console errors), plus
    the error path (`eval_days` out of range) and info-tooltip wiring.
  - Docs: `app/README.md` ("The engine", API table, "Dashboard", "Known caveats" all
    updated), `CLAUDE.md`'s Architecture section, this file, `docs/csv-to-forecast-
    architecture.html` (new evaluation section/diagram, republished), and the User
    Manual (new Section 13 "Evaluating Forecast Accuracy (Backtest)" in both
    `docs/treasury-dashboard-user-manual.html` and its Design-canvas source,
    `docs/user-manual-design/Main.dc.html` — see `notes.txt` for a mistake made and
    fixed while republishing the latter's live canvas artifact).
- [x] **A `pytest` suite around `TreasuryEngine`'s pure functions, plus full API-level
  tests.** Done 2026-08-27 — 38 tests, `tests/` at repo root, ~5s to run, zero
  dependency on the real CSV (see below). `pytest`/`httpx` added to
  `requirements-dev.txt`.
  - `tests/conftest.py` — a small, fully synthetic, deterministically-seeded fixture
    dataset (120 days, two real corridors — AUSTRALIA/UAE → NEPAL, reusing
    `business_data_send.py`'s own original corridor) wired in via FastAPI's
    `dependency_overrides` on `get_repository`. Deliberately **not** the real
    `Business_Report.csv` (233MB, gitignored) — a fresh clone or CI runner won't have
    that file, so any test depending on it would only ever pass on this one machine.
    Real (not placeholder) country names specifically so holiday-resolution tests are
    meaningful — `resolve_country_code()` needs something `pycountry`'s fuzzy search can
    actually match.
  - `tests/test_engine.py` — `prepare_data`'s gap-filling/status-filtering/`ValueError`,
    `simulate_liquidity`'s exact formula (`safety_stock`, `daily_shortfall`,
    `cumulative_shortfall`, `liquidity_to_volume_ratio` incl. the `yhat==0` → NaN case)
    via a fully controlled DataFrame with no Prophet involved at all, `summarize`'s
    empty-horizon guard, `suggest_baseline`'s median-resists-a-spike behavior.
  - `tests/test_api_forecast.py` / `test_api_meta.py` / `test_api_system.py` — real
    `TestClient` calls against every endpoint: success shapes, the 422/503 exception-
    hierarchy mapping, the by-Dimension holiday-resolution behavior (receiver_country →
    receiver only, corridor → both, agent → neither) locked in as a regression test
    after being found and documented by hand in the user manual, `/health` vs `/ready`
    liveness-vs-readiness distinction, dataset-summary exact-value checks.
  - `tests/test_openapi.py` — automates what had been a manual per-change audit all
    session: live `/openapi.json` is valid (`openapi_spec_validator`), every schema
    field has a `description`, every operation has a `summary`/`tags`/`operationId`.
  - Real bug caught by the suite on its first run (not a pre-existing app bug --  a bug
    in the test itself, which is exactly what a test suite is for): a baseline-window
    test used `window_days=99999`, forgetting the field's own `le=365` cap -- fixed to
    use `365` instead, same clamping behavior demonstrated either way.
  - Not done here, deliberately deferred as separate follow-ups: `pytest-cov`/coverage
    reporting (gitignore patterns for it already added pre-emptively), and any test that
    needs the *real* CSV (a `slow` marker is reserved for that in `pyproject.toml` but
    unused so far).
- [x] **`git init`.** Done 2026-08-27 — `main` branch, root commit `61b9edd` (46 files,
  35,829 lines). `.gitignore` excludes `Business_Report.csv` (233MB — exceeds GitHub's
  100MB hard limit), `.env` (replaced by a committed `.env.example`), `.venv/`, `logs/`,
  `*.log`, `.ruff_cache/`, `.pytest_cache/`. `.gitattributes` added at the same time to
  normalize line endings to LF in the repo (added *before* the first commit specifically
  so it wouldn't need a disruptive reformat-everything commit later). Verified every
  exclusion with `git check-ignore -v` before committing, not just assumed from
  `git status`; `.git` is 2.7MB after the commit, confirming the CSV never touched it.
- [x] **CI.** Done 2026-08-27 — `.github/workflows/ci.yml`, `ubuntu-latest`, Python 3.14
  (matching the project's actual runtime, not a speculative version matrix — this is an
  application pinned to one interpreter, not a library). Runs on push/PR to `main` plus
  a manual `workflow_dispatch`: `ruff check app/ tests/`, `flake8 app/ tests/`,
  `pytest tests/ -v` (which already includes live OpenAPI-schema validation via
  `tests/test_openapi.py` — no separate step needed for that). Needs no data-
  provisioning step since the test suite never touches the real CSV. YAML validity and
  every command verified to actually pass, run verbatim as the workflow specifies them,
  not just assumed from having run similar commands before.
  **One real gap, disclosed rather than hidden**: this only runs once pushed to an
  actual GitHub remote — none is configured yet (this repo is local-only so far). Also
  genuinely unverified: whether `pip install prophet` bootstraps a working CmdStan
  binary cleanly on a truly fresh machine with no pre-existing venv — this project's own
  `.venv` already had it working before CI was ever written, so that specific first-run
  path has never actually been observed here. Should be fine (modern `prophet`/
  `cmdstanpy` auto-installs CmdStan on first use) but the honest status is "expected to
  work, not observed to work" until the first real push triggers an actual run.

## Tier 2 — structure

- [ ] Formalize the user-manual regeneration step (`docs/user-manual-design/Main.dc.html`
  → `docs/treasury-dashboard-user-manual.html`) as a real checked-in script instead of a
  scratchpad one-off rebuilt each session — removes the "did I forget to regenerate"
  risk that two-file pattern currently carries.

## Tier 3 — code quality

- [ ] **Resolve `growth: "logistic"` one way or the other.** Right now it's a permanently
  broken `Literal` option on `ForecastRequest` — deliberately excluded from the
  dashboard's dropdown but still reachable via the raw API, where it always 500s (no
  `cap` column is ever set). Either implement real `cap` support, or drop it from the
  schema entirely so the OpenAPI contract doesn't advertise a guaranteed failure.
- [ ] **Fix the concurrent cold-start CSV re-parse.** `CSVDataRepository.load()` has no
  lock around its mtime-check-then-read sequence, so several meta endpoints hit at once
  on a cold cache (exactly what the dashboard's own startup fetch does) each
  independently re-parse the full 233MB file in parallel instead of one loading while the
  others wait and reuse the result. An `asyncio.Lock` around `load()` fixes it. Only
  matters on a cold start; once cached it's instant either way.
- [ ] **Add a static type checker** (mypy or pyright) to the lint pass. Type hints are
  already used well throughout (`Literal`, `str | None`, dataclasses, PEP 695 generics)
  but nothing currently enforces they're internally *consistent* — a different bug class
  than what `ruff`/`flake8` catch.

## Tier 4 — app / product completeness

- [ ] **Auth.** Even a minimal API key or single-user login. The API's own OpenAPI
  description already states outright not to expose this without protection — right now
  that's a warning, not a safeguard.
- [ ] **A deployment story.** A `Dockerfile` plus an actual target decision (still open —
  see `notes.txt`). `uvicorn --reload` on a dev machine is the only way this has ever run.
- [ ] **Replace CSV-in-memory with DuckDB + Parquet.** Supersedes the earlier
  "implement `PostgresDataRepository`" framing — no live Postgres is available, and on
  reflection Postgres was never actually the requirement, just the assumed vehicle for
  fixing the same real ceiling (single point of failure, full 233MB re-parse on every
  cache miss). DuckDB is a better *architectural* fit anyway (embedded, zero server,
  columnar/analytical — matches this project's actual query shape better than a
  row-oriented RDBMS would), and doesn't foreclose Postgres later since
  `DataRepository` stays the same interface either way.
  **Full design doc, with diagrams comparing both architectures and specifically how
  adding historic/newest data works under each**:
  `docs/csv-to-duckdb-migration-design.html` (also published at
  <https://claude.ai/code/artifact/09a93abd-e461-4da7-bcfb-9ee0ea63df87>).
  Plan (from that doc): Phase 1 — convert to `data/*.parquet`, build
  `DuckDBDataRepository` behind the unchanged `load() -> pd.DataFrame` contract,
  dedup by `Control_No`, cache-invalidate off the folder's file listing; the existing
  38-test suite should pass unmodified as the actual proof nothing above the interface
  moved. Phase 2 (separate, optional) — push the meta endpoints' `groupby`/`nunique`
  down into SQL for the deepest performance win. Not yet implemented — this entry is
  the design, not the build.
- [ ] **A caller/actor field in request logs.** Flagged as a real, known gap in
  `CLAUDE.md` itself — relevant the moment more than one person/system calls this API.
- [ ] **Rate limiting / basic abuse protection**, once this is ever reachable beyond
  localhost — not needed before then, but worth doing before auth alone is trusted to
  carry that weight.
