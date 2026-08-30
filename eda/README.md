# EDA: Business_Report.csv

Scratch data-science work, separate from the (still-undecided) Phase 1 app structure in
`notes.txt` at the repo root.

## Tooling decision

Originally planned to use `ydata-profiling` (per user request). It doesn't support Python 3.14
(this project's `.venv`) — its newest release, 4.18.4, requires `Python <3.14`, and
`pip install ydata-profiling` fails outright (`No matching distribution found`). Per the fallback
chain in the plan, switched to **sweetviz**, which installed cleanly and produces the same kind of
single-file HTML EDA report (distributions, missing values, pairwise associations, alerts).

If a future Python downgrade or an ydata-profiling 3.14 release becomes available, revisit —
sweetviz's report is less detailed (no per-pair interaction scatterplots, simpler correlation
view) than ydata-profiling's.

## Usage

```bash
python eda/generate_eda_report.py                      # full dataset, full report
python eda/generate_eda_report.py --sample 100000       # fast dev run on a random sample
python eda/generate_eda_report.py --minimal             # skip pairwise associations (large-data mode)
```

Output HTML is written into this folder; the filename encodes whether the run was sampled and/or
minimal (e.g. `Business_Report_EDA.html` vs. `Business_Report_EDA_sample100000.html`).

## Runs so far

- `--sample 100000` (seed 42): completed in ~45s, produced a ~1.2 MB HTML report
  (`Business_Report_EDA_sample100000.html`). Used to validate the script/dtypes/config before
  committing to the full file.
- Full 1,430,655-row run (`pairwise_analysis=auto`): completed in ~2 min, produced
  `Business_Report_EDA.html` (~1.1 MB).

## Findings that fed back into the repo's CLAUDE.md

The full-file run surfaced real cardinality that the original schema notes (written from a
~200k-row partial sample) had undercounted:

- `transstatus` has **11** distinct values, not 3 — `Payment`/`Cancel`/`Block` plus 8 more granular
  states (`PAYOUTPROCESSING`, `FAILED`, `INITIALIZED`, `RETURNED`, `PAYOUTFAIL`, `Compliance`,
  `OFAC`, `EXPIRED`), all rare but real.
- `Sending_Country`: 24 distinct (not ~16). `Receiver_Country`: 71 distinct (not ~46+).
- `Transaction_Method`: 178 distinct (not ~80+).

See the root `CLAUDE.md` schema table for the corrected figures.
