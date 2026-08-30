# Walkthrough: Remittance Feature Engineering, EVAL Results & August 2026 Forecasts

The notebook [remittance_volume_forecasting_feature_engineering.ipynb](file:///d:/RandD/iSendAnalytics/remittance_volume_forecasting_feature_engineering.ipynb) has been executed against the full **`Business_Report.csv`** dataset (1,430,655 remittance transactions from `2024-01-01` to `2026-07-31`).

---

## 1. Actual Execution Results & Model EVAL Scorecard (July 2026 Holdout)

The evaluation holdout was run on the 31 actual days of **July 2026 (2026-07-01 to 2026-07-31)**, trained on history up to `2026-06-30`:

| Metric | Prophet (with Regressors) | LightGBM (Feature-Rich GBDT) | Ensemble (Prophet + LightGBM) | Best Performer |
| :--- | :---: | :---: | :---: | :---: |
| **WAPE (%)** (Weighted Error) | 73.37% | **44.03%** | 53.55% | **LightGBM** ($-29.34\%$ lower error) |
| **MAPE (%)** (Non-zero Days) | 174.62% | **107.57%** | 137.28% | **LightGBM** |
| **MAE ($)** (Mean Absolute Error) | \$118,154.54 | **\$70,910.09** | \$86,244.59 | **LightGBM** |
| **RMSE ($)** (Root Mean Sq Error)| \$133,269.55 | **\$79,565.56** | \$100,829.67 | **LightGBM** |
| **R² Score** (Variance Explained) | -0.251 | **0.554** | 0.284 | **LightGBM** ($55.4\%$ variance explained) |
| **Directional Accuracy (%)** | 56.67% | 56.67% | **60.00%** | **Ensemble** |
| **Safety Stock Coverage (%)** | **96.8%** | N/A | **96.8%** | **Prophet** (Exceeds $95\%$ target) |

### Key Takeaways from the EVAL:
1. **LightGBM Outperformed in Point Accuracy:** Benefited significantly from the engineered non-leaking lags ($t-1, t-7, t-28, t-365$), 7d/30d rolling statistics, and growth velocity metrics, achieving a **44.03% WAPE** compared to Prophet's 73.37%.
2. **Prophet Provided Robust Risk Provisioning:** Its 95% Bayesian upper bound ($yhat_{upper}$) successfully covered **96.8%** of all real-world demand spikes in the holdout.
3. **Ensemble Delivered Highest Directional Accuracy:** Combining both models achieved **60.0%** day-over-day directional accuracy.

---

## 2. August 2026 Daily Projections (Receiver Country: Nepal)

*Full refit on 2024-01-01 to 2026-07-31. Sample First 7 Days of August 2026:*

| Date | Day | Salary Week? | Prophet ($yhat$) | LightGBM ($yhat$) | Ensemble ($yhat$) | Prophet 95% Safety Stock | Daily Shortfall vs Baseline |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **2026-08-01** | Saturday | **Yes** | \$136,434.98 | \$139,870.46 | **\$138,152.72** | \$317,743.70 | \$133,337.39 |
| **2026-08-02** | Sunday | **Yes** | \$229,237.77 | \$211,653.29 | **\$220,445.53** | \$410,493.19 | \$226,086.88 |
| **2026-08-03** | Monday | **Yes** | \$156,986.10 | \$172,751.28 | **\$164,868.69** | \$326,738.88 | \$142,332.57 |
| **2026-08-04** | Tuesday | **Yes** | \$164,728.52 | \$177,223.39 | **\$170,975.96** | \$346,316.46 | \$161,910.15 |
| **2026-08-05** | Wednesday | **Yes** | \$166,631.76 | \$195,328.51 | **\$180,980.14** | \$348,499.07 | \$164,092.76 |
| **2026-08-06** | Thursday | No | \$172,899.65 | \$216,417.00 | **\$194,658.32** | \$360,111.45 | \$175,705.14 |
| **2026-08-07** | Friday | No | \$169,494.59 | \$242,542.75 | **\$206,018.67** | \$351,169.30 | \$166,762.99 |

- **Total Projected Volume (August 2026):** **\$5,716,595.49**
- **Peak Single-Day Safety Stock Required:** **\$420,617.26** (Sunday, August 9)
- **Baseline Funding Level:** **\$184,406.31/day**

---

## 3. Multi-Hierarchy Projections

The notebook also executed August 2026 projections for:
- **Corridor Level:** `HONG KONG -> INDIA`
- **Agent Level:** `EPAY LIMITED`

All cells have executed cleanly, and the notebook with embedded plots, scorecards, and forecast dataframes is saved at [remittance_volume_forecasting_feature_engineering.ipynb](file:///d:/RandD/iSendAnalytics/remittance_volume_forecasting_feature_engineering.ipynb).
