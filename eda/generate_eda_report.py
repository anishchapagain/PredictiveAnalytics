"""Generate an HTML exploratory-data-analysis report for Business_Report.csv.

Uses sweetviz rather than ydata-profiling: ydata-profiling has no release supporting
Python 3.14 (this project's .venv) as of writing -- its newest release (4.18.4) requires
Python <3.14. Sweetviz produces the same kind of single-file HTML EDA report with a much
lighter, newer-Python-friendly dependency set.

Usage:
    python eda/generate_eda_report.py                     # full dataset, full report
    python eda/generate_eda_report.py --sample 100000      # fast dev run on a random sample
    python eda/generate_eda_report.py --minimal            # skip pairwise associations (large-data mode)
    python eda/generate_eda_report.py --sample 100000 --minimal

Output is written next to this script, in eda/, with a filename that encodes whether the
run was sampled and/or minimal so outputs are never ambiguous.
"""

import argparse
from pathlib import Path

import pandas as pd
import sweetviz as sv

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "Business_Report.csv"
OUTPUT_DIR = Path(__file__).resolve().parent

DTYPES = {
    "Control_No": "string",
    "Agent_Name": "string",
    "Transaction_Method": "string",
    "Payment_Type": "category",
    "Sending_Country": "category",
    "Sending_Country_Currency": "category",
    "Receiver_Country": "category",
    "Payout_Currency": "category",
    "Transaction_Amount_USD": "float64",
    "transstatus": "category",
    "Turn_Around_Time_Hours": "float64",
}
DATE_COLUMNS = ["TRN_Date", "Paid_Date"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Random sample size to profile instead of the full dataset (dev/iteration runs).",
    )
    parser.add_argument(
        "--minimal",
        action="store_true",
        help="Skip sweetviz's pairwise association analysis (large-data / fast mode).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed used when --sample is given (default: 42).",
    )
    return parser.parse_args()


def load_data(sample: int | None, seed: int) -> pd.DataFrame:
    print(f"Loading {CSV_PATH} ...")
    df = pd.read_csv(
        CSV_PATH,
        dtype=DTYPES,
        parse_dates=DATE_COLUMNS,
    )
    print(f"Loaded {len(df):,} rows, {len(df.columns)} columns.")
    if sample is not None and sample < len(df):
        df = df.sample(n=sample, random_state=seed).reset_index(drop=True)
        print(f"Sampled down to {len(df):,} rows (seed={seed}).")
    return df


def build_output_path(sample: int | None, minimal: bool) -> Path:
    suffix = ""
    if sample is not None:
        suffix += f"_sample{sample}"
    if minimal:
        suffix += "_minimal"
    return OUTPUT_DIR / f"Business_Report_EDA{suffix}.html"


def main() -> None:
    args = parse_args()
    df = load_data(args.sample, args.seed)

    pairwise = "off" if args.minimal else "auto"
    print(f"Building sweetviz report (pairwise_analysis={pairwise}) ...")
    report = sv.analyze(df, pairwise_analysis=pairwise)

    output_path = build_output_path(args.sample, args.minimal)
    report.show_html(filepath=str(output_path), open_browser=False)
    print(f"Wrote report to {output_path}")


if __name__ == "__main__":
    main()
