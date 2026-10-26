#!/usr/bin/env python3
"""
scripts/01_profile.py  --  HW4 Part A: data-quality audit (profile + rule checks)
MIS3060 Business Intelligence with AI | Wildcat Capital

WHAT IT DOES
  1. Reads every table AS TEXT (dtype=str, keep_default_na=False) so nothing is
     silently converted before we look at it.
  2. Profiles EVERY column of EVERY table with one generic function
     (profile_column) - works on any CSV, no per-table code:
        null/blank count, null-like tokens ("None", "NULL", ...), distinct count,
        dominant value shapes, share that fails to parse as the expected type,
        whitespace problems, mixed formats, case variants, leading zeros,
        numeric min/max/mean/median, date min/max, IQR outlier count.
  3. Runs checks in four groups and tags every finding ERROR / WARN / INFO:
        1 Column level        type, allowed values, patterns, ranges, key uniqueness
        2 Single-table rules  duplicates, near-duplicates, row-level contradictions
        3 Referential         every FK must point to an existing PK
        4 Cross-table rules   facts vs dimensions, facts vs facts
     Checks that ran and found nothing are written with severity PASS so the
     report shows full coverage.
  4. Saves reports/profile_raw.csv and reports/dq_summary_raw.csv

SEVERITY
  ERROR  wrong, or will break a join or calculation
  WARN   probably wrong - investigate
  INFO   worth knowing (often legitimate structure, e.g. blanks that are expected)
  PASS   check ran, nothing found

GENERIC DESIGN
  The engine (profile_column, generic column checks, duplicate/near-duplicate
  checks, ri_check, functional_dependency, ...) takes any DataFrame. The
  business knowledge about the Wildcat tables lives in the CONFIG section
  (SCHEMA, FOREIGN_KEYS, row rules, cross-table rules) as data, not as code
  paths. A table that is not in SCHEMA still gets the full profile, inferred
  types, all generic column findings, and exact-duplicate checks.

USAGE  (from the hw04 folder, venv active)
  python scripts/01_profile.py
  python scripts/01_profile.py --data-dir "Wildcat Capital"
  python scripts/01_profile.py --data-dir some/folder --out-dir reports
  python scripts/01_profile.py --files path/a.csv path/b.csv    # any CSVs

Requires: pandas >= 1.5, numpy
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
except ModuleNotFoundError as _e:   # friendly message instead of a traceback
    _req = Path(__file__).resolve().parent.parent / "requirements.txt"
    _cmd = f'"{sys.executable}" -m pip install -r "{_req}"'
    if sys.platform.startswith("win"):
        _fix = (f"    PowerShell (VS Code default):  & {_cmd}\n"
                f"    Command Prompt:                {_cmd}\n")
    else:
        _fix = f"    {_cmd}\n"
    sys.exit(
        f"\nMissing Python package: {_e.name}\n"
        f"The Python that ran this script does not have the required packages:\n"
        f"    {sys.executable}\n\n"
        f"EASIEST FIX (Windows): run  run_profile.bat  in the hw04 folder - it sets\n"
        f"everything up and runs the script.\n\n"
        f"Or install the packages into exactly that Python (copy the whole line):\n"
        f"{_fix}"
        f"then run the script again with that same Python.\n")

# =============================================================================
# 0. CONFIG
# =============================================================================
ROOT = Path(__file__).resolve().parent.parent          # hw04/
DEFAULT_OUT_DIR = ROOT / "reports"
# Searched in order; the first folder that contains at least one of the
# Wildcat files is used (unless --data-dir / --files is given).
DEFAULT_DATA_DIRS = [ROOT / "data" / "raw", ROOT / "Wildcat Capital",
                     ROOT / "02_Data" / "Raw", ROOT]

WILDCAT_TABLES = ["dim_advisor", "dim_benchmark", "dim_client", "dim_date",
                  "dim_security", "fact_fees", "fact_portfolio_snapshot",
                  "fact_transactions"]

NULL_TOKENS = {"NULL", "NONE", "NAN", "N/A", "NA", "#N/A", "NIL", "NONE.",
               "-", "--", "?", "MISSING", "UNKNOWN", "(BLANK)"}
TYPE_INFER_THRESHOLD = 0.90      # >= 90% of non-blank values parse -> that type
HEAPING_MIN_COUNT = 25           # repeated value in a near-unique numeric column
DATA_PERIOD_END = "2024-12-31"   # end of the extract (dim_date covers 2020-2024)

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]
CERTS = "(?:CFA|CFP|CIMA|CPA|ChFC|CPWA|CAIA|CLU)"

# Column spec keys:
#   type      int | float | date | bool | str
#   required  True -> blanks are a finding (required_severity, default WARN;
#             key columns are always ERROR)
#   allowed   list of permitted values (blanks/null tokens checked separately)
#   pattern   regex the whole stripped value must match
#   min/max   inclusive bounds; gt = strictly greater than
#   range_severity  severity for out-of-range (default WARN)
#   unique    natural key -> must be unique
#   true/false  literal values for bool columns
SCHEMA: dict[str, dict] = {
    "dim_advisor": {
        "pk": "advisor_id", "expected_rows": 25,
        "columns": {
            "advisor_id": {"type": "int"},
            "first_name": {"type": "str", "required": True},
            "last_name": {"type": "str", "required": True},
            "region": {"type": "str", "required": True,
                       "allowed": ["Northeast", "Mid-Atlantic", "Southeast",
                                   "Midwest", "Southwest", "West"]},
            "years_experience": {"type": "int", "required": True, "min": 0, "max": 60},
            "aum_target_mm": {"type": "float", "required": True, "gt": 0, "max": 10000},
            "certifications": {"type": "str",
                               "pattern": rf"No Certification|{CERTS}(?:, {CERTS})*"},
        },
        "near_duplicates": [{"block": [], "compare": ["first_name", "last_name"],
                             "threshold": 0.90, "severity": "WARN"}],
    },
    "dim_benchmark": {
        "pk": "benchmark_id", "expected_rows": 8,
        "columns": {
            "benchmark_id": {"type": "int"},
            "name": {"type": "str", "required": True, "unique": True},
            "asset_class": {"type": "str", "required": True,
                            "allowed": ["Equity", "Fixed Income", "Multi-Asset",
                                        "International", "Small Cap", "Emerging", "Cash"]},
            "ticker_proxy": {"type": "str", "required": True, "unique": True,
                             "pattern": r"[A-Z0-9_]{1,12}"},
        },
    },
    "dim_client": {
        "pk": "client_id", "expected_rows": 2525,
        "columns": {
            "client_id": {"type": "int"},
            "first_name": {"type": "str", "required": True},
            "last_name": {"type": "str", "required": True},
            "age": {"type": "int", "required": True, "min": 18, "max": 110},
            "city": {"type": "str", "required": True},
            "state": {"type": "str", "required": True, "allowed": sorted(US_STATES),
                      "allowed_severity": "ERROR"},
            "zip_code": {"type": "str", "required": True, "pattern": r"\d{5}",
                         "pattern_severity": "ERROR"},
            "risk_profile": {"type": "str", "required": True,
                             "allowed": ["Conservative", "Moderately Conservative",
                                         "Balanced", "Moderately Aggressive", "Aggressive"]},
            "client_tier": {"type": "str", "required": True,
                            "allowed": ["Retail", "HNW", "UHNW"]},
            "primary_account_type": {"type": "str", "required": True,
                                     "allowed": ["Taxable", "IRA", "Roth IRA", "Trust", "529"]},
            "initial_aum": {"type": "float", "required": True, "gt": 0},
            "client_since": {"type": "date", "required": True,
                             "min": "1950-01-01", "max": DATA_PERIOD_END},
            "advisor_id": {"type": "int", "required": True},
            "is_active": {"type": "bool", "required": True,
                          "true": ["True"], "false": ["False"]},
        },
        "near_duplicates": [
            # same ZIP, very similar full name
            {"block": ["zip_code"], "compare": ["first_name", "last_name"],
             "threshold": 0.85, "severity": "WARN"},
            # same first name + age, similar last name (catches truncated surnames)
            {"block": ["first_name", "age"], "compare": ["last_name"],
             "threshold": 0.85, "severity": "WARN"},
        ],
    },
    "dim_date": {
        "pk": "date_id", "expected_rows": 1827,
        "columns": {
            "date_id": {"type": "int"},
            "date": {"type": "date", "required": True, "unique": True},
            "year": {"type": "int", "required": True, "min": 1900, "max": 2100},
            "quarter": {"type": "int", "required": True, "min": 1, "max": 4,
                        "range_severity": "ERROR"},
            "month": {"type": "int", "required": True, "min": 1, "max": 12,
                      "range_severity": "ERROR"},
            "month_name": {"type": "str", "required": True, "allowed": MONTH_NAMES},
            "week_of_year": {"type": "int", "required": True, "min": 1, "max": 53,
                             "range_severity": "ERROR"},
            "day_of_week": {"type": "str", "required": True, "allowed": DAY_NAMES},
            "is_month_end": {"type": "bool", "required": True, "true": ["1"], "false": ["0"]},
            "is_quarter_end": {"type": "bool", "required": True, "true": ["1"], "false": ["0"]},
            "is_year_end": {"type": "bool", "required": True, "true": ["1"], "false": ["0"]},
            "is_trading_day": {"type": "bool", "required": True, "true": ["1"], "false": ["0"]},
        },
    },
    "dim_security": {
        "pk": "security_id", "expected_rows": 500,
        "columns": {
            "security_id": {"type": "int"},
            "ticker": {"type": "str", "required": True, "unique": True,
                       "pattern": r"[A-Z]{1,5}"},
            "name": {"type": "str", "required": True},
            "asset_class": {"type": "str", "required": True,
                            "allowed": ["Equity", "Fixed Income", "Alternative",
                                        "Cash Equivalent"]},
            # sector is legitimately blank for ETFs -> conditional rule below
            "sector": {"type": "str",
                       "allowed": ["Communication Services", "Consumer Discretionary",
                                   "Consumer Staples", "Energy", "Financials",
                                   "Healthcare", "Industrials", "Materials",
                                   "Real Estate", "Technology", "Utilities"]},
            "benchmark_id": {"type": "int", "required": True},
            "is_etf": {"type": "bool", "required": True, "true": ["True"], "false": ["False"]},
            # expense ratio only exists for funds -> conditional rule below
            "expense_ratio_bps": {"type": "float", "min": 0, "max": 300},
        },
        "near_duplicates": [{"block": [], "compare": ["name"], "threshold": 0.95,
                             "severity": "INFO"}],
    },
    "fact_fees": {
        "pk": "fee_id", "expected_rows": 122197,
        "business_key": ["client_id", "fee_date", "fee_type"],
        "columns": {
            "fee_id": {"type": "int"},
            "client_id": {"type": "int", "required": True},
            "advisor_id": {"type": "int", "required": True},
            "fee_date": {"type": "date", "required": True},
            "fee_type": {"type": "str", "required": True,
                         "allowed": ["Management", "Advisory", "Custodial", "Performance"]},
            "aum_basis": {"type": "float", "required": True, "gt": 0,
                          "range_severity": "ERROR"},
            "bps_charged": {"type": "float", "required": True, "gt": 0, "max": 300},
            "amount": {"type": "float", "required": True, "gt": 0,
                       "range_severity": "ERROR"},
        },
    },
    "fact_portfolio_snapshot": {
        "pk": "snapshot_id", "expected_rows": 129547,
        "business_key": ["client_id", "snapshot_date"],
        "columns": {
            "snapshot_id": {"type": "int"},
            "client_id": {"type": "int", "required": True},
            "advisor_id": {"type": "int", "required": True},
            "benchmark_id": {"type": "int", "required": True},
            "snapshot_date": {"type": "date", "required": True},
            "year": {"type": "int", "required": True, "min": 1900, "max": 2100},
            "month": {"type": "int", "required": True, "min": 1, "max": 12,
                      "range_severity": "ERROR"},
            "aum": {"type": "float", "required": True, "gt": 0, "range_severity": "ERROR"},
            # YTD return below -100% is impossible; above +100% is implausible
            "return_ytd": {"type": "float", "required": True, "min": -1, "max": 1},
            "benchmark_return_ytd": {"type": "float", "required": True, "min": -1, "max": 1},
            "active_alpha_ytd": {"type": "float", "required": True, "min": -1, "max": 1},
            "cash_pct": {"type": "float", "required": True, "min": 0, "max": 1,
                         "range_severity": "ERROR"},
            "account_type": {"type": "str", "required": True,
                             "allowed": ["Taxable", "IRA", "Roth IRA", "Trust", "529"]},
        },
    },
    "fact_transactions": {
        "pk": "txn_id", "expected_rows": 298772,
        "columns": {
            "txn_id": {"type": "int"},
            "client_id": {"type": "int", "required": True},
            "advisor_id": {"type": "int", "required": True},
            # security_id/shares/price only apply to security trades -> rules below
            "security_id": {"type": "int"},
            "txn_date": {"type": "date", "required": True},
            "txn_type": {"type": "str", "required": True,
                         "allowed": ["Buy", "Sell", "Dividend", "Deposit",
                                     "Withdrawal", "Advisory Fee"]},
            "shares": {"type": "float"},
            "price": {"type": "float", "gt": 0, "range_severity": "ERROR"},
            "amount": {"type": "float", "required": True, "gt": 0, "range_severity": "ERROR"},
        },
    },
}

# (child table, child col, parent table, parent col, nullable, kind)
# kind "id" = surrogate/natural key, "date" = join to the date dimension
FOREIGN_KEYS = [
    ("dim_client", "advisor_id", "dim_advisor", "advisor_id", False, "id"),
    ("dim_security", "benchmark_id", "dim_benchmark", "benchmark_id", False, "id"),
    ("fact_fees", "client_id", "dim_client", "client_id", False, "id"),
    ("fact_fees", "advisor_id", "dim_advisor", "advisor_id", False, "id"),
    ("fact_portfolio_snapshot", "client_id", "dim_client", "client_id", False, "id"),
    ("fact_portfolio_snapshot", "advisor_id", "dim_advisor", "advisor_id", False, "id"),
    ("fact_portfolio_snapshot", "benchmark_id", "dim_benchmark", "benchmark_id", False, "id"),
    ("fact_transactions", "client_id", "dim_client", "client_id", False, "id"),
    ("fact_transactions", "advisor_id", "dim_advisor", "advisor_id", False, "id"),
    ("fact_transactions", "security_id", "dim_security", "security_id", True, "id"),
    ("fact_fees", "fee_date", "dim_date", "date", False, "date"),
    ("fact_portfolio_snapshot", "snapshot_date", "dim_date", "date", False, "date"),
    ("fact_transactions", "txn_date", "dim_date", "date", False, "date"),
]

SECURITY_TXN_TYPES = {"Buy", "Sell", "Dividend"}
TRADE_TXN_TYPES = {"Buy", "Sell"}
# which benchmark asset classes are a sensible benchmark for a security asset class
SECURITY_BENCHMARK_FIT = {
    "Equity": {"Equity", "International", "Small Cap", "Emerging"},
    "Fixed Income": {"Fixed Income"},
    "Alternative": {"Multi-Asset"},
    "Cash Equivalent": {"Cash"},
}
# client_tier bands observed in the firm's segmentation (initial AUM, USD)
TIER_BANDS = {"Retail": (0, 500_000), "HNW": (500_000, 5_000_000),
              "UHNW": (5_000_000, float("inf"))}

# =============================================================================
# 1. FINDINGS COLLECTOR
# =============================================================================
SEV_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2, "PASS": 3}
GROUPS = {1: "1 Column level", 2: "2 Single-table", 3: "3 Referential integrity",
          4: "4 Cross-table"}


class Findings:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, group: int, table: str, column: str, check: str, severity: str,
            affected: int, checked: int, detail: str = "", examples="") -> None:
        """severity = severity IF the check finds something; 0 affected -> PASS."""
        affected = int(affected)
        checked = int(checked)
        if isinstance(examples, (list, tuple, pd.Index, pd.Series, np.ndarray)):
            examples = "; ".join(str(e) for e in list(examples)[:5])
        self.rows.append({
            "group": GROUPS[group], "table": table, "column": column, "check": check,
            "severity": severity if affected > 0 else "PASS",
            "rows_affected": affected, "rows_checked": checked,
            "pct_affected": round(100 * affected / checked, 4) if checked else 0.0,
            "detail": detail, "examples": examples,
        })

    def frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.rows, columns=[
            "group", "table", "column", "check", "severity", "rows_affected",
            "rows_checked", "pct_affected", "detail", "examples"])
        df["_s"] = df["severity"].map(SEV_ORDER)
        df = df.sort_values(["_s", "group", "table", "column"], kind="stable")
        df = df.drop(columns="_s").reset_index(drop=True)
        df.insert(0, "check_id", [f"DQ{i:03d}" for i in range(1, len(df) + 1)])
        return df


# =============================================================================
# 2. GENERIC HELPERS (parsing, shapes, formats) - work on any text Series
# =============================================================================
RE_INT = r"[+-]?\d+"
RE_FLOAT = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
RE_DATE = r"\d{4}-\d{2}-\d{2}"
BOOL_TRUE = {"true", "t", "yes", "y"}
BOOL_FALSE = {"false", "f", "no", "n"}


def read_table(path: Path) -> pd.DataFrame:
    """Read a CSV strictly as text: no NaN conversion, no type guessing."""
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False,
                       encoding="utf-8-sig")


def is_blank(s: pd.Series) -> pd.Series:
    return s.str.strip() == ""


def is_null_token(s: pd.Series) -> pd.Series:
    return s.str.strip().str.upper().isin(NULL_TOKENS)


def convert(s: pd.Series, typ: str, spec: dict | None = None) -> pd.Series:
    """Strict conversion of a text Series. Blank or unparseable -> NaN/NaT/NA.
    Values are stripped first (whitespace is reported as its own problem)."""
    spec = spec or {}
    st = s.astype(str).str.strip()
    if typ == "int":
        return pd.to_numeric(st.where(st.str.fullmatch(RE_INT)), errors="coerce")
    if typ == "float":
        return pd.to_numeric(st.where(st.str.fullmatch(RE_FLOAT)), errors="coerce")
    if typ == "date":
        return pd.to_datetime(st.where(st.str.fullmatch(RE_DATE)),
                              format="%Y-%m-%d", errors="coerce")
    if typ == "bool":
        out = pd.Series(pd.NA, index=s.index, dtype="boolean")   # nullable bool
        if "true" in spec or "false" in spec:
            out[st.isin(set(spec.get("true", [])))] = True
            out[st.isin(set(spec.get("false", [])))] = False
        else:
            low = st.str.lower()
            out[low.isin(BOOL_TRUE)] = True
            out[low.isin(BOOL_FALSE)] = False
        return out
    return st.where(st != "")          # str: blank -> NaN, everything else parses


def parses(s: pd.Series, typ: str, spec: dict | None = None) -> pd.Series:
    """True where a NON-blank value parses as typ."""
    return convert(s, typ, spec).notna() & ~is_blank(s)


def infer_type(s: pd.Series) -> str:
    """Infer the intended type from the values (used when no schema is given)."""
    vc = s.str.strip().value_counts()
    vc = vc[vc.index != ""]
    if vc.empty:
        return "empty"
    vals = pd.Series(vc.index, dtype=str)
    cnt = vc.to_numpy()
    total = cnt.sum()

    def share(mask: pd.Series) -> float:
        return cnt[mask.to_numpy()].sum() / total

    low = vals.str.lower()
    if share(low.isin(BOOL_TRUE | BOOL_FALSE)) >= TYPE_INFER_THRESHOLD and vals.size <= 4:
        return "bool"
    if share(vals.str.fullmatch(RE_INT)) >= TYPE_INFER_THRESHOLD:
        return "int"
    if share(vals.str.fullmatch(RE_FLOAT)) >= TYPE_INFER_THRESHOLD:
        return "float"
    if share(parses(vals, "date")) >= TYPE_INFER_THRESHOLD:
        return "date"
    return "str"


def shape_of(vals: pd.Series) -> pd.Series:
    """Value 'shape': digit runs -> 9, UPPER runs -> A, lower runs -> a, other
    characters kept.  'Pennsylvania'->'Aa', 'PA'->'A', '45.0'->'9.9',
    '2020-01-01'->'9-9-9', '-0.64%'->'-9.9%', ' 45'->' 9'."""
    out = (vals.astype(str).str.replace(r"\d+", "9", regex=True)
           .str.replace(r"[A-Z]+", "A", regex=True)
           .str.replace(r"[a-z]+", "a", regex=True))
    return out.where(vals.str.strip() != "", "<blank>")


def format_family(v: str, typ: str) -> str:
    """Coarse format family of one stripped value, relative to the expected type."""
    if v == "":
        return "blank"
    if re.fullmatch(r"[+-]?\d+", v):
        return "number" if typ == "float" else "integer"
    if re.fullmatch(r"[+-]?\d+\.0+", v):
        return "number" if typ == "float" else "integer-written-as-decimal (x.0)"
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)", v):
        return "number" if typ == "float" else "decimal"
    if re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)[eE][+-]?\d+", v):
        return "scientific"
    if re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)\s*%", v):
        return "percent string (x%)"
    if "$" in v and re.search(r"\d", v):
        return "currency string ($)"
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", v):
        return "thousands separator (1,000)"
    if re.fullmatch(r"\(\s*\d[\d,]*\.?\d*\s*\)", v):
        return "accounting negative (x)"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return "date yyyy-mm-dd"
    if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", v):
        return "date yyyy/mm/dd"
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", v):
        return "date m/d/y"
    if re.fullmatch(r"\d{1,2}-[A-Za-z]{3}-\d{2,4}", v):
        return "date d-Mon-y"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(:\d{2})?.*", v):
        return "datetime"
    if v.lower() in BOOL_TRUE | BOOL_FALSE:
        return "boolean word"
    if v.upper() in NULL_TOKENS:
        return "null token"
    # text: describe casing
    letters = re.sub(r"[^A-Za-z]", "", v)
    if not letters:
        return "digits/symbols text"
    if letters.isupper():
        return "text UPPER"
    if letters.islower():
        return "text lower"
    words = [w for w in re.split(r"[\s\-/&]+", v) if re.search(r"[A-Za-z]", w)]
    if all(w[0].isupper() or not w[0].isalpha() for w in words):
        return "text Title"
    return "text Mixed"


def norm_key(s: pd.Series) -> pd.Series:
    """Normalise ID-like keys so '45', ' 45', '45.0', '045' compare equal."""
    st = s.astype(str).str.strip()
    m = st.str.fullmatch(r"[+-]?\d+(?:\.0+)?")
    if m.any():
        st = st.copy()
        st[m] = pd.to_numeric(st[m], errors="coerce").astype("int64").astype(str)
    return st


def norm_text(s: pd.Series) -> pd.Series:
    """Normalise free text for duplicate / variant detection."""
    return (s.astype(str).str.lower().str.replace(r"[^a-z0-9 ]", "", regex=True)
            .str.replace(r"\s+", " ", regex=True).str.strip())


def fmt_examples(vc: pd.Series, n: int = 5) -> str:
    return "; ".join(f"'{v}' ({c})" for v, c in vc.head(n).items())


def infer_pk(df: pd.DataFrame) -> str | None:
    """For tables with no schema: the first column named 'id' or '*_id' is
    treated as the primary key (convention in star-schema extracts)."""
    return next((c for c in df.columns if c.lower() == "id" or c.lower().endswith("_id")), None)


def key_examples(df: pd.DataFrame, mask: pd.Series, cols: list[str], n: int = 5) -> str:
    sub = df.loc[mask, [c for c in cols if c in df.columns]].head(n)
    return "; ".join(", ".join(f"{c}={row[c]}" for c in sub.columns)
                     for _, row in sub.iterrows())


# =============================================================================
# 3. GENERIC COLUMN PROFILE
# =============================================================================
def profile_column(table: str, col: str, s: pd.Series, expected_type: str,
                   spec: dict | None, position: int) -> dict:
    """Profile one text column. Works for any table/column."""
    spec = spec or {}
    n = len(s)
    vc = s.value_counts(dropna=False)                 # work on distinct values
    vals = pd.Series(vc.index.astype(str), dtype=str)
    cnt = vc.to_numpy()

    def total(mask: pd.Series) -> int:
        return int(cnt[mask.to_numpy()].sum())

    stripped = vals.str.strip()
    blank_m = stripped == ""
    token_m = stripped.str.upper().isin(NULL_TOKENS) & ~blank_m
    nonblank = n - total(blank_m)

    # shapes
    shapes = pd.Series(cnt, index=shape_of(vals)).groupby(level=0).sum()
    shapes = shapes.sort_values(ascending=False)
    nb_shapes = shapes.drop("<blank>", errors="ignore")
    dom_shape = nb_shapes.index[0] if len(nb_shapes) else ""
    dom_share = nb_shapes.iloc[0] / nonblank if nonblank else 0.0

    # parse failures vs expected type (non-blank values only)
    if expected_type in ("int", "float", "date", "bool"):
        ok = parses(vals, expected_type, spec)
        fail_m = ~ok & ~blank_m
    else:
        fail_m = pd.Series(False, index=vals.index)
    fail_n = total(fail_m)
    fail_vc = pd.Series(cnt[fail_m.to_numpy()], index=vals[fail_m]).sort_values(ascending=False)

    # format families -> mixed formats
    fam = stripped[~blank_m].map(lambda v: format_family(v, expected_type))
    fam_counts = (pd.Series(cnt[(~blank_m).to_numpy()], index=fam.to_numpy())
                  .groupby(level=0).sum().sort_values(ascending=False))
    # free text (names, cities) naturally mixes casing -> for text columns only
    # categorical columns (<= 50 distinct values) count as "mixed format"
    n_distinct = int(stripped[~blank_m].nunique())
    mixed = len(fam_counts) > 1 and (expected_type != "str" or n_distinct <= 50)

    # whitespace
    lt_ws = total(vals != stripped)
    multi_ws = total(stripped.str.contains(r"\s{2,}", regex=True))
    nonprint = total(vals.str.contains(r"[\x00-\x08\x0b-\x1f\x7f\xa0]", regex=True))

    # case / spelling variants (same normalised text, several raw spellings)
    # - text and boolean columns only (for numbers, '1.5' vs '15' would collide)
    variant_groups, variant_examples = 0, ""
    if expected_type in ("str", "bool"):
        nt = norm_text(stripped)
        var_df = pd.DataFrame({"raw": stripped, "norm": nt, "cnt": cnt})[~blank_m.to_numpy()]
        var_groups = var_df.groupby("norm")["raw"].nunique()
        variant_groups = int((var_groups > 1).sum())
    if variant_groups:
        bad_norms = var_groups[var_groups > 1].index[:3]
        variant_examples = " | ".join(
            " / ".join(f"'{r}'" for r in var_df.loc[var_df.norm == b, "raw"].unique()[:4])
            for b in bad_norms)

    lead_zero = total(stripped.str.fullmatch(r"0\d+"))

    lengths = stripped[~blank_m].str.len()
    prof = {
        "table": table, "column": col, "position": position, "rows": n,
        "expected_type": expected_type,
        "inferred_type": infer_type(s),
        "null_blank_count": total(blank_m),
        "null_blank_pct": round(100 * total(blank_m) / n, 4) if n else 0.0,
        "null_token_count": total(token_m),
        "null_tokens_found": fmt_examples(pd.Series(cnt[token_m.to_numpy()],
                                                    index=vals[token_m])),
        "non_blank_count": nonblank,
        "distinct_count": int(stripped[~blank_m].nunique()),
        "distinct_pct": round(100 * stripped[~blank_m].nunique() / nonblank, 4) if nonblank else 0.0,
        "is_unique": bool(nonblank == n and stripped.nunique() == n and len(vc) == n),
        "top_values": fmt_examples(vc.sort_values(ascending=False)),
        "n_shapes": int(len(nb_shapes)),
        "dominant_shape": dom_shape,
        "dominant_shape_pct": round(100 * dom_share, 4),
        "top_shapes": fmt_examples(nb_shapes, 5),
        "format_families": fmt_examples(fam_counts, 6),
        "mixed_format": mixed,
        "parse_fail_count": fail_n,
        "parse_fail_pct": round(100 * fail_n / nonblank, 4) if nonblank else 0.0,
        "parse_fail_examples": fmt_examples(fail_vc),
        "leading_trailing_ws_count": lt_ws,
        "internal_multi_space_count": multi_ws,
        "nonprintable_count": nonprint,
        "case_variant_groups": variant_groups,
        "case_variant_examples": variant_examples,
        "leading_zero_count": lead_zero,
        "min_length": int(lengths.min()) if len(lengths) else None,
        "max_length": int(lengths.max()) if len(lengths) else None,
        "num_min": None, "num_max": None, "num_mean": None, "num_median": None,
        "iqr_outlier_count": None, "date_min": None, "date_max": None,
        "top_value_count": int(cnt[(~blank_m).to_numpy()].max()) if nonblank else 0,
    }
    if expected_type in ("int", "float"):
        x = convert(s, expected_type).dropna()
        if len(x):
            q1, q3 = x.quantile(0.25), x.quantile(0.75)
            iqr = q3 - q1
            prof.update(num_min=float(x.min()), num_max=float(x.max()),
                        num_mean=round(float(x.mean()), 6),
                        num_median=float(x.median()),
                        iqr_outlier_count=int(((x < q1 - 1.5 * iqr) | (x > q3 + 1.5 * iqr)).sum()))
    if expected_type == "date":
        d = convert(s, "date").dropna()
        if len(d):
            prof.update(date_min=d.min().strftime("%Y-%m-%d"),
                        date_max=d.max().strftime("%Y-%m-%d"))
    return prof


# =============================================================================
# 4. GROUP 1 - COLUMN-LEVEL CHECKS (generic, driven by profile + spec)
# =============================================================================
def column_checks(F: Findings, table: str, df: pd.DataFrame, profiles: dict,
                  schema: dict | None, key_cols: set[str]) -> None:
    n = len(df)
    pk = schema.get("pk") if schema else infer_pk(df)
    cols_spec = schema.get("columns", {}) if schema else {}

    # schema drift + row count
    if schema:
        missing = [c for c in cols_spec if c not in df.columns]
        extra = [c for c in df.columns if c not in cols_spec]
        F.add(1, table, "(table)", "schema: expected columns present", "ERROR",
              len(missing), len(cols_spec), "missing columns break downstream code",
              missing)
        F.add(1, table, "(table)", "schema: no unexpected columns", "WARN",
              len(extra), len(df.columns), "columns not in the documented schema", extra)
        if "expected_rows" in schema:
            diff = abs(n - schema["expected_rows"])
            F.add(1, table, "(table)", "row count matches documented count", "WARN",
                  diff, schema["expected_rows"],
                  f"documented {schema['expected_rows']:,}, found {n:,}")

    for col in df.columns:
        p = profiles[col]
        spec = cols_spec.get(col, {})
        typ = p["expected_type"]
        is_key = col in key_cols
        s = df[col]
        st = s.str.strip()
        blank = st == ""
        token = is_null_token(s) & ~blank
        usable = ~blank & ~token
        nb = int((~blank).sum())

        # --- type validity --------------------------------------------------
        if typ in ("int", "float", "date", "bool"):
            sev = "ERROR"   # unparseable numbers/dates/keys break calcs and joins
            fails = ~parses(s, typ, spec) & usable
            fam = (st[fails].map(lambda v: format_family(v, typ)).value_counts())
            hint = f"failing formats: {fmt_examples(fam, 4)}" if fails.any() else ""
            if fails.any() and (fam.index == "integer-written-as-decimal (x.0)").all():
                hint += " -> integers stored with a '.0' suffix; text joins to integer keys fail"
            if fails.any() and "percent string (x%)" in fam.index:
                hint += " -> percent strings mixed with decimals; divide by 100 after stripping '%'"
            F.add(1, table, col, f"type: values parse as {typ}", sev,
                  int(fails.sum()), int(usable.sum()),
                  f"expected {typ} ({'schema' if spec else 'inferred'}). {hint}".strip(),
                  fmt_examples(st[fails].value_counts()))

        # --- completeness -----------------------------------------------------
        if col == pk:
            F.add(1, table, col, "primary key: not blank", "ERROR", int(blank.sum()), n)
        elif spec.get("required"):
            sev = "ERROR" if is_key else spec.get("required_severity", "WARN")
            F.add(1, table, col, "required: not blank", sev, int(blank.sum()), n,
                  "missing values cannot be recovered without a source")
        elif blank.any():
            F.add(1, table, col, "optional column: blank values", "INFO",
                  int(blank.sum()), n,
                  "blanks allowed by schema - see conditional rules in group 2 "
                  "to tell legitimate structure from missing data")

        F.add(1, table, col, "null-like text tokens (e.g. 'None', 'NULL')", "WARN",
              int(token.sum()), n,
              "placeholder text instead of a true blank; BI tools treat it as a real "
              "category and default pandas parsing turns it into NaN",
              fmt_examples(st[token].value_counts()))

        # --- whitespace / formatting -----------------------------------------
        ws = s != st
        F.add(1, table, col, "leading/trailing whitespace", "ERROR" if is_key else "WARN",
              int(ws.sum()), n, "breaks exact-match joins and group-bys",
              fmt_examples(s[ws].map(repr).value_counts()))
        if p["internal_multi_space_count"]:
            F.add(1, table, col, "repeated internal spaces", "WARN",
                  p["internal_multi_space_count"], n)
        if p["nonprintable_count"]:
            F.add(1, table, col, "non-printable / non-breaking characters", "WARN",
                  p["nonprintable_count"], n)
        # typed columns with parse failures already list their formats above.
        # Text categoricals: when an allowed list exists, that check decides.
        if p["mixed_format"] and not (typ in ("int", "float", "date", "bool")
                                      and p["parse_fail_count"]) \
                and not (typ == "str" and ("allowed" in spec or "pattern" in spec)):
            # what is left parses fine (e.g. '2e-05' next to '0.000020') or is a
            # free-text categorical -> worth knowing, not wrong by itself
            sev = "INFO"
            fam_vc = st[usable].map(lambda v: format_family(v, typ)).value_counts()
            minority = int(fam_vc.iloc[1:].sum()) if len(fam_vc) > 1 else 0
            if minority:
                F.add(1, table, col, "mixed value formats", sev, minority, int(usable.sum()),
                      f"format families: {p['format_families']}",
                      fmt_examples(st[usable][st[usable].map(
                          lambda v: format_family(v, typ)) != fam_vc.index[0]].value_counts()))
        if p["case_variant_groups"]:
            F.add(1, table, col, "case/spelling variants of the same value", "WARN",
                  p["case_variant_groups"], p["distinct_count"],
                  "same text after lower-casing/stripping punctuation, different raw spelling "
                  "(rows_affected = number of variant groups)", p["case_variant_examples"])
        if p["leading_zero_count"]:
            if typ in ("int", "float"):
                F.add(1, table, col, "leading zeros in a numeric column", "ERROR" if is_key else "WARN",
                      p["leading_zero_count"], nb, "lost on numeric conversion; '007' != '7' in a text join")
            else:
                F.add(1, table, col, "leading zeros (must stay text)", "INFO",
                      p["leading_zero_count"], nb,
                      "numeric-looking text with leading zeros (e.g. ZIP codes): "
                      "default parsing would drop them - keep as text")

        # --- allowed values / pattern ----------------------------------------
        if "allowed" in spec:
            bad = usable & ~st.isin(set(spec["allowed"]))
            F.add(1, table, col, "allowed values", spec.get("allowed_severity", "ERROR"),
                  int(bad.sum()), int(usable.sum()),
                  f"allowed: {', '.join(map(str, spec['allowed']))[:200]}",
                  fmt_examples(st[bad].value_counts()))
        if "pattern" in spec:
            bad = usable & ~st.str.fullmatch(spec["pattern"])
            F.add(1, table, col, f"pattern {spec['pattern']}",
                  spec.get("pattern_severity", "WARN"), int(bad.sum()), int(usable.sum()),
                  "", fmt_examples(st[bad].value_counts()))

        # --- ranges ------------------------------------------------------------
        if typ in ("int", "float", "date") and any(k in spec for k in ("min", "max", "gt")):
            x = convert(s, typ)
            lo = spec.get("min")
            hi = spec.get("max")
            if typ == "date":
                lo = pd.Timestamp(lo) if lo is not None else None
                hi = pd.Timestamp(hi) if hi is not None else None
            bad = pd.Series(False, index=s.index)
            if lo is not None:
                bad |= x < lo
            if hi is not None:
                bad |= x > hi
            if "gt" in spec:
                bad |= x <= spec["gt"]
            rng = (f"{'>' + str(spec['gt']) if 'gt' in spec else ''}"
                   f"{' >=' + str(spec['min']) if 'min' in spec else ''}"
                   f"{' <=' + str(spec['max']) if 'max' in spec else ''}").strip()
            F.add(1, table, col, f"range {rng}", spec.get("range_severity", "WARN"),
                  int(bad.fillna(False).sum()), int(x.notna().sum()),
                  f"observed min={p['num_min'] if typ != 'date' else p['date_min']}, "
                  f"max={p['num_max'] if typ != 'date' else p['date_max']}",
                  fmt_examples(st[bad.fillna(False)].value_counts()))

        # --- key uniqueness ------------------------------------------------------
        if col == pk or spec.get("unique"):
            raw_dup = s.duplicated(keep=False) & ~blank
            normk = norm_key(s) if (typ in ("int", "float") or col in key_cols) else norm_text(s)
            norm_dup = normk.duplicated(keep=False) & ~blank
            label = "primary key" if col == pk else "natural key"
            F.add(1, table, col, f"{label}: unique (exact text)", "ERROR",
                  int(raw_dup.sum()), n, "", fmt_examples(st[raw_dup].value_counts()))
            F.add(1, table, col, f"{label}: unique after normalising", "ERROR",
                  int((norm_dup & ~raw_dup).sum()), n,
                  "values that differ only by spacing/case/'.0' collide once cleaned",
                  fmt_examples(st[norm_dup & ~raw_dup].value_counts()))

        # --- generic plausibility signals ---------------------------------------
        if typ in ("int", "float") and p["distinct_pct"] >= 50 and nb >= 1000 \
                and p["top_value_count"] >= HEAPING_MIN_COUNT:
            top = st[~blank].value_counts().head(3)
            F.add(1, table, col, "value heaping (repeated value in a near-unique column)",
                  "WARN", int(top.iloc[0]), nb,
                  "a continuous measure repeats one value far more than expected - "
                  "often a floor, default or placeholder", fmt_examples(top))
        if p["distinct_count"] == 1 and nb == n and n > 1:
            F.add(1, table, col, "constant column", "INFO", n, n,
                  "single value in every row", p["top_values"])
        if not schema and typ == "float":
            dec = st[~blank]
            if len(dec) and dec.str.fullmatch(r"[+-]?\d+\.0+").all():
                F.add(1, table, col, "integers stored as decimals (x.0)",
                      "ERROR" if col.endswith("_id") else "INFO", len(dec), nb,
                      "probably an integer column exported through a float")


# =============================================================================
# 5. GROUP 2 - SINGLE-TABLE RULES (generic engines + rule config)
# =============================================================================
def duplicate_checks(F: Findings, table: str, df: pd.DataFrame, schema: dict | None) -> None:
    n = len(df)
    pk = schema.get("pk") if schema else infer_pk(df)
    exact = df.duplicated(keep=False)
    F.add(2, table, "(row)", "exact duplicate rows (all columns)", "ERROR",
          int(exact.sum()), n, "rows involved in duplicate groups",
          key_examples(df, exact, list(df.columns[:3])))
    if pk and pk in df.columns:
        rest = [c for c in df.columns if c != pk]
        d = df[rest].duplicated(keep=False)
        F.add(2, table, "(row)", f"duplicate rows ignoring {pk}", "WARN",
              int(d.sum()), n, "same content under different surrogate keys - "
              "probable double load", key_examples(df, d, [pk] + rest[:3]))
        # same after trimming/case-folding every text column
        normed = df[rest].apply(lambda c: c.str.strip().str.lower())
        d2 = normed.duplicated(keep=False) & ~d
        F.add(2, table, "(row)", f"duplicate rows ignoring {pk} after trim/lower-case", "WARN",
              int(d2.sum()), n, "", key_examples(df, d2, [pk] + rest[:3]))
    if schema and schema.get("business_key"):
        bk = schema["business_key"]
        nk = df[bk].apply(lambda c: norm_key(c) if c.name.endswith("_id") else c.str.strip())
        d = nk.duplicated(keep=False)
        F.add(2, table, ",".join(bk), "business key unique", "ERROR", int(d.sum()), n,
              "one fact per grain - duplicates double-count in every SUM",
              key_examples(df, d, ([pk] if pk else []) + bk))


def near_duplicate_check(F: Findings, table: str, df: pd.DataFrame, pk: str,
                         specs: list[dict]) -> None:
    """Fuzzy duplicates: block on exact columns, compare normalised text."""
    pairs: dict[tuple, float] = {}
    for spec in specs:
        cmp = df[spec["compare"]].apply(norm_text).agg(" ".join, axis=1)
        blocks = (df.groupby([df[b].str.strip() for b in spec["block"]]).groups.values()
                  if spec["block"] else [df.index])
        for idx in blocks:
            idx = list(idx)
            if len(idx) < 2 or len(idx) > 2000:
                continue
            keys = cmp.loc[idx].tolist()
            for i in range(len(idx)):
                a = keys[i]
                for j in range(i + 1, len(idx)):
                    b = keys[j]
                    sm = difflib.SequenceMatcher(None, a, b)
                    if sm.real_quick_ratio() < spec["threshold"] or sm.quick_ratio() < spec["threshold"]:
                        continue
                    r = sm.ratio()
                    # truncation (one is a prefix of the other) counts as a match
                    if r >= spec["threshold"] or (min(len(a), len(b)) >= 4 and
                                                  (a.startswith(b) or b.startswith(a))):
                        pair = tuple(sorted((idx[i], idx[j])))
                        pairs[pair] = max(pairs.get(pair, 0), r)
    rows = sorted({i for p in pairs for i in p})
    sev = specs[0].get("severity", "WARN")
    cols = sorted({c for s in specs for c in s["compare"]})
    ex = [f"{df.at[a, pk]} '{' '.join(df.loc[a, cols])}' ~ {df.at[b, pk]} "
          f"'{' '.join(df.loc[b, cols])}' ({r:.2f})"
          for (a, b), r in sorted(pairs.items(), key=lambda kv: -kv[1])]
    desc = "; ".join(f"block={s['block'] or 'none'} compare={s['compare']} >= {s['threshold']}"
                     for s in specs)
    F.add(2, table, ",".join(cols), "near-duplicate records (fuzzy match)", sev,
          len(rows), len(df), f"{len(pairs)} candidate pairs. {desc}", ex[:5])


def functional_dependency(F: Findings, group: int, table: str, df: pd.DataFrame,
                          determinant: list[str], dependent: str, severity: str,
                          pk: str | None, label: str = "") -> None:
    """Each determinant value should map to exactly one dependent value.
    Violating rows = rows whose dependent differs from the group's most common value."""
    d = df.dropna(subset=determinant + [dependent])
    d = d[(d[determinant + [dependent]].astype(str).apply(lambda c: c.str.strip()) != "").all(axis=1)]
    mode = d.groupby(determinant)[dependent].agg(lambda x: x.value_counts().index[0])
    nuniq = d.groupby(determinant)[dependent].nunique()
    merged = d.join(mode.rename("_mode"), on=determinant)
    bad = merged[dependent] != merged["_mode"]
    F.add(group, table, f"{','.join(determinant)} -> {dependent}",
          label or f"functional dependency {','.join(determinant)} -> {dependent}",
          severity, int(bad.sum()), len(d),
          f"{int((nuniq > 1).sum())} of {len(nuniq)} determinant values map to >1 {dependent}",
          key_examples(merged, bad, ([pk] if pk else []) + determinant + [dependent]))


def row_rule(F: Findings, group: int, table: str, column: str, check: str, severity: str,
             mask: pd.Series, checked: int, df: pd.DataFrame, ex_cols: list[str],
             detail: str = "") -> None:
    mask = mask.fillna(False).astype(bool)
    F.add(group, table, column, check, severity, int(mask.sum()), checked, detail,
          key_examples(df, mask, ex_cols))


def single_table_rules(F: Findings, R: dict, T: dict) -> None:
    """Row-level contradictions - business rules for the Wildcat tables."""
    # ---- dim_client -------------------------------------------------------------
    if "dim_client" in T:
        t, r = T["dim_client"], R["dim_client"]
        lo = t["client_tier"].map(lambda k: TIER_BANDS.get(k, (np.nan, np.nan))[0])
        hi = t["client_tier"].map(lambda k: TIER_BANDS.get(k, (np.nan, np.nan))[1])
        has = t["initial_aum"].notna() & lo.notna()
        bad = has & ~((t["initial_aum"] >= lo) & (t["initial_aum"] < hi))
        row_rule(F, 2, "dim_client", "client_tier,initial_aum",
                 "client_tier consistent with initial_aum band", "WARN", bad, int(has.sum()),
                 r, ["client_id", "client_tier", "initial_aum"],
                 "Retail <$500k, HNW $500k-$5M, UHNW >=$5M")

    # ---- dim_date ---------------------------------------------------------------
    if "dim_date" in T:
        t, r = T["dim_date"], R["dim_date"]
        d = t["date"]
        ok = d.notna()
        ex = ["date_id", "date"]
        iso_week = d.dt.isocalendar().week.astype("float")
        for col, expect in [("year", d.dt.year), ("quarter", d.dt.quarter),
                            ("month", d.dt.month), ("week_of_year", iso_week)]:
            row_rule(F, 2, "dim_date", col, f"{col} matches date", "ERROR",
                     ok & t[col].notna() & (t[col] != expect), int(ok.sum()), r, ex + [col])
        row_rule(F, 2, "dim_date", "month_name", "month_name matches date", "ERROR",
                 ok & (t["month_name"] != d.dt.month_name()), int(ok.sum()), r, ex + ["month_name"])
        row_rule(F, 2, "dim_date", "day_of_week", "day_of_week matches date", "ERROR",
                 ok & (t["day_of_week"] != d.dt.day_name()), int(ok.sum()), r, ex + ["day_of_week"])
        for col, expect in [("is_month_end", d.dt.is_month_end), ("is_quarter_end", d.dt.is_quarter_end),
                            ("is_year_end", d.dt.is_year_end)]:
            row_rule(F, 2, "dim_date", col, f"{col} flag matches date", "ERROR",
                     ok & t[col].notna() & (t[col] != expect), int(ok.sum()), r, ex + [col])
        weekend = d.dt.dayofweek >= 5
        row_rule(F, 2, "dim_date", "is_trading_day", "weekend flagged as trading day", "ERROR",
                 ok & weekend & (t["is_trading_day"] == True), int(ok.sum()), r,  # noqa: E712
                 ex + ["day_of_week", "is_trading_day"])
        md = d.dt.strftime("%m-%d")
        holiday = ok & ~weekend & md.isin(["01-01", "07-04", "12-25"])
        row_rule(F, 2, "dim_date", "is_trading_day",
                 "fixed-date NYSE holidays (Jan 1, Jul 4, Dec 25) flagged as trading days",
                 "WARN", holiday & (t["is_trading_day"] == True), int(holiday.sum()), r,  # noqa: E712
                 ex + ["day_of_week", "is_trading_day"],
                 "calendar has no holiday logic; floating holidays (Thanksgiving, Good Friday, "
                 "etc.) are probably also marked as trading days")
        ds = d.dropna().sort_values()
        full = pd.date_range(ds.min(), ds.max(), freq="D") if len(ds) else []
        F.add(2, "dim_date", "date", "calendar is continuous (no missing days)", "ERROR",
              len(set(full) - set(ds)), len(full),
              f"range {ds.min():%Y-%m-%d} to {ds.max():%Y-%m-%d}" if len(ds) else "")
        order = t.dropna(subset=["date"]).sort_values("date")
        idn = pd.to_numeric(order["date_id"], errors="coerce")
        bad = idn.diff().fillna(1) != 1
        F.add(2, "dim_date", "date_id", "date_id increases by 1 with date", "WARN",
              int(bad.sum()), len(order), "surrogate key should be a sequential day counter")

    # ---- dim_security -------------------------------------------------------------
    if "dim_security" in T:
        t, r = T["dim_security"], R["dim_security"]
        etf = t["is_etf"] == True  # noqa: E712
        non = t["is_etf"] == False  # noqa: E712
        has_er = t["expense_ratio_bps"].notna()
        has_sec = t["sector"].notna()
        ex = ["security_id", "ticker", "is_etf", "sector", "expense_ratio_bps"]
        row_rule(F, 2, "dim_security", "expense_ratio_bps", "ETF must have an expense ratio",
                 "WARN", etf & ~has_er, int(etf.sum()), r, ex)
        row_rule(F, 2, "dim_security", "expense_ratio_bps", "non-ETF must not have an expense ratio",
                 "WARN", non & has_er, int(non.sum()), r, ex)
        row_rule(F, 2, "dim_security", "sector", "non-ETF must have a sector", "WARN",
                 non & ~has_sec, int(non.sum()), r, ex)
        row_rule(F, 2, "dim_security", "sector",
                 "ETF sector blank (legitimate structure: funds span sectors)", "INFO",
                 etf & ~has_sec, int(etf.sum()), r, ex,
                 "explains the sector blanks - do NOT fill")
        row_rule(F, 2, "dim_security", "expense_ratio_bps",
                 "non-ETF expense ratio blank (legitimate structure: stocks have none)", "INFO",
                 non & ~has_er, int(non.sum()), r, ex, "explains the expense_ratio blanks - do NOT fill")
        dn = norm_text(r["name"])
        dup = dn.duplicated(keep=False)
        row_rule(F, 2, "dim_security", "name", "same security name on different tickers",
                 "INFO", dup, len(r), r, ["security_id", "ticker", "name", "asset_class"],
                 "different tickers/asset classes - probably distinct products, verify")

    # ---- fact_fees ------------------------------------------------------------------
    if "fact_fees" in T:
        t, r = T["fact_fees"], R["fact_fees"]
        calc = t["aum_basis"] * t["bps_charged"] / 10000 / 12
        ok = calc.notna() & t["amount"].notna()
        row_rule(F, 2, "fact_fees", "amount",
                 "amount = aum_basis x bps_charged / 10,000 / 12 (+-0.011)", "ERROR",
                 ok & ((calc - t["amount"]).abs() > 0.011), int(ok.sum()), r,
                 ["fee_id", "aum_basis", "bps_charged", "amount"])
        okd = t["fee_date"].notna()
        row_rule(F, 2, "fact_fees", "fee_date", "fee_date is the 1st of the month (monthly grain)",
                 "WARN", okd & (t["fee_date"].dt.day != 1), int(okd.sum()), r, ["fee_id", "fee_date"])
        functional_dependency(F, 2, "fact_fees", t, ["client_id"], "advisor_id", "WARN", "fee_id",
                              "one advisor per client within the fact")

    # ---- fact_portfolio_snapshot -------------------------------------------------------
    if "fact_portfolio_snapshot" in T:
        tn = "fact_portfolio_snapshot"
        t, r = T[tn], R[tn]
        calc = t["return_ytd"] - t["benchmark_return_ytd"]
        ok = calc.notna() & t["active_alpha_ytd"].notna()
        row_rule(F, 2, tn, "active_alpha_ytd",
                 "active_alpha_ytd = return_ytd - benchmark_return_ytd (+-2e-6)", "ERROR",
                 ok & ((calc - t["active_alpha_ytd"]).abs() > 2e-6), int(ok.sum()), r,
                 ["snapshot_id", "return_ytd", "benchmark_return_ytd", "active_alpha_ytd"],
                 "rows with unparseable inputs are excluded here (see type checks)")
        okd = t["snapshot_date"].notna()
        row_rule(F, 2, tn, "year", "year matches snapshot_date", "ERROR",
                 okd & (t["year"] != t["snapshot_date"].dt.year), int(okd.sum()), r,
                 ["snapshot_id", "snapshot_date", "year"])
        row_rule(F, 2, tn, "month", "month matches snapshot_date", "ERROR",
                 okd & (t["month"] != t["snapshot_date"].dt.month), int(okd.sum()), r,
                 ["snapshot_id", "snapshot_date", "month"])
        row_rule(F, 2, tn, "snapshot_date", "snapshot_date is the 1st of the month",
                 "WARN", okd & (t["snapshot_date"].dt.day != 1), int(okd.sum()), r,
                 ["snapshot_id", "snapshot_date"])
        functional_dependency(F, 2, tn, t, ["benchmark_id", "snapshot_date"],
                              "benchmark_return_ytd", "ERROR", "snapshot_id",
                              "same benchmark + date must have one benchmark return")
        for dep in ["advisor_id", "benchmark_id", "account_type"]:
            functional_dependency(F, 2, tn, t, ["client_id"], dep, "WARN", "snapshot_id",
                                  f"one {dep} per client across snapshots")

    # ---- fact_transactions ---------------------------------------------------------------
    if "fact_transactions" in T:
        tn = "fact_transactions"
        t, r = T[tn], R[tn]
        ex = ["txn_id", "txn_type", "security_id", "shares", "price", "amount"]
        sec_type = t["txn_type"].isin(SECURITY_TXN_TYPES)
        has_sec = ~is_blank(r["security_id"])
        has_sh = ~is_blank(r["shares"])
        has_px = ~is_blank(r["price"])
        row_rule(F, 2, tn, "security_id", "Buy/Sell/Dividend must have a security_id", "ERROR",
                 sec_type & ~has_sec, int(sec_type.sum()), r, ex)
        row_rule(F, 2, tn, "security_id",
                 "cash flows (Deposit/Withdrawal/Advisory Fee) must not have a security_id",
                 "ERROR", ~sec_type & t["txn_type"].notna() & has_sec, int((~sec_type).sum()), r, ex)
        row_rule(F, 2, tn, "security_id",
                 "security_id blank on cash-flow rows (legitimate structure)", "INFO",
                 ~sec_type & t["txn_type"].notna() & ~has_sec, len(r), r, ex,
                 "explains security_id/shares/price blanks - do NOT fill")
        row_rule(F, 2, tn, "shares,price", "shares and price present exactly when security_id is",
                 "ERROR", (has_sh != has_sec) | (has_px != has_sec), len(r), r, ex)
        trade = t["txn_type"].isin(TRADE_TXN_TYPES)
        row_rule(F, 2, tn, "shares", "Buy/Sell shares must be > 0 (sign rule)", "ERROR",
                 trade & (t["shares"] <= 0), int((trade & t["shares"].notna()).sum()), r, ex,
                 "direction is carried by txn_type; amount is always positive")
        calc = t["shares"] * t["price"]
        ok = calc.notna() & t["amount"].notna()
        bad = ok & ((calc - t["amount"]).abs() > 0.011)
        abs_ok = bad & ((calc.abs() - t["amount"]).abs() <= 0.011)
        row_rule(F, 2, tn, "amount", "amount = shares x price (+-0.011)", "ERROR",
                 bad, int(ok.sum()), r, ex,
                 f"{int(abs_ok.sum())} of these match exactly once the sign of shares is "
                 "flipped -> sign error in shares, amount is right")
        div = t["txn_type"] == "Dividend"
        row_rule(F, 2, tn, "amount",
                 "Dividend amount equals full position value (shares x price)", "WARN",
                 div & ok & ((calc - t["amount"]).abs() <= 0.011), int(div.sum()), r, ex,
                 "a dividend is normally a small % of position value - check what 'amount' "
                 "means for dividends before summing income")
        functional_dependency(F, 2, tn, t, ["client_id"], "advisor_id", "WARN", "txn_id",
                              "one advisor per client within the fact")


# =============================================================================
# 6. GROUP 3 - REFERENTIAL INTEGRITY (one generic function)
# =============================================================================
def ri_check(F: Findings, R: dict, child: str, ccol: str, parent: str, pcol: str,
             nullable: bool, kind: str) -> dict:
    """Every non-blank FK value must exist in the parent key.
    Checks the RAW text (what a text join sees) and, separately, after key
    normalisation (strip, '45.0'->'45') to separate format problems from true
    orphans. Returns the metrics so they can be reused."""
    c = R[child][ccol]
    blank = is_blank(c)
    pk_raw = set(R[parent][pcol])
    match_raw = c.isin(pk_raw) & ~blank
    if kind == "id":
        pk_norm = set(norm_key(R[parent][pcol]))
        match_norm = norm_key(c).isin(pk_norm) & ~blank
    else:
        pk_norm = set(R[parent][pcol].str.strip())
        match_norm = c.str.strip().isin(pk_norm) & ~blank
    nb = int((~blank).sum())
    orphan_raw = ~blank & ~match_raw
    orphan_true = ~blank & ~match_norm
    fixable = orphan_raw & match_norm
    rel = f"{child}.{ccol} -> {parent}.{pcol}"
    metrics = {
        "relationship": rel,
        "match_rate_pct": round(100 * match_raw.sum() / nb, 4) if nb else 0.0,
        "match_rate_after_normalising_pct": round(100 * match_norm.sum() / nb, 4) if nb else 0.0,
        "orphan_rows": int(orphan_true.sum()),
        "distinct_orphan_keys": int(c[orphan_true].nunique()),
        "null_key_rate_pct": round(100 * blank.sum() / len(c), 4) if len(c) else 0.0,
    }
    detail = (f"raw match {metrics['match_rate_pct']}%, after normalising "
              f"{metrics['match_rate_after_normalising_pct']}%; null-key rate "
              f"{metrics['null_key_rate_pct']}%")
    F.add(3, child, ccol, f"FK {rel}: raw text value exists in parent", "ERROR",
          int(orphan_raw.sum()), nb,
          detail + (f"; {int(fixable.sum())} rows are format-only mismatches "
                    "(fix the format, not the data)" if fixable.any() else ""),
          fmt_examples(c[orphan_raw].value_counts()))
    F.add(3, child, ccol, f"FK {rel}: true orphans (no parent even after normalising)",
          "ERROR", int(orphan_true.sum()), nb,
          f"{metrics['distinct_orphan_keys']} distinct orphan keys; these rows drop out of an "
          "inner join and land in '(Blank)' in a BI model",
          fmt_examples(c[orphan_true].value_counts()))
    if not nullable:
        F.add(3, child, ccol, f"FK {rel}: not blank", "ERROR", int(blank.sum()), len(c))
    else:
        F.add(3, child, ccol, f"FK {rel}: blank (nullable FK)", "INFO", int(blank.sum()), len(c),
              "nullable by design - see conditional rule in group 2")
    return metrics


def unreferenced_parent_keys(F: Findings, R: dict, fks: list[tuple]) -> None:
    """Dimension rows that no child table ever points to (INFO)."""
    by_parent: dict[tuple, list] = {}
    for child, ccol, parent, pcol, _n, kind in fks:
        if child in R and parent in R:
            by_parent.setdefault((parent, pcol, kind), []).append((child, ccol))
    for (parent, pcol, kind), kids in by_parent.items():
        keyf = norm_key if kind == "id" else (lambda s: s.str.strip())
        used = set()
        for child, ccol in kids:
            used |= set(keyf(R[child][ccol]))
        pk = keyf(R[parent][pcol])
        unused = ~pk.isin(used)
        F.add(3, parent, pcol, f"parent keys never referenced by {', '.join(k[0] for k in kids)}",
              "INFO", int(unused.sum()), len(pk), "unused dimension members",
              R[parent].loc[unused, pcol].head(5).tolist())


# =============================================================================
# 7. GROUP 4 - CROSS-TABLE BUSINESS RULES
# =============================================================================
def cross_table_rules(F: Findings, R: dict, T: dict) -> None:
    need = lambda *ts: all(x in T for x in ts)  # noqa: E731
    if need("dim_client"):
        cl = T["dim_client"]
        # ---- facts vs dimension attributes ------------------------------------------
        for fact, pk in [("fact_fees", "fee_id"), ("fact_portfolio_snapshot", "snapshot_id"),
                         ("fact_transactions", "txn_id")]:
            if fact not in T:
                continue
            m = T[fact][[pk, "client_id", "advisor_id"]].merge(
                cl[["client_id", "advisor_id"]], on="client_id", how="inner",
                suffixes=("", "_dim"))
            bad = m["advisor_id"] != m["advisor_id_dim"]
            row_rule(F, 4, fact, "advisor_id", "fact advisor_id = dim_client.advisor_id", "WARN",
                     bad, len(m), m, [pk, "client_id", "advisor_id", "advisor_id_dim"],
                     "orphan clients excluded (see group 3)")

    if need("dim_client", "fact_portfolio_snapshot"):
        sn, cl = T["fact_portfolio_snapshot"], T["dim_client"]
        m = sn.merge(cl[["client_id", "primary_account_type", "risk_profile", "client_since",
                         "is_active"]], on="client_id", how="inner")
        row_rule(F, 4, "fact_portfolio_snapshot", "account_type",
                 "snapshot account_type = dim_client.primary_account_type", "WARN",
                 m["account_type"] != m["primary_account_type"], len(m), m,
                 ["snapshot_id", "client_id", "account_type", "primary_account_type"])
        functional_dependency(F, 4, "fact_portfolio_snapshot", m, ["risk_profile"],
                              "benchmark_id", "WARN", "snapshot_id",
                              "benchmark assigned consistently by client risk_profile")
        blank_rp = m["risk_profile"].isna()
        F.add(4, "fact_portfolio_snapshot", "benchmark_id",
              "snapshots for clients with blank risk_profile (benchmark not explainable)", "INFO",
              int(blank_rp.sum()), len(m),
              f"benchmark_id used: {fmt_examples(m.loc[blank_rp, 'benchmark_id'].value_counts())} "
              "- the fact may reveal the missing risk_profile; verify before filling")
        # activity before the client relationship began
        ok = m["client_since"].notna() & m["snapshot_date"].notna()
        sm = m["snapshot_date"].dt.to_period("M")
        cm = m["client_since"].dt.to_period("M")
        row_rule(F, 4, "fact_portfolio_snapshot", "snapshot_date",
                 "snapshot in a month before client_since", "WARN", ok & (sm < cm), int(ok.sum()),
                 m, ["snapshot_id", "client_id", "snapshot_date", "client_since"])
        row_rule(F, 4, "fact_portfolio_snapshot", "snapshot_date",
                 "snapshot dated the 1st of the onboarding month (before client_since day)", "INFO",
                 ok & (sm == cm) & (m["snapshot_date"] < m["client_since"]), int(ok.sum()), m,
                 ["snapshot_id", "client_id", "snapshot_date", "client_since"],
                 "month-grain fact dated the 1st - legitimate, do not treat as an error")
        # is_active vs latest snapshot
        last = sn.groupby("client_id")["snapshot_date"].max()
        gmax = sn["snapshot_date"].max()
        c2 = cl.set_index("client_id").join(last.rename("last_snapshot"), how="inner")
        act = c2["is_active"] == True  # noqa: E712
        ina = c2["is_active"] == False  # noqa: E712
        c2r = c2.reset_index()
        row_rule(F, 4, "dim_client", "is_active",
                 "active client has no snapshot in the latest period", "WARN",
                 (act & (c2["last_snapshot"] < gmax)).reset_index(drop=True), int(act.sum()), c2r,
                 ["client_id", "is_active", "last_snapshot"])
        row_rule(F, 4, "dim_client", "is_active",
                 "inactive client still has a snapshot in the latest period", "WARN",
                 (ina & (c2["last_snapshot"] >= gmax)).reset_index(drop=True), int(ina.sum()), c2r,
                 ["client_id", "is_active", "last_snapshot"])
        if "fact_transactions" in T:
            tx = T["fact_transactions"].merge(
                c2r[["client_id", "is_active", "last_snapshot"]], on="client_id", how="inner")
            after = ((tx["is_active"] == False)  # noqa: E712
                     & (tx["txn_date"].dt.to_period("M") > tx["last_snapshot"].dt.to_period("M")))
            row_rule(F, 4, "fact_transactions", "txn_date",
                     "inactive client: transaction after last snapshot month", "WARN",
                     after, int((tx["is_active"] == False).sum()), tx,  # noqa: E712
                     ["txn_id", "client_id", "txn_type", "txn_date", "last_snapshot"],
                     "account appears closed in the snapshot fact but keeps trading")

    if need("dim_client", "fact_fees"):
        fe = T["fact_fees"].merge(T["dim_client"][["client_id", "client_since"]],
                                  on="client_id", how="inner")
        ok = fe["client_since"].notna() & fe["fee_date"].notna()
        row_rule(F, 4, "fact_fees", "fee_date", "fee in a month before client_since", "WARN",
                 ok & (fe["fee_date"].dt.to_period("M") < fe["client_since"].dt.to_period("M")),
                 int(ok.sum()), fe, ["fee_id", "client_id", "fee_date", "client_since"])

    if need("dim_client", "fact_transactions"):
        tx = T["fact_transactions"].merge(T["dim_client"][["client_id", "client_since"]],
                                          on="client_id", how="inner")
        ok = tx["client_since"].notna() & tx["txn_date"].notna()
        row_rule(F, 4, "fact_transactions", "txn_date", "transaction before client_since", "WARN",
                 ok & (tx["txn_date"] < tx["client_since"]), int(ok.sum()), tx,
                 ["txn_id", "client_id", "txn_date", "client_since"])

    # ---- dimension members with no facts at all ------------------------------------------
    facts_with_client = [f for f in ("fact_fees", "fact_portfolio_snapshot", "fact_transactions")
                         if f in T]
    if need("dim_client") and facts_with_client:
        used = set()
        for f in facts_with_client:
            used |= set(T[f]["client_id"].dropna())
        cl = T["dim_client"]
        none = ~cl["client_id"].isin(used)
        row_rule(F, 4, "dim_client", "client_id", "client has no fees, snapshots or transactions",
                 "WARN", none, len(cl), R["dim_client"],
                 ["client_id", "first_name", "last_name", "client_since"],
                 "a client record with no activity - compare with the near-duplicate check")

    # ---- facts vs facts -------------------------------------------------------------------
    if need("fact_fees", "fact_portfolio_snapshot"):
        fe = T["fact_fees"].assign(_m=T["fact_fees"]["fee_date"].dt.to_period("M"))
        sn = T["fact_portfolio_snapshot"]
        sn = sn.assign(_m=sn["snapshot_date"].dt.to_period("M"))[["client_id", "_m", "aum"]]
        m = fe.merge(sn, on=["client_id", "_m"], how="left")
        nos = m["aum"].isna()
        row_rule(F, 4, "fact_fees", "client_id,fee_date",
                 "fee has a matching snapshot (same client, same month)", "ERROR",
                 nos, len(m), m, ["fee_id", "client_id", "fee_date"])
        ok = ~nos & m["aum_basis"].notna()
        row_rule(F, 4, "fact_fees", "aum_basis", "aum_basis = snapshot aum (same client & month)",
                 "ERROR", ok & ((m["aum_basis"] - m["aum"]).abs() > 0.011), int(ok.sum()), m,
                 ["fee_id", "client_id", "fee_date", "aum_basis", "aum"],
                 "fee is billed on the month's AUM - reconciliation between two facts")
        fk = set(zip(fe["client_id"], fe["_m"]))
        miss = ~pd.Series([k in fk for k in zip(sn["client_id"], sn["_m"])], index=sn.index)
        F.add(4, "fact_portfolio_snapshot", "client_id,snapshot_date",
              "snapshot month with no fee row", "INFO", int(miss.sum()), len(sn),
              "not every client-month is billed - confirm whether this is a billing gap")

    if need("fact_fees", "fact_transactions"):
        tx = T["fact_transactions"]
        adv = tx[tx["txn_type"] == "Advisory Fee"]
        a = adv.groupby(["client_id", adv["txn_date"].dt.year])["amount"].sum()
        fe = T["fact_fees"]
        b = fe.groupby(["client_id", fe["fee_date"].dt.year])["amount"].sum()
        j = pd.concat([a.rename("txn_fee_debits"), b.rename("fact_fees_total")], axis=1)
        both = j.dropna()
        off = (both["txn_fee_debits"] - both["fact_fees_total"]).abs() > 0.10 * both["fact_fees_total"]
        ratio = (both["txn_fee_debits"] / both["fact_fees_total"])
        F.add(4, "fact_transactions", "amount",
              "Advisory Fee debits reconcile to fact_fees (client-year, +-10%)", "WARN",
              int(off.sum()), len(both),
              f"txn/fees ratio median {ratio.median():.2f}, min {ratio.min():.2f}, "
              f"max {ratio.max():.2f}; {int(j['txn_fee_debits'].isna().sum())} client-years "
              f"with fees but no debit, {int(j['fact_fees_total'].isna().sum())} with debits "
              "but no fees. Two sources of fee revenue disagree - decide which is the "
              "system of record",
              [f"client {k[0]} {int(k[1])}: debits {v.txn_fee_debits:,.2f} vs fees "
               f"{v.fact_fees_total:,.2f}" for k, v in both[off].head(5).iterrows()])

    # ---- facts vs date dimension ---------------------------------------------------------
    if need("fact_transactions", "dim_date"):
        tx = T["fact_transactions"]
        dd = T["dim_date"][["date", "is_trading_day"]].rename(columns={"date": "txn_date"})
        m = tx.merge(dd, on="txn_date", how="inner")
        trade = m["txn_type"].isin(TRADE_TXN_TYPES)
        row_rule(F, 4, "fact_transactions", "txn_date",
                 "Buy/Sell on a non-trading day (dim_date.is_trading_day = 0)", "WARN",
                 trade & (m["is_trading_day"] == False), int(trade.sum()), m,  # noqa: E712
                 ["txn_id", "txn_type", "txn_date"],
                 "markets are closed - trade date is probably wrong (or a booking date)")

    # ---- dimension vs dimension ----------------------------------------------------------
    if need("dim_security", "dim_benchmark"):
        m = T["dim_security"].merge(T["dim_benchmark"][["benchmark_id", "asset_class"]],
                                    on="benchmark_id", how="inner", suffixes=("", "_bm"))
        fit = m.apply(lambda r: r["asset_class_bm"] in SECURITY_BENCHMARK_FIT.get(r["asset_class"], set()),
                      axis=1) if len(m) else pd.Series(dtype=bool)
        row_rule(F, 4, "dim_security", "benchmark_id",
                 "security benchmark matches its asset class", "WARN", ~fit, len(m), m,
                 ["security_id", "ticker", "asset_class", "benchmark_id", "asset_class_bm"])


# =============================================================================
# 8. TYPED VIEW (for rules) + DRIVER
# =============================================================================
def build_typed(df: pd.DataFrame, schema: dict | None, id_cols: set[str]) -> pd.DataFrame:
    """Strictly typed copy used by the rule checks. ID key columns are kept as
    normalised text so '45.0' still joins to '45' inside the rules."""
    cols = schema.get("columns", {}) if schema else {}
    out = pd.DataFrame(index=df.index)
    for c in df.columns:
        spec = cols.get(c, {})
        typ = spec.get("type") or infer_type(df[c])
        if c in id_cols:
            k = norm_key(df[c])
            out[c] = k.where(k != "")
        elif typ in ("int", "float", "date", "bool"):
            out[c] = convert(df[c], typ, spec)
        else:
            out[c] = convert(df[c], "str")
    return out


def find_inputs(args) -> dict[str, Path]:
    """--files wins; else --data-dir; else the first default folder holding a
    Wildcat table. Only the 8 Wildcat tables are read unless --all-csv is set
    or the folder holds none of them (then every CSV is profiled)."""
    if args.files:
        paths = [Path(f) for f in args.files]
    else:
        if args.data_dir:
            data_dir = Path(args.data_dir)
            if not data_dir.is_dir():
                sys.exit(f"--data-dir not found: {data_dir}")
        else:
            data_dir = next((d for d in DEFAULT_DATA_DIRS if d.is_dir() and
                             any((d / f"{t}.csv").exists() for t in WILDCAT_TABLES)), None)
            if data_dir is None:
                sys.exit("\nCould not find the Wildcat Capital CSV files "
                         "(dim_client.csv, fact_fees.csv, ...).\nLooked in:\n    "
                         + "\n    ".join(str(d) for d in DEFAULT_DATA_DIRS)
                         + '\n\nPoint the script at the folder that holds them, e.g.\n'
                         '    python scripts/01_profile.py --data-dir "C:\\path\\to\\Wildcat Capital"\n')
        paths = sorted(data_dir.glob("*.csv"))
        known = [p for p in paths if p.stem in WILDCAT_TABLES]
        if known and not args.all_csv:
            paths = known
        if not paths:
            sys.exit(f"No CSV files in {data_dir}")
        print(f"Data folder: {data_dir}")
    missing = [p for p in paths if not p.exists()]
    if missing:
        sys.exit(f"File(s) not found: {', '.join(map(str, missing))}")
    return {p.stem: p for p in paths}


def main() -> None:
    ap = argparse.ArgumentParser(description="Generic data-quality profile + DQ checks.")
    ap.add_argument("--data-dir", help="folder with the raw CSVs (default: auto-detect)")
    ap.add_argument("--files", nargs="+", help="profile these CSV files instead")
    ap.add_argument("--all-csv", action="store_true",
                    help="profile every CSV in the folder, not only the 8 Wildcat tables")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="where reports go")
    ap.add_argument("--suffix", default="raw", help="report suffix (profile_<suffix>.csv)")
    args = ap.parse_args()

    inputs = find_inputs(args)
    R = {name: read_table(path) for name, path in inputs.items()}
    for name, df in R.items():
        print(f"  read {name:<26} {len(df):>8,} rows x {df.shape[1]:>2} cols")

    # key columns per table (PKs, FKs, parents of FKs, business keys)
    key_cols: dict[str, set] = {t: set() for t in R}
    id_cols: dict[str, set] = {t: set() for t in R}
    for t in R:
        sch = SCHEMA.get(t)
        if sch:
            key_cols[t].add(sch["pk"])
            id_cols[t].add(sch["pk"])
            key_cols[t] |= set(sch.get("business_key", []))
        else:   # unknown table: treat *_id columns as keys
            key_cols[t] |= {c for c in R[t].columns
                            if c.lower() == "id" or c.lower().endswith("_id")}
            id_cols[t] |= key_cols[t]
    fks = [fk for fk in FOREIGN_KEYS if fk[0] in R and fk[2] in R]
    for child, ccol, parent, pcol, _n, kind in FOREIGN_KEYS:
        if child in R:
            key_cols[child].add(ccol)
            if kind == "id":
                id_cols[child].add(ccol)
        if parent in R and kind == "id":
            id_cols[parent].add(pcol)

    F = Findings()
    profile_rows = []
    T = {}
    for t, df in R.items():
        sch = SCHEMA.get(t)
        cols_spec = sch.get("columns", {}) if sch else {}
        profiles = {}
        for i, c in enumerate(df.columns, start=1):
            spec = cols_spec.get(c)
            typ = spec["type"] if spec else infer_type(df[c])
            profiles[c] = profile_column(t, c, df[c], typ, spec, i)
            profile_rows.append(profiles[c])
        column_checks(F, t, df, profiles, sch, key_cols[t])
        duplicate_checks(F, t, df, sch)
        if sch and sch.get("near_duplicates"):
            near_duplicate_check(F, t, df, sch["pk"], sch["near_duplicates"])
        T[t] = build_typed(df, sch, id_cols[t])
        print(f"  profiled {t}")

    single_table_rules(F, R, T)
    ri = [ri_check(F, R, *fk) for fk in fks]
    unreferenced_parent_keys(F, R, fks)
    cross_table_rules(F, R, T)

    # ---- save ----------------------------------------------------------------------
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    prof_df = pd.DataFrame(profile_rows)
    dq = F.frame()
    p1 = out / f"profile_{args.suffix}.csv"
    p2 = out / f"dq_summary_{args.suffix}.csv"
    prof_df.to_csv(p1, index=False, encoding="utf-8")
    dq.to_csv(p2, index=False, encoding="utf-8")

    # ---- console summary -------------------------------------------------------------
    print("\nReferential integrity")
    print(pd.DataFrame(ri).to_string(index=False))
    print("\nFindings by group and severity")
    print(pd.crosstab(dq["group"], dq["severity"]).reindex(
        columns=[s for s in SEV_ORDER if s in set(dq["severity"])]).to_string())
    issues = dq[dq["severity"].isin(["ERROR", "WARN"])]
    print(f"\nTop issues ({len(issues)} ERROR/WARN findings):")
    with pd.option_context("display.max_colwidth", 70, "display.width", 200):
        print(issues[["check_id", "severity", "table", "column", "check", "rows_affected"]]
              .head(40).to_string(index=False))
    print(f"\nSaved {p1}\nSaved {p2}")


if __name__ == "__main__":
    main()
