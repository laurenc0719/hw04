#!/usr/bin/env python3
"""
scripts/03_validate.py  --  HW4 Part D: referential integrity + validation
MIS3060 Business Intelligence with AI | Wildcat Capital

Runs every check on BOTH stages - the raw tables and the clean tables from
scripts/02_clean.py - so the effect of cleaning is visible as before/after
numbers.

Contents
  check_referential_integrity(fact_df, fact_key, dim_df, dim_key)
      ONE reusable function (keep it for Assignment 5). Returns match rate,
      orphan rows, distinct orphan keys and null-key rate. Applied to all 13
      relationships: 10 ID-based + 3 joins to the date table.
  Rule checks
      every column with a natural business bound: allowed code lists,
      patterns, valid ranges and sign rules.
  Reconciliations
      recomputed vs stored amounts, and the same fact in two tables.
  IQR outlier flags
      numeric columns with no natural upper bound.

Outputs (reports/)
  data_quality_scorecard.csv   one row per table per stage (raw / clean):
                               % completeness, % key uniqueness, % type validity,
                               % rule pass, % referential match
  ri_results.csv               every relationship x stage
  rule_results.csv             every rule x table x stage
  reconciliations.csv          every reconciliation x stage
  iqr_outlier_summary.csv      bounds and counts per column x stage
  iqr_outlier_flags.csv        one row per flagged value

Usage (from the hw04 folder):
  python scripts/03_validate.py
  python scripts/03_validate.py --data-dir "path/to/Wildcat Capital"
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from pandas.api.types import (is_bool_dtype, is_datetime64_any_dtype,
                                  is_numeric_dtype)
except ModuleNotFoundError as _e:   # friendly message instead of a traceback
    _req = Path(__file__).resolve().parent.parent / "requirements.txt"
    _cmd = f'"{sys.executable}" -m pip install -r "{_req}"'
    _fix = (f"    PowerShell (VS Code default):  & {_cmd}\n"
            f"    Command Prompt:                {_cmd}\n"
            if sys.platform.startswith("win") else f"    {_cmd}\n")
    sys.exit(f"\nMissing Python package: {_e.name}\n"
             f"The Python that ran this script does not have the required packages:\n"
             f"    {sys.executable}\n\n"
             f"Install the packages into exactly that Python (copy the whole line):\n"
             f"{_fix}then run the script again with that same Python.\n")

ROOT = Path(__file__).resolve().parent.parent          # hw04/
CLEAN_DIR = ROOT / "data" / "clean"
DEFAULT_OUT_DIR = ROOT / "reports"


def _load_profile_module():
    """Reuse SCHEMA / FOREIGN_KEYS / strict parsers from scripts/01_profile.py,
    so the audit, the cleaning and the validation apply identical rules."""
    path = Path(__file__).resolve().parent / "01_profile.py"
    if not path.exists():
        sys.exit(f"Cannot find {path} - 03_validate.py reuses its schema and helpers.")
    spec = importlib.util.spec_from_file_location("profile01", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load_profile_module()
TABLES = P.WILDCAT_TABLES
MONEY_TOL = 0.011        # two-decimal amounts: cents rounding
ALPHA_TOL = 2e-6         # six-decimal returns: rounding of three numbers
# Natural (business) keys for the scorecard's key-uniqueness measure, on top of
# the PK and SCHEMA business keys. A client is one person: the same first name,
# age, ZIP code and onboarding date should never appear twice. This catches
# duplicate clients that were given a new client_id and an altered surname.
NATURAL_KEYS = {
    "dim_client": ["first_name", "age", "zip_code", "client_since"],
}
IQR_COLUMNS = [          # numeric columns with no natural upper bound
    ("fact_portfolio_snapshot", "aum", "snapshot_id"),
    ("fact_transactions", "amount", "txn_id"),
    ("dim_client", "initial_aum", "client_id"),
]


# =============================================================================
# 1. REFERENTIAL INTEGRITY  (the reusable function - keep for Assignment 5)
# =============================================================================
def _as_key(s: pd.Series) -> pd.Series:
    """Express a key column as text the way a join would compare it.
    Works whether the DataFrame was read as text (dtype=str) or typed:
    integer-valued numbers -> '45', dates -> 'YYYY-MM-DD', text unchanged
    (so a text '45.0' stays '45.0' and correctly fails to match '45')."""
    if is_datetime64_any_dtype(s):
        return s.dt.strftime("%Y-%m-%d").astype("string")
    if is_numeric_dtype(s) and not is_bool_dtype(s):
        nn = s.dropna()
        if len(nn) and bool((nn % 1 == 0).all()):
            return s.astype("Int64").astype("string")
        return s.astype("string")
    return s.astype("string")


def check_referential_integrity(fact_df: pd.DataFrame, fact_key: str,
                                dim_df: pd.DataFrame, dim_key: str) -> dict:
    """Check that every non-null fact_df[fact_key] exists in dim_df[dim_key].

    Returns a dict:
        total_rows            rows in fact_df
        null_key_rows         rows whose key is null/blank
        null_key_rate_pct     null_key_rows / total_rows * 100
        non_null_rows         rows with a key value
        matched_rows          non-null keys found in the dimension
        match_rate_pct        matched_rows / non_null_rows * 100 (100 if no keys)
        orphan_rows           non-null keys NOT found in the dimension
        distinct_orphan_keys  number of different orphan key values
        orphan_key_examples   up to 5 most frequent orphan keys
        dim_key_unique        True if dim_key is unique (a valid 'one' side)
    """
    fk = _as_key(fact_df[fact_key])
    pk = _as_key(dim_df[dim_key])
    is_null = fk.fillna("").str.strip().eq("").astype(bool)
    keys = fk[~is_null]
    parent = set(pk.dropna())
    matched = keys.isin(parent).astype(bool)
    orphans = keys[~matched]
    total, non_null = len(fk), len(keys)
    return {
        "total_rows": total,
        "null_key_rows": int(is_null.sum()),
        "null_key_rate_pct": round(100 * is_null.sum() / total, 4) if total else 0.0,
        "non_null_rows": non_null,
        "matched_rows": int(matched.sum()),
        "match_rate_pct": round(100 * matched.sum() / non_null, 4) if non_null else 100.0,
        "orphan_rows": int((~matched).sum()),
        "distinct_orphan_keys": int(orphans.nunique()),
        "orphan_key_examples": "; ".join(map(str, orphans.value_counts().head(5).index)),
        "dim_key_unique": bool(pk.dropna().is_unique),
    }


# =============================================================================
# 2. RULE CHECKS (natural business bounds)
# =============================================================================
def _rule(results: list, stage: str, table: str, column: str, rule: str, kind: str,
          evaluated: pd.Series, passed: pd.Series, values: pd.Series) -> None:
    ev = evaluated.fillna(False).astype(bool)
    ok = passed.fillna(False).astype(bool) & ev
    bad = ev & ~ok
    results.append({
        "stage": stage, "table": table, "column": column, "rule": rule, "kind": kind,
        "evaluated": int(ev.sum()), "passed": int(ok.sum()), "failed": int(bad.sum()),
        "pass_pct": round(100 * ok.sum() / ev.sum(), 4) if ev.sum() else 100.0,
        "fail_examples": "; ".join(f"'{v}' ({c})" for v, c in
                                   values[bad].value_counts().head(5).items()),
    })


def run_rules(stage: str, D: dict) -> list:
    """Code lists, patterns and ranges come from the shared SCHEMA; sign and
    conditional rules are listed explicitly below."""
    out: list = []
    for t in TABLES:
        df = D[t]
        for col, spec in P.SCHEMA[t]["columns"].items():
            s = df[col]
            st = s.str.strip()
            nb = st != ""
            if "allowed" in spec:
                _rule(out, stage, t, col, f"in allowed list ({len(spec['allowed'])} codes)",
                      "code list", nb, st.isin(set(spec["allowed"])), st)
            if "pattern" in spec:
                _rule(out, stage, t, col, f"matches {spec['pattern']}", "pattern",
                      nb, st.str.fullmatch(spec["pattern"]), st)
            if any(k in spec for k in ("min", "max", "gt")):
                x = P.convert(s, spec["type"])
                ok = pd.Series(True, index=s.index)
                lo, hi = spec.get("min"), spec.get("max")
                if spec["type"] == "date":
                    lo = pd.Timestamp(lo) if lo is not None else None
                    hi = pd.Timestamp(hi) if hi is not None else None
                if lo is not None:
                    ok &= x >= lo
                if hi is not None:
                    ok &= x <= hi
                if "gt" in spec:
                    ok &= x > spec["gt"]
                label = " and ".join(
                    ([f"> {spec['gt']}"] if "gt" in spec else [])
                    + ([f">= {spec['min']}"] if "min" in spec else [])
                    + ([f"<= {spec['max']}"] if "max" in spec else []))
                kind = "sign" if spec.get("gt") == 0 and "max" not in spec else "range"
                _rule(out, stage, t, col, label, kind, x.notna(), ok, st)

        # flag columns added by 02_clean.py must be 0/1
        for col in [c for c in df.columns if c.endswith("_flag")]:
            _rule(out, stage, t, col, "flag is 0 or 1", "code list",
                  pd.Series(True, index=df.index), df[col].isin(["0", "1"]), df[col])

    # ---- explicit sign / conditional rules -------------------------------------
    tx = D["fact_transactions"]
    trade = tx["txn_type"].isin(P.TRADE_TXN_TYPES)
    sh = P.convert(tx["shares"], "float")
    _rule(out, stage, "fact_transactions", "shares", "Buy/Sell shares > 0 (direction is in txn_type)",
          "sign", trade & sh.notna(), sh > 0, tx["shares"])
    sec_type = tx["txn_type"].isin(P.SECURITY_TXN_TYPES)
    has_sec = tx["security_id"].str.strip() != ""
    _rule(out, stage, "fact_transactions", "security_id",
          "present for Buy/Sell/Dividend, blank for cash flows", "conditional",
          pd.Series(True, index=tx.index), has_sec == sec_type, tx["txn_type"])
    sec = D["dim_security"]
    etf = sec["is_etf"] == "True"
    _rule(out, stage, "dim_security", "expense_ratio_bps", "present exactly for ETFs",
          "conditional", pd.Series(True, index=sec.index),
          (sec["expense_ratio_bps"].str.strip() != "") == etf, sec["is_etf"])
    _rule(out, stage, "dim_security", "sector", "present exactly for non-ETFs",
          "conditional", pd.Series(True, index=sec.index),
          (sec["sector"].str.strip() != "") == ~etf, sec["is_etf"])
    return out


# =============================================================================
# 3. RECONCILIATIONS
# =============================================================================
def _recon(stage: str, name: str, scope: str, compared: int, not_comparable: int,
           mismatches: int, max_diff, note: str) -> dict:
    return {"stage": stage, "reconciliation": name, "scope": scope,
            "rows_compared": int(compared), "rows_not_comparable": int(not_comparable),
            "mismatches": int(mismatches),
            "match_pct": round(100 * (compared - mismatches) / compared, 4) if compared else 100.0,
            "max_abs_diff": None if max_diff is None or pd.isna(max_diff) else round(float(max_diff), 8),
            "note": note}


def run_reconciliations(stage: str, D: dict) -> list:
    f = lambda s: P.convert(s, "float")  # noqa: E731
    out = []

    # R1 within-table: fee amount recomputed from AUM basis and bps
    fe = D["fact_fees"]
    calc = f(fe["aum_basis"]) * f(fe["bps_charged"]) / 10000 / 12
    diff = (calc - f(fe["amount"])).abs()
    ok = diff.notna()
    out.append(_recon(stage, "fact_fees.amount = aum_basis x bps_charged / 10,000 / 12",
                      "within table (recomputed vs stored)", ok.sum(), (~ok).sum(),
                      (diff[ok] > MONEY_TOL).sum(), diff.max(), f"tolerance {MONEY_TOL}"))

    # R2 within-table: trade amount recomputed from shares x price
    tx = D["fact_transactions"]
    sec = tx["txn_type"].isin(P.SECURITY_TXN_TYPES)
    diff = (f(tx["shares"]) * f(tx["price"]) - f(tx["amount"])).abs()
    ok = sec & diff.notna()
    out.append(_recon(stage, "fact_transactions.amount = shares x price (Buy/Sell/Dividend)",
                      "within table (recomputed vs stored)", ok.sum(), (sec & ~ok).sum(),
                      (diff[ok] > MONEY_TOL).sum(), diff[ok].max(), f"tolerance {MONEY_TOL}"))

    # R3 within-table: alpha identity
    sn = D["fact_portfolio_snapshot"]
    diff = (f(sn["return_ytd"]) - f(sn["benchmark_return_ytd"]) - f(sn["active_alpha_ytd"])).abs()
    ok = diff.notna()
    out.append(_recon(stage, "active_alpha_ytd = return_ytd - benchmark_return_ytd",
                      "within table (recomputed vs stored)", ok.sum(), (~ok).sum(),
                      (diff[ok] > ALPHA_TOL).sum(), diff.max(),
                      f"tolerance {ALPHA_TOL}; rows with unparseable values are not comparable"))

    # R4 cross-table: fee AUM basis vs snapshot AUM for the same client & month
    m = fe[["client_id", "fee_date", "aum_basis"]].merge(
        sn[["client_id", "snapshot_date", "aum"]], how="left",
        left_on=["client_id", "fee_date"], right_on=["client_id", "snapshot_date"])
    assert len(m) == len(fe), "snapshot (client, date) is not unique - merge would duplicate fees"
    diff = (f(m["aum_basis"]) - f(m["aum"])).abs()
    ok = diff.notna()
    out.append(_recon(stage, "fact_fees.aum_basis = fact_portfolio_snapshot.aum (same client & month)",
                      "cross-table", ok.sum(), (~ok).sum(), (diff[ok] > MONEY_TOL).sum(),
                      diff.max(), "not comparable = fee with no snapshot for that client-month"))

    # R5 cross-table: advisor on each fact = advisor on dim_client
    cl = D["dim_client"][["client_id", "advisor_id"]]
    for fact in ("fact_fees", "fact_portfolio_snapshot", "fact_transactions"):
        m = D[fact][["client_id", "advisor_id"]].merge(cl, on="client_id", how="left",
                                                       suffixes=("", "_dim"))
        ok = m["advisor_id_dim"].notna()
        out.append(_recon(stage, f"{fact}.advisor_id = dim_client.advisor_id", "cross-table",
                          ok.sum(), (~ok).sum(),
                          (m.loc[ok, "advisor_id"] != m.loc[ok, "advisor_id_dim"]).sum(), None,
                          "not comparable = client_id not found in dim_client"))

    # R6 cross-table: snapshot account type = client's primary account type
    m = sn[["client_id", "account_type"]].merge(
        D["dim_client"][["client_id", "primary_account_type"]], on="client_id", how="left")
    ok = m["primary_account_type"].notna()
    out.append(_recon(stage, "fact_portfolio_snapshot.account_type = dim_client.primary_account_type",
                      "cross-table", ok.sum(), (~ok).sum(),
                      (m.loc[ok, "account_type"] != m.loc[ok, "primary_account_type"]).sum(), None, ""))

    # R7 cross-table: Advisory Fee debits vs fact_fees, client-year (documented limitation)
    adv = tx[tx["txn_type"] == "Advisory Fee"]
    a = f(adv["amount"]).groupby([adv["client_id"], adv["txn_date"].str[:4]]).sum()
    b = f(fe["amount"]).groupby([fe["client_id"], fe["fee_date"].str[:4]]).sum()
    j = pd.concat([a.rename("debits"), b.rename("fees")], axis=1)
    both = j.dropna()
    diff = (both["debits"] - both["fees"]).abs()
    out.append(_recon(stage, "Advisory Fee debits (fact_transactions) = fact_fees total, per client-year",
                      "cross-table", len(both), len(j) - len(both),
                      (diff > 0.10 * both["fees"]).sum(), diff.max(),
                      "tolerance 10%; known limitation - fact_fees is the system of record"))
    return out


# =============================================================================
# 4. IQR OUTLIER FLAGS
# =============================================================================
def run_iqr(stage: str, D: dict) -> tuple[list, pd.DataFrame]:
    summary, flags = [], []
    for t, col, key in IQR_COLUMNS:
        df = D[t]
        x = P.convert(df[col], "float")
        q1, q3 = x.quantile(0.25), x.quantile(0.75)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        low, high = x < lo, x > hi
        flagged = low | high
        summary.append({"stage": stage, "table": t, "column": col, "values": int(x.notna().sum()),
                        "q1": round(q1, 2), "q3": round(q3, 2), "iqr": round(iqr, 2),
                        "lower_fence": round(lo, 2), "upper_fence": round(hi, 2),
                        "flagged_low": int(low.sum()), "flagged_high": int(high.sum()),
                        "flagged_pct": round(100 * flagged.sum() / x.notna().sum(), 4)})
        flags.append(pd.DataFrame({"stage": stage, "table": t, "key_column": key,
                                   "key": df.loc[flagged, key].to_numpy(), "column": col,
                                   "value": df.loc[flagged, col].to_numpy(),
                                   "side": np.where(high[flagged], "high", "low"),
                                   "lower_fence": round(lo, 2), "upper_fence": round(hi, 2)}))
    return summary, pd.concat(flags, ignore_index=True)


def iqr_interpretation(D: dict) -> str:
    """Data-driven sentence: who are the clients behind the high AUM flags?"""
    sn, cl = D["fact_portfolio_snapshot"], D["dim_client"]
    x = P.convert(sn["aum"], "float")
    hi = x > x.quantile(0.75) + 1.5 * (x.quantile(0.75) - x.quantile(0.25))
    tier = sn.loc[hi, "client_id"].map(cl.set_index("client_id")["client_tier"])
    share = (tier == "UHNW").mean() if len(tier) else 0.0
    return (f"Flagged does not mean wrong: {share:.0%} of the high-AUM snapshot flags belong to "
            "UHNW clients, whose $5M+ balances are real but sit far above the median, so an IQR "
            "flag only says a value is unusual for this column and is a prompt to investigate, "
            "not a reason to change or delete it.")


# =============================================================================
# 5. SCORECARD
# =============================================================================
def scorecard_rows(stage: str, D: dict, ri: list, rules: list) -> list:
    rows = []
    child_fks = {}
    for r in ri:
        child_fks.setdefault(r["fact_table"], []).append(r)
    for t in TABLES:
        df = D[t]
        sch = P.SCHEMA[t]
        cols = sch["columns"]
        pk = sch["pk"]
        n = len(df)

        # completeness: required cells (required columns, PK, non-nullable FKs) that are filled.
        # Structural blanks (ETF sector, cash-flow security_id, ...) are excluded on purpose.
        nonnull_fk = {c for (ch, c, _p, _pc, nullable, _k) in P.FOREIGN_KEYS if ch == t and not nullable}
        req = [c for c in cols if cols[c].get("required") or c == pk or c in nonnull_fk]
        filled = sum(int((df[c].str.strip() != "").sum()) for c in req)
        completeness = 100 * filled / (n * len(req)) if n and req else 100.0

        # key uniqueness: PK, business key and natural keys - share of rows whose key is unique
        key_sets = [[pk]] + ([sch["business_key"]] if sch.get("business_key") else []) \
            + [[c] for c in cols if cols[c].get("unique")] \
            + ([NATURAL_KEYS[t]] if t in NATURAL_KEYS else [])
        uniq_rows = sum(int((~df[k].duplicated(keep=False)).sum()) for k in key_sets)
        uniqueness = 100 * uniq_rows / (n * len(key_sets)) if n else 100.0

        # type validity: non-blank values in typed columns that parse as the schema type
        typed = [c for c in cols if cols[c]["type"] in ("int", "float", "date", "bool")]
        nb_total = valid = 0
        for c in typed:
            nb = df[c].str.strip() != ""
            nb_total += int(nb.sum())
            valid += int((nb & P.parses(df[c], cols[c]["type"], cols[c])).sum())
        validity = 100 * valid / nb_total if nb_total else 100.0

        # rule pass: all rule evaluations for this table
        tr = [r for r in rules if r["table"] == t]
        ev = sum(r["evaluated"] for r in tr)
        rule_pass = 100 * sum(r["passed"] for r in tr) / ev if ev else 100.0

        # referential match: non-null FK values found in their parent (blank if table has no FK)
        fks = child_fks.get(t, [])
        nn = sum(r["non_null_rows"] for r in fks)
        ref = 100 * sum(r["matched_rows"] for r in fks) / nn if nn else None

        rows.append({"table": t, "stage": stage, "rows": n,
                     "pct_completeness": round(completeness, 4),
                     "pct_key_uniqueness": round(uniqueness, 4),
                     "pct_type_validity": round(validity, 4),
                     "pct_rule_pass": round(rule_pass, 4),
                     "pct_referential_match": round(ref, 4) if ref is not None else None,
                     "rules_evaluated": len(tr), "fk_relationships": len(fks)})
    return rows


# =============================================================================
# DRIVER
# =============================================================================
def load_stage(folder: Path, label: str) -> dict:
    missing = [t for t in TABLES if not (folder / f"{t}.csv").exists()]
    if missing:
        hint = ("Run  python scripts/02_clean.py  first." if label == "clean"
                else 'Use --data-dir "path/to/Wildcat Capital".')
        sys.exit(f"\n{label} stage: missing {', '.join(missing)} in {folder}\n{hint}\n")
    return {t: P.read_table(folder / f"{t}.csv") for t in TABLES}


def find_raw_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    d = next((x for x in P.DEFAULT_DATA_DIRS
              if x.is_dir() and all((x / f"{t}.csv").exists() for t in TABLES)), None)
    if d is None:
        sys.exit("\nCould not find the 8 raw Wildcat Capital CSV files. Looked in:\n    "
                 + "\n    ".join(str(x) for x in P.DEFAULT_DATA_DIRS)
                 + '\n\nUse  python scripts/03_validate.py --data-dir "path/to/Wildcat Capital"\n')
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description="Referential integrity + validation, raw vs clean.")
    ap.add_argument("--data-dir", help="folder with the 8 RAW CSVs (default: auto-detect)")
    ap.add_argument("--clean-dir", default=str(CLEAN_DIR), help="folder with the clean CSVs")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="where reports are written")
    args = ap.parse_args()

    stages = {"raw": load_stage(find_raw_dir(args.data_dir), "raw"),
              "clean": load_stage(Path(args.clean_dir), "clean")}

    ri_all, rules_all, recon_all, iqr_all, flag_frames, score = [], [], [], [], [], []
    for stage, D in stages.items():
        print(f"\n===== {stage.upper()} =====")
        ri = []
        for child, ccol, parent, pcol, nullable, kind in P.FOREIGN_KEYS:
            res = check_referential_integrity(D[child], ccol, D[parent], pcol)
            ri.append({"stage": stage, "relationship": f"{child}.{ccol} -> {parent}.{pcol}",
                       "kind": "date" if kind == "date" else "id", "fact_table": child,
                       "fact_key": ccol, "dim_table": parent, "dim_key": pcol,
                       "nullable": nullable, **res})
        rules = run_rules(stage, D)
        recon = run_reconciliations(stage, D)
        iqr, flags = run_iqr(stage, D)
        score += scorecard_rows(stage, D, ri, rules)
        ri_all += ri
        rules_all += rules
        recon_all += recon
        iqr_all += iqr
        flag_frames.append(flags)

        with pd.option_context("display.width", 220, "display.max_colwidth", 60):
            print("\nReferential integrity")
            print(pd.DataFrame(ri)[["relationship", "match_rate_pct", "orphan_rows",
                                    "distinct_orphan_keys", "null_key_rate_pct"]].to_string(index=False))
            rf = pd.DataFrame(rules)
            print(f"\nRules: {len(rf)} checks, {int((rf['failed'] > 0).sum())} with failures")
            print(rf[rf["failed"] > 0][["table", "column", "rule", "failed", "pass_pct"]]
                  .to_string(index=False))
            print("\nReconciliations")
            print(pd.DataFrame(recon)[["reconciliation", "rows_compared", "rows_not_comparable",
                                       "mismatches", "match_pct"]].to_string(index=False))
            print("\nIQR outliers")
            print(pd.DataFrame(iqr)[["table", "column", "lower_fence", "upper_fence",
                                     "flagged_low", "flagged_high", "flagged_pct"]].to_string(index=False))

    sentence = iqr_interpretation(stages["clean"])
    print(f"\n{sentence}")

    # ---- checks on this script's own results -----------------------------------
    assert len(P.FOREIGN_KEYS) == 13 and sum(fk[5] == "id" for fk in P.FOREIGN_KEYS) == 10, \
        "expected 10 ID-based + 3 date relationships"
    assert all(r["dim_key_unique"] for r in ri_all), "a dimension key is not unique"

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sc = pd.DataFrame(score)
    sc["_t"] = sc["table"].map({t: i for i, t in enumerate(TABLES)})
    sc["_s"] = sc["stage"].map({"raw": 0, "clean": 1})
    sc = sc.sort_values(["_t", "_s"]).drop(columns=["_t", "_s"]).reset_index(drop=True)
    sc.to_csv(out / "data_quality_scorecard.csv", index=False, encoding="utf-8")
    pd.DataFrame(ri_all).to_csv(out / "ri_results.csv", index=False, encoding="utf-8")
    pd.DataFrame(rules_all).to_csv(out / "rule_results.csv", index=False, encoding="utf-8")
    pd.DataFrame(recon_all).to_csv(out / "reconciliations.csv", index=False, encoding="utf-8")
    iqr_df = pd.DataFrame(iqr_all)
    iqr_df["interpretation"] = sentence
    iqr_df.to_csv(out / "iqr_outlier_summary.csv", index=False, encoding="utf-8")
    pd.concat(flag_frames, ignore_index=True).to_csv(out / "iqr_outlier_flags.csv", index=False,
                                                     encoding="utf-8")

    print("\nData quality scorecard")
    with pd.option_context("display.width", 220):
        print(sc.to_string(index=False))
    print(f"\nSaved to {out}: data_quality_scorecard.csv, ri_results.csv, rule_results.csv, "
          "reconciliations.csv, iqr_outlier_summary.csv, iqr_outlier_flags.csv")


if __name__ == "__main__":
    main()
