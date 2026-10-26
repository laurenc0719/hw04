# hw04 — Cleaning the Wildcat Capital Data
MIS3060 Business Intelligence with AI | Villanova University

## Structure

```
hw04/
├── README.md                    this file
├── decision_log.md              Part B: one entry per issue (evidence, dimension, decision, justification, risk)
├── requirements.txt             Python packages (pandas, numpy)
├── run_all.bat                  one-click runner for Windows (runs all three scripts)
├── scripts/
│   ├── 01_profile.py            Part A: generic profile + data-quality checks (4 groups, ERROR/WARN/INFO)
│   ├── 02_clean.py              Part C: one asserted transformation at a time, quarantine, audit trail
│   └── 03_validate.py           Part D: referential integrity, rules, reconciliations, IQR, scorecard
├── data/
│   ├── clean/                   the 8 clean tables (same names/columns + 0/1 *_flag columns)
│   │   └── README_clean.md      relationships, BI column types, flag columns, known limitations
│   └── audit/
│       ├── cleaning_log.csv     table, issue, action, rows affected, note
│       ├── value_changes.csv    table, key column, key, column, old value, new value
│       └── quarantine_*.csv     one file per quarantined set (duplicate clients, orphan transactions)
└── reports/
    ├── profile_raw.csv          Part A: one row per table x column
    ├── dq_summary_raw.csv       Part A: every check, tagged ERROR / WARN / INFO / PASS
    ├── data_quality_scorecard.csv  Part D: one row per table per stage (raw / clean)
    ├── reconciliations.csv      Part D: recomputed vs stored amounts, facts vs dimensions (raw / clean)
    ├── iqr_outlier_summary.csv  Part D: IQR fences and flag counts per column (raw / clean)
    └── cleaning_report.md       Part E: report for the portfolio committee
```

The eight raw Wildcat Capital CSVs are **not** in this repo. Put them in this folder (or a `Wildcat Capital/` subfolder), or pass `--data-dir`.

## How to run

**Requirements:** Python 3.9+ and the packages in `requirements.txt` (pandas, numpy).

### Option A: one click (Windows)
Double-click **`run_all.bat`** (or type `run_all.bat` in a terminal opened in this folder).
On the first run it creates a private environment in `.venv` and installs `requirements.txt`. Then it runs the three scripts in order and stops at the first failure. Later runs reuse the environment.

### Option B: any terminal (Windows / Mac / Linux)
```bash
cd hw04
python -m venv .venv
# Windows:      .venv\Scripts\activate
# Mac / Linux:  source .venv/bin/activate
pip install -r requirements.txt
python scripts/01_profile.py      # Part A: audit
python scripts/02_clean.py        # Part C: cleaning
python scripts/03_validate.py     # Part D: validation + scorecard
```

If the CSVs live somewhere else:
```bash
python scripts/01_profile.py  --data-dir "path/to/Wildcat Capital"
python scripts/02_clean.py    --data-dir "path/to/Wildcat Capital"
python scripts/03_validate.py --data-dir "path/to/Wildcat Capital"
```

> Run the commands from a terminal rather than VS Code's ▶ button: the button may use a different Python interpreter that doesn't have pandas installed. If a package is missing, each script prints the exact install command for the Python that ran it.

### Outputs
| Script | Writes |
|---|---|
| `scripts/01_profile.py` | `reports/profile_raw.csv`, `reports/dq_summary_raw.csv` |
| `scripts/02_clean.py` | `data/clean/` (8 tables + `README_clean.md`); `data/audit/cleaning_log.csv`, `data/audit/value_changes.csv`, `data/audit/quarantine_*.csv`. Stops with an `AssertionError` if any fix does not do what it claims. Raw files are never modified (checked by hash). |
| `scripts/03_validate.py` | `reports/data_quality_scorecard.csv`, `reports/reconciliations.csv`, `reports/iqr_outlier_summary.csv`; plus row-level detail (`ri_results.csv`, `rule_results.csv`, `iqr_outlier_flags.csv`) that is regenerated on every run and not committed. |

### IQR outliers: does "flagged" mean "wrong"?
No. IQR flags are run on `aum`, transaction `amount` and `initial_aum`, columns with no natural upper bound. 99% of the high-AUM flags belong to UHNW clients, whose $5M+ balances are real but sit far above the median; a flag only says a value is unusual for its column and is a prompt to investigate, not a reason to change or delete it. (The same sentence, computed from the data, is in `reports/iqr_outlier_summary.csv`.)

## Claude Code review findings

| Script | What Claude Code flagged | What I did about it |
|---|---|---|
| `scripts/01_profile.py` | _to fill in_ | _to fill in_ |
| `scripts/02_clean.py` | _to fill in_ | _to fill in_ |
| `scripts/03_validate.py` | _to fill in_ | _to fill in_ |
