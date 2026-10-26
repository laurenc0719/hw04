#!/usr/bin/env python3
"""
scripts/02_clean.py  --  HW4 Part C: cleaning program
MIS3060 Business Intelligence with AI | Wildcat Capital

Reads the eight raw Wildcat Capital CSVs (as text) and writes a NEW set of
eight files to data/clean/. The raw files are never modified (checked with a
SHA-256 hash before and after).

Every transformation follows decision_log.md and runs one at a time:
    1. compute the BEFORE state
    2. apply the change
    3. print rows affected + before/after check
    4. `assert` the AFTER state  ->  the script stops if a fix did not work
Repairs are reconciled against other columns/tables instead of guessed.

Outputs
    data/clean/<same 8 file names>.csv     same columns, plus 0/1 flag columns
    data/clean/README_clean.md             relationships, BI column types, flags, limitations
    data/audit/cleaning_log.csv            step, table, issue, action, rows_affected, note
    data/audit/value_changes.csv           table, key_column, key, column, old_value, new_value
    data/audit/quarantine_*.csv            one file per quarantined set

Usage (from the hw04 folder):
    python scripts/02_clean.py
    python scripts/02_clean.py --data-dir "path/to/Wildcat Capital"

Shares SCHEMA / FOREIGN_KEYS / parsing helpers with scripts/01_profile.py so the
audit and the cleaning use exactly the same rules.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
except ModuleNotFoundError as _e:   # friendly message instead of a traceback
    _req = Path(__file__).resolve().parent.parent / "requirements.txt"
    _cmd = f'"{sys.executable}" -m pip install -r "{_req}"'
    _fix = (f"    PowerShell (VS Code default):  & {_cmd}\n"
            f"    Command Prompt:                {_cmd}\n"
            if sys.platform.startswith("win") else f"    {_cmd}\n")
    sys.exit(f"\nMissing Python package: {_e.name}\n"
             f"The Python that ran this script does not have the required packages:\n"
             f"    {sys.executable}\n\n"
             f"EASIEST FIX (Windows): run  run_all.bat  in the hw04 folder.\n\n"
             f"Or install the packages into exactly that Python (copy the whole line):\n"
             f"{_fix}then run the script again with that same Python.\n")

ROOT = Path(__file__).resolve().parent.parent          # hw04/
CLEAN_DIR = ROOT / "data" / "clean"
AUDIT_DIR = ROOT / "data" / "audit"


def _load_profile_module():
    """Import scripts/01_profile.py (its name starts with a digit, so importlib)."""
    path = Path(__file__).resolve().parent / "01_profile.py"
    if not path.exists():
        sys.exit(f"Cannot find {path} - 02_clean.py reuses its schema and helpers.")
    spec = importlib.util.spec_from_file_location("profile01", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load_profile_module()
TABLES = P.WILDCAT_TABLES
FACTS_WITH_CLIENT = ["fact_fees", "fact_portfolio_snapshot", "fact_transactions"]

STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY"}
NO_CERT_LABEL = "No Certification"
AUM_FLOOR = 1000.0
MONEY_TOL = 0.011          # cents rounding on two-decimal amounts
ALPHA_TOL_PCT = 1e-4       # percent strings carry 2 decimals -> +-0.00005


# =============================================================================
# AUDIT TRAIL
# =============================================================================
class Audit:
    def __init__(self) -> None:
        self.log: list[dict] = []
        self.changes: list[pd.DataFrame] = []
        self.quarantine: dict[str, tuple[str, pd.DataFrame]] = {}   # name -> (table, rows)
        self.flags: list[dict] = []

    def step(self, table: str, issue: str, action: str, rows: int, note: str = "") -> None:
        self.log.append({"step": len(self.log) + 1, "table": table, "issue": issue,
                         "action": action, "rows_affected": int(rows), "note": note})

    def changed(self, table: str, key_col: str, df: pd.DataFrame, mask: pd.Series,
                col: str, old: pd.Series, new: pd.Series) -> None:
        if mask.any():
            self.changes.append(pd.DataFrame({
                "table": table, "key_column": key_col,
                "key": df.loc[mask, key_col].to_numpy(), "column": col,
                "old_value": old[mask].to_numpy(), "new_value": new[mask].to_numpy()}))

    def quarantined_rows(self, table: str) -> int:
        return sum(len(rows) for t, rows in self.quarantine.values() if t == table)


def banner(n: int, title: str) -> None:
    print(f"\n[Step {n:02d}] {title}")


def show(rows: int, before: str, after: str) -> None:
    print(f"    rows affected : {rows:,}")
    print(f"    before        : {before}")
    print(f"    after         : {after}")


def num(s: pd.Series) -> pd.Series:
    return P.convert(s, "float")


def nonblank(s: pd.Series) -> pd.Series:
    return s.str.strip() != ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# =============================================================================
# TRANSFORMATIONS  (numbers refer to decision_log.md entries)
# =============================================================================
def fix_security_id_format(D: dict, A: Audit, n: int) -> None:
    """#2 security_id stored as '45.0' -> '45' (verified against dim_security)."""
    banner(n, "fact_transactions.security_id: integer keys stored as decimals (log #2)")
    t, col = "fact_transactions", "security_id"
    df = D[t]
    s = df[col]
    nb = nonblank(s)
    dim_keys = set(D["dim_security"]["security_id"])
    before_rate = s[nb].isin(dim_keys).mean()
    m = nb & s.str.strip().str.fullmatch(r"\d+\.0+")
    assert not (nb & ~m & ~s.str.fullmatch(r"\d+")).any(), \
        "unexpected security_id format (not 'n' or 'n.0') - stop and investigate"
    new = s.copy()
    new[m] = pd.to_numeric(s[m]).astype("int64").astype(str)
    after_rate = new[nb].isin(dim_keys).mean()
    A.changed(t, "txn_id", df, m, col, s, new)
    df[col] = new
    show(int(m.sum()), f"text match to dim_security = {before_rate:.2%}",
         f"text match to dim_security = {after_rate:.2%}")
    assert df.loc[nb, col].str.fullmatch(r"\d+").all(), "security_id still not integer text"
    assert after_rate == 1.0, "some security_id values still do not match dim_security"
    assert (df[col] == "").sum() == (~nb).sum(), "blank security_id count changed"
    A.step(t, "security_id stored as decimal text ('45.0')", "convert to integer text",
           int(m.sum()), f"text join match {before_rate:.2%} -> {after_rate:.2%}")


def fix_return_percent_strings(D: dict, A: Audit, n: int) -> None:
    """#5 return_ytd '-0.64%' -> recomputed as benchmark_return_ytd + active_alpha_ytd.
    The percent string has only 2 decimals, so it is used to VERIFY the recomputed
    value (must agree within rounding), not as the new value."""
    banner(n, "fact_portfolio_snapshot.return_ytd: percent strings (log #5)")
    t, col = "fact_portfolio_snapshot", "return_ytd"
    df = D[t]
    s = df[col]
    m = s.str.strip().str.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)\s*%")
    before_fail = int((num(s).isna() & nonblank(s)).sum())
    pct = s[m].map(lambda v: Decimal(v.strip().rstrip("%").strip()) / Decimal(100))
    recomputed = (df.loc[m, "benchmark_return_ytd"].map(lambda v: Decimal(v.strip()))
                  + df.loc[m, "active_alpha_ytd"].map(lambda v: Decimal(v.strip())))
    gap = (recomputed - pct).abs()
    # reconcile BEFORE changing: recomputed value must round to the stored percent
    assert (gap <= Decimal(str(ALPHA_TOL_PCT))).all(), \
        "benchmark + alpha disagrees with the percent string - cannot confirm the repair"
    new = s.copy()
    new[m] = recomputed.map(lambda d: format(d, "f"))
    A.changed(t, "snapshot_id", df, m, col, s, new)
    df[col] = new
    after_fail = int((num(df[col]).isna() & nonblank(df[col])).sum())
    diff = (num(df[col]) - num(df["benchmark_return_ytd"]) - num(df["active_alpha_ytd"])).abs()
    show(int(m.sum()), f"{before_fail} values fail to parse as numbers",
         f"{after_fail} fail; max |recomputed - percent/100| = {float(gap.max()):.6f}; "
         f"max |return - benchmark - alpha| on changed rows = {diff[m].max():.2e}")
    assert after_fail == 0, "return_ytd still has unparseable values"
    assert (diff[m] <= 2e-6).all(), "alpha identity does not hold on the changed rows"
    A.step(t, "return_ytd stored as percent strings ('-0.64%')",
           "convert: recompute as benchmark_return_ytd + active_alpha_ytd", int(m.sum()),
           f"verified against the percent string (max gap {float(gap.max()):.6f}, within its "
           "2-decimal rounding); full 6-decimal precision kept")


def fix_state_names(D: dict, A: Audit, n: int) -> None:
    """#6 'Pennsylvania' -> 'PA'."""
    banner(n, "dim_client.state: full state names (log #6)")
    t, col = "dim_client", "state"
    df = D[t]
    s = df[col]
    m = nonblank(s) & ~s.isin(P.US_STATES)
    mapped = s[m].str.strip().str.lower().map(STATE_NAME_TO_CODE)
    assert mapped.notna().all(), f"unmappable state values: {s[m][mapped.isna()].unique()}"
    new = s.copy()
    new[m] = mapped
    A.changed(t, "client_id", df, m, col, s, new)
    before = s[m].value_counts().to_dict()
    df[col] = new
    bad_after = int((nonblank(df[col]) & ~df[col].isin(P.US_STATES)).sum())
    show(int(m.sum()), f"{int(m.sum())} non-code values {before}", f"{bad_after} non-code values")
    assert bad_after == 0, "state still contains values that are not USPS codes"
    A.step(t, "state written as full name", "standardize to USPS code", int(m.sum()),
           "; ".join(f"{k} -> {STATE_NAME_TO_CODE[k.lower()]} ({v})" for k, v in before.items()))


def fix_certification_placeholder(D: dict, A: Audit, n: int) -> None:
    """#8 'None' -> 'No Certification'."""
    banner(n, "dim_advisor.certifications: text placeholder 'None' (log #8)")
    t, col = "dim_advisor", "certifications"
    df = D[t]
    s = df[col]
    m = s.str.strip().str.upper().isin(P.NULL_TOKENS)
    new = s.copy()
    new[m] = NO_CERT_LABEL
    A.changed(t, "advisor_id", df, m, col, s, new)
    df[col] = new
    left = int(df[col].str.strip().str.upper().isin(P.NULL_TOKENS).sum())
    show(int(m.sum()), f"{int(m.sum())} null-like tokens ({s[m].unique().tolist()})",
         f"{left} null-like tokens")
    assert left == 0, "null-like tokens remain in certifications"
    assert (df[col] == NO_CERT_LABEL).sum() == m.sum()
    A.step(t, "certifications = literal text 'None'", f"standardize to '{NO_CERT_LABEL}'",
           int(m.sum()), "means 'no certification' (a real answer), not missing data")


def quarantine_duplicate_clients(D: dict, A: Audit, n: int) -> set:
    """#1 clients identical in every column except last_name (and client_id)."""
    banner(n, "dim_client: duplicate clients with altered surnames (log #1)")
    t = "dim_client"
    c = D[t]
    activity = pd.concat([D[f]["client_id"] for f in FACTS_WITH_CLIENT]).value_counts()
    act = c["client_id"].map(activity).fillna(0).astype(int)
    attrs = [col for col in c.columns if col not in ("client_id", "last_name")]
    sig = c[attrs].agg("\x1f".join, axis=1)

    def similar(a: str, b: str) -> bool:
        a, b = a.strip().lower(), b.strip().lower()
        return (a != b and (a.startswith(b) or b.startswith(a))) or \
            difflib.SequenceMatcher(None, a, b).ratio() >= 0.8

    pairs, ambiguous = [], []
    for _, idx in c.groupby(sig).groups.items():
        idx = list(idx)
        for i in range(len(idx)):
            for j in range(i + 1, len(idx)):
                a, b = idx[i], idx[j]
                if not similar(c.at[a, "last_name"], c.at[b, "last_name"]):
                    continue
                if act[a] > 0 and act[b] == 0:
                    pairs.append((b, a))          # (duplicate, keeper)
                elif act[b] > 0 and act[a] == 0:
                    pairs.append((a, b))
                else:
                    ambiguous.append((c.at[a, "client_id"], c.at[b, "client_id"]))
    assert not ambiguous, f"duplicate pairs where the keeper is unclear: {ambiguous}"
    dup_idx = [d for d, _ in pairs]
    assert len(set(dup_idx)) == len(dup_idx), "a row was matched as duplicate twice"
    q = c.loc[dup_idx].copy()
    q["duplicate_of_client_id"] = [c.at[k, "client_id"] for _, k in pairs]
    q["duplicate_of_last_name"] = [c.at[k, "last_name"] for _, k in pairs]
    q["quarantine_reason"] = ("identical to duplicate_of_client_id in every column except "
                              "last_name; no fact rows reference it")
    before = len(c)
    D[t] = c.drop(index=dup_idx)
    A.quarantine["quarantine_dim_client_duplicates"] = (t, q)
    ids = set(q["client_id"])
    refs = sum(int(D[f]["client_id"].isin(ids).sum()) for f in FACTS_WITH_CLIENT)
    keepers_present = q["duplicate_of_client_id"].isin(D[t]["client_id"]).all()
    show(len(q), f"{before:,} clients", f"{len(D[t]):,} clients; {len(q)} quarantined; "
         f"fact rows referencing quarantined ids = {refs}")
    assert refs == 0, "a quarantined client is referenced by a fact table"
    assert keepers_present, "a kept original is missing from dim_client"
    assert len(D[t]) + len(q) == before
    assert D[t]["client_id"].is_unique
    A.step(t, "duplicate clients (same in all columns except a truncated/altered last_name)",
           "quarantine", len(q), "verified: 0 fact rows reference them; original kept for each")
    return ids


def quarantine_orphan_transactions(D: dict, A: Audit, n: int, dup_ids: set) -> None:
    """#3 transactions whose client_id is not in dim_client."""
    banner(n, "fact_transactions.client_id: orphan transactions (log #3)")
    t = "fact_transactions"
    df = D[t]
    valid = set(D["dim_client"]["client_id"])
    m = ~df["client_id"].isin(valid)
    assert not df.loc[m, "client_id"].isin(dup_ids).any(), \
        "our own duplicate quarantine created orphans - stop"
    q = df[m].copy()
    q["quarantine_reason"] = "client_id not found in dim_client (no way to attach the client)"
    before = len(df)
    D[t] = df[~m]
    A.quarantine["quarantine_fact_transactions_orphans"] = (t, q)
    amt = num(q["amount"]).sum()
    show(int(m.sum()), f"{before:,} rows; {int(m.sum())} with unknown client_id",
         f"{len(D[t]):,} rows; {int((~D[t]['client_id'].isin(valid)).sum())} with unknown client_id")
    assert D[t]["client_id"].isin(valid).all(), "orphan transactions remain"
    assert len(D[t]) + len(q) == before
    A.step(t, "client_id not in dim_client (orphans)", "quarantine", int(m.sum()),
           f"{q['client_id'].nunique()} distinct ids; amount ${amt:,.2f}; "
           f"types {q['txn_type'].value_counts().to_dict()}")


def fix_negative_buy_shares(D: dict, A: Audit, n: int) -> None:
    """#4 Buy rows with negative shares -> absolute value (verified against amount)."""
    banner(n, "fact_transactions.shares: negative shares on Buy rows (log #4)")
    t, col = "fact_transactions", "shares"
    df = D[t]
    sh, px, amt = num(df[col]), num(df["price"]), num(df["amount"])
    trade = df["txn_type"].isin(P.TRADE_TXN_TYPES)
    m = trade & (sh < 0)
    # reconcile BEFORE changing anything: the sign must be the only problem
    abs_ok = ((sh.abs() * px - amt).abs() <= MONEY_TOL)
    assert abs_ok[m].all(), "some negative-share rows do not reconcile even after abs() - stop"
    new = df[col].copy()
    new[m] = df.loc[m, col].str.strip().str.lstrip("-")
    A.changed(t, "txn_id", df, m, col, df[col], new)
    df[col] = new
    sh2 = num(df[col])
    sec = df["txn_type"].isin(P.SECURITY_TXN_TYPES)
    recon_bad = int((sec & ((sh2 * px - amt).abs() > MONEY_TOL)).sum())
    show(int(m.sum()), f"{int(m.sum())} trades with shares <= 0",
         f"{int((trade & (sh2 <= 0)).sum())} trades with shares <= 0; "
         f"{recon_bad} security rows where amount != shares x price")
    assert (sh2[trade] > 0).all(), "trades with non-positive shares remain"
    assert recon_bad == 0, "amount = shares x price does not hold for every security row"
    A.step(t, "Buy rows with negative shares", "convert to absolute value", int(m.sum()),
           "verified: abs(shares) x price = amount (+-$0.01) for every changed row; "
           "afterwards the identity holds for all security rows")


def fill_risk_profile(D: dict, A: Audit, n: int) -> None:
    """#7 blank risk_profile filled from the benchmark the firm assigned."""
    banner(n, "dim_client.risk_profile: blanks filled from assigned benchmark (log #7)")
    t, col = "dim_client", "risk_profile"
    c = D[t]
    sn = D["fact_portfolio_snapshot"]
    per_client = sn.groupby("client_id")["benchmark_id"].nunique()
    assert (per_client == 1).all(), "a client has more than one benchmark - mapping unsafe"
    bench = c["client_id"].map(sn.drop_duplicates("client_id").set_index("client_id")["benchmark_id"])
    blank = c[col].str.strip() == ""
    known = pd.DataFrame({"risk": c.loc[~blank, col], "bench": bench[~blank]}).dropna()
    profiles_per_bench = known.groupby("bench")["risk"].unique()
    unambiguous = {b: p[0] for b, p in profiles_per_bench.items() if len(p) == 1}
    fill = bench.map(unambiguous)
    m = blank & fill.notna()
    new = c[col].copy()
    new[m] = fill[m]
    A.changed(t, "client_id", c, m, col, c[col], new)
    c[col] = new
    c["risk_profile_imputed_flag"] = m.astype(int).astype(str)
    left = int((c[col].str.strip() == "").sum())
    # after-state: the benchmark -> profile mapping must be unchanged (no new conflicts)
    after = pd.DataFrame({"risk": c[col], "bench": bench}).dropna()
    after = after[after["risk"] != ""].groupby("bench")["risk"].unique()
    show(int(m.sum()), f"{int(blank.sum())} blank risk_profile; benchmark used by blanks: "
         f"{bench[blank].value_counts(dropna=False).to_dict()}",
         f"{left} blank; filled {fill[m].value_counts().to_dict()}")
    for b, profs in after.items():
        assert set(profs) == set(profiles_per_bench.get(b, profs)), \
            f"fill created a new benchmark->profile combination for benchmark {b}"
    assert int(c["risk_profile_imputed_flag"].astype(int).sum()) == int(m.sum())
    assert left == int((blank & fill.isna()).sum())
    A.step(t, "risk_profile blank", "fill from benchmark (only where benchmark maps to one "
           "profile) + risk_profile_imputed_flag", int(m.sum()),
           f"benchmark->profile used: {unambiguous}; {left} blanks left (no unambiguous source)")


def add_aum_floor_flags(D: dict, A: Audit, n: int) -> None:
    """#9 AUM of exactly $1,000 (system floor) in snapshots and fees."""
    banner(n, "aum_floor_flag on fact_portfolio_snapshot and fact_fees (log #9)")
    sn, fe = D["fact_portfolio_snapshot"], D["fact_fees"]
    ms = num(sn["aum"]) == AUM_FLOOR
    mf = num(fe["aum_basis"]) == AUM_FLOOR
    sn["aum_floor_flag"] = ms.astype(int).astype(str)
    fe["aum_floor_flag"] = mf.astype(int).astype(str)
    # reconcile: every flagged fee must sit on a flagged snapshot (same client & month)
    snap_keys = set(zip(sn.loc[ms, "client_id"], sn.loc[ms, "snapshot_date"].str[:7]))
    fee_keys = list(zip(fe.loc[mf, "client_id"], fe.loc[mf, "fee_date"].str[:7]))
    unmatched = sum(k not in snap_keys for k in fee_keys)
    show(int(ms.sum()) + int(mf.sum()), "no flag columns",
         f"snapshots flagged {int(ms.sum())} ({sn.loc[ms, 'client_id'].nunique()} clients); "
         f"fees flagged {int(mf.sum())}; flagged fees without flagged snapshot = {unmatched}")
    assert sn["aum_floor_flag"].isin(["0", "1"]).all() and fe["aum_floor_flag"].isin(["0", "1"]).all()
    assert unmatched == 0, "fee floor rows do not line up with snapshot floor rows"
    A.step("fact_portfolio_snapshot", "aum exactly $1,000 (floor/placeholder)", "flag aum_floor_flag",
           int(ms.sum()), f"{sn.loc[ms, 'client_id'].nunique()} clients")
    A.step("fact_fees", "aum_basis exactly $1,000 (same floor)", "flag aum_floor_flag",
           int(mf.sum()), "verified: every flagged fee matches a flagged snapshot (client, month)")


def add_return_outlier_flag(D: dict, A: Audit, n: int) -> None:
    """#10 YTD return outside -100%..+100%."""
    banner(n, "return_outlier_flag on fact_portfolio_snapshot (log #10)")
    sn = D["fact_portfolio_snapshot"]
    r = num(sn["return_ytd"])
    assert r.notna().all(), "return_ytd must be numeric before flagging (step order)"
    m = (r > 1) | (r < -1)
    sn["return_outlier_flag"] = m.astype(int).astype(str)
    show(int(m.sum()), f"max return_ytd = {r.max():.4f}",
         f"{int(m.sum())} rows flagged; max unflagged = {r[~m].max():.4f}")
    assert r[~m].between(-1, 1).all()
    A.step("fact_portfolio_snapshot", "return_ytd outside -100%..+100% YTD", "flag return_outlier_flag",
           int(m.sum()), f"{sn.loc[m, 'client_id'].nunique()} clients; max {r.max():.2f}")


def add_alpha_outlier_flag(D: dict, A: Audit, n: int) -> None:
    """#10 (cont.) active alpha outside -100%..+100% - includes rows whose return
    is inside the range but whose alpha is not (negative benchmark years)."""
    banner(n, "alpha_outlier_flag on fact_portfolio_snapshot (log #10)")
    sn = D["fact_portfolio_snapshot"]
    a = num(sn["active_alpha_ytd"])
    assert a.notna().all(), "active_alpha_ytd must be numeric before flagging"
    m = a.abs() > 1
    sn["alpha_outlier_flag"] = m.astype(int).astype(str)
    only_alpha = m & (sn["return_outlier_flag"] == "0")
    show(int(m.sum()), f"max |active_alpha_ytd| = {a.abs().max():.4f}",
         f"{int(m.sum())} rows flagged ({int(only_alpha.sum())} not already return outliers); "
         f"max unflagged |alpha| = {a[~m].abs().max():.4f}")
    assert a[~m].abs().le(1).all()
    assert int(sn["alpha_outlier_flag"].astype(int).sum()) == int(m.sum())
    A.step("fact_portfolio_snapshot", "active_alpha_ytd outside -100%..+100% YTD",
           "flag alpha_outlier_flag", int(m.sum()),
           f"{int(only_alpha.sum())} of these have a return inside +-100% (not caught by "
           "return_outlier_flag)")


def add_non_trading_day_flag(D: dict, A: Audit, n: int) -> None:
    """#11 Buy/Sell on a non-trading day."""
    banner(n, "non_trading_day_flag on fact_transactions (log #11)")
    tx = D["fact_transactions"]
    trading = D["dim_date"].set_index("date")["is_trading_day"]
    day = tx["txn_date"].map(trading)
    assert day.notna().all(), "txn_date missing from dim_date"
    m = tx["txn_type"].isin(P.TRADE_TXN_TYPES) & (day == "0")
    tx["non_trading_day_flag"] = m.astype(int).astype(str)
    trades = int(tx["txn_type"].isin(P.TRADE_TXN_TYPES).sum())
    show(int(m.sum()), f"{trades:,} Buy/Sell rows, none flagged",
         f"{int(m.sum()):,} flagged ({m.sum() / trades:.1%} of trades)")
    assert int(tx["non_trading_day_flag"].astype(int).sum()) == int(m.sum())
    A.step("fact_transactions", "Buy/Sell dated on a non-trading day", "flag non_trading_day_flag",
           int(m.sum()), f"{m.sum() / trades:.1%} of trades; true trade date not recoverable")


def add_txn_after_close_flag(D: dict, A: Audit, n: int) -> None:
    """#12 inactive client transacting after their last snapshot month."""
    banner(n, "txn_after_close_flag on fact_transactions (log #12)")
    tx, c, sn = D["fact_transactions"], D["dim_client"], D["fact_portfolio_snapshot"]
    last = sn.groupby("client_id")["snapshot_date"].max().str[:7]
    inactive = set(c.loc[c["is_active"] == "False", "client_id"])
    lm = tx["client_id"].map(last)
    m = tx["client_id"].isin(inactive) & lm.notna() & (tx["txn_date"].str[:7] > lm)
    tx["txn_after_close_flag"] = m.astype(int).astype(str)
    show(int(m.sum()), "no flag column",
         f"{int(m.sum()):,} flagged across {tx.loc[m, 'client_id'].nunique()} inactive clients")
    assert tx.loc[m, "client_id"].isin(inactive).all()
    A.step("fact_transactions", "inactive client transacting after last snapshot month",
           "flag txn_after_close_flag", int(m.sum()), "close date or transactions wrong - cannot tell which")


# =============================================================================
# LEFT AS IS (documented with live counts so the log is complete)
# =============================================================================
def document_left_as_is(D: dict, A: Audit) -> dict:
    tx, fe, sn = D["fact_transactions"], D["fact_fees"], D["fact_portfolio_snapshot"]
    sec, dd, c = D["dim_security"], D["dim_date"], D["dim_client"]
    out = {}

    adv = tx[tx["txn_type"] == "Advisory Fee"]
    a = adv.groupby(["client_id", adv["txn_date"].str[:4]])["amount"].apply(lambda s: num(s).sum())
    b = fe.groupby(["client_id", fe["fee_date"].str[:4]])["amount"].apply(lambda s: num(s).sum())
    j = pd.concat([a.rename("debits"), b.rename("fees")], axis=1).dropna()
    off = int(((j["debits"] - j["fees"]).abs() > 0.10 * j["fees"]).sum())
    out["fee_mismatch"] = (off, len(j))

    div = tx["txn_type"] == "Dividend"
    out["dividend_full_value"] = int((div & ((num(tx["shares"]) * num(tx["price"])
                                              - num(tx["amount"])).abs() <= MONEY_TOL)).sum())
    d = pd.to_datetime(dd["date"])
    hol = (d.dt.dayofweek < 5) & d.dt.strftime("%m-%d").isin(["01-01", "07-04", "12-25"]) \
        & (dd["is_trading_day"] == "1")
    out["holidays"] = int(hol.sum())
    out["etf_sector_blank"] = int(((sec["is_etf"] == "True") & (sec["sector"] == "")).sum())
    out["stock_er_blank"] = int(((sec["is_etf"] == "False") & (sec["expense_ratio_bps"] == "")).sum())
    out["cashflow_blank"] = int((~tx["txn_type"].isin(P.SECURITY_TXN_TYPES)
                                 & (tx["security_id"] == "")).sum())
    since = sn["client_id"].map(c.set_index("client_id")["client_since"])
    out["same_month_before_since"] = int(((sn["snapshot_date"] < since)
                                          & (sn["snapshot_date"].str[:7] == since.str[:7])).sum())
    fee_keys = set(zip(fe["client_id"], fe["fee_date"]))
    out["snap_no_fee"] = int(sum(k not in fee_keys for k in zip(sn["client_id"], sn["snapshot_date"])))
    out["zip_leading_zero"] = int(c["zip_code"].str.match(r"0").sum())
    out["sci_alpha"] = int(sn["active_alpha_ytd"].str.contains(r"[eE]").sum())
    used_b = set(sec["benchmark_id"]) | set(sn["benchmark_id"])
    out["unused_benchmarks"] = sorted(set(D["dim_benchmark"]["benchmark_id"]) - used_b, key=int)

    rows = [
        ("fact_transactions", "Advisory Fee debits do not reconcile to fact_fees (client-year, +-10%)",
         off, "fact_fees is the system of record: it reconciles 100% to aum_basis x bps / 10,000 / 12 "
              "and to snapshot AUM"),
        ("fact_transactions", "Dividend amount = shares x price (full position value)",
         out["dividend_full_value"], "consistent source definition; true dividend not recoverable - "
                                     "do not sum as income"),
        ("dim_date", "fixed-date market holidays flagged as trading days", out["holidays"],
         "calendar has no holiday logic; partial fix would be misleading"),
        ("dim_security", "sector blank for ETFs", out["etf_sector_blank"], "legitimate structure"),
        ("dim_security", "expense_ratio_bps blank for stocks", out["stock_er_blank"],
         "legitimate structure - do not fill with 0"),
        ("fact_transactions", "security_id/shares/price blank on cash flows", out["cashflow_blank"],
         "legitimate structure (nullable FK)"),
        ("fact_portfolio_snapshot", "snapshot dated the 1st of the onboarding month",
         out["same_month_before_since"], "month grain - legitimate"),
        ("fact_portfolio_snapshot", "snapshot month with no fee row", out["snap_no_fee"],
         "possible waivers/billing gaps; nothing to fill from"),
        ("dim_client", "same-name clients (e.g. Deborah Miller 1626 / 2060)", 2,
         "different city, ZIP, advisor and history - different people, not merged"),
        ("dim_client", "zip_code with leading zeros", out["zip_leading_zero"], "keep as text"),
        ("fact_portfolio_snapshot", "active_alpha_ytd in scientific notation", out["sci_alpha"],
         "parses correctly - formatting only"),
        ("dim_benchmark", "benchmark never referenced", len(out["unused_benchmarks"]),
         f"benchmark_id {', '.join(out['unused_benchmarks'])} - reference data, keep"),
    ]
    for table, issue, cnt, note in rows:
        A.step(table, issue, "leave as is (documented)", cnt, note)
    return out


# =============================================================================
# FINAL VERIFICATION
# =============================================================================
def final_checks(raw: dict, D: dict, A: Audit, raw_paths: dict, hashes: dict) -> pd.DataFrame:
    print("\n[Final] verification of the clean tables")
    recon = []
    for t in TABLES:
        q = A.quarantined_rows(t)
        recon.append({"table": t, "raw_rows": len(raw[t]), "clean_rows": len(D[t]),
                      "quarantined_rows": q,
                      "balanced": len(raw[t]) == len(D[t]) + q})
    recon = pd.DataFrame(recon)
    print(recon.to_string(index=False))
    assert recon["balanced"].all(), "raw rows != clean rows + quarantined rows"

    for t in TABLES:
        raw_cols = list(raw[t].columns)
        assert list(D[t].columns[:len(raw_cols)]) == raw_cols, f"{t}: columns changed"
        extra = list(D[t].columns[len(raw_cols):])
        assert all(c.endswith("_flag") for c in extra), f"{t}: unexpected added columns {extra}"
        for c in extra:
            assert D[t][c].isin(["0", "1"]).all(), f"{t}.{c} is not 0/1"
        pk = P.SCHEMA[t]["pk"]
        assert D[t][pk].is_unique and nonblank(D[t][pk]).all(), f"{t}: primary key broken"
        # schema types parse on every non-blank value
        for col, spec in P.SCHEMA[t]["columns"].items():
            if spec["type"] in ("int", "float", "date", "bool"):
                bad = nonblank(D[t][col]) & ~P.parses(D[t][col], spec["type"], spec)
                assert not bad.any(), f"{t}.{col}: {int(bad.sum())} values do not parse as {spec['type']}"

    print("\n  referential integrity (exact text match):")
    for child, ccol, parent, pcol, nullable, _k in P.FOREIGN_KEYS:
        s = D[child][ccol]
        nb = nonblank(s)
        rate = s[nb].isin(set(D[parent][pcol])).mean()
        print(f"    {child}.{ccol:<14} -> {parent}.{pcol:<13} {rate:8.2%}   blanks {int((~nb).sum()):,}")
        assert rate == 1.0, f"{child}.{ccol} has orphans in the clean data"
        assert nullable or nb.all(), f"{child}.{ccol} has blanks but is not nullable"

    for t, path in raw_paths.items():
        assert sha256(path) == hashes[t], f"RAW FILE MODIFIED: {path}"
    print("\n  raw files unchanged (SHA-256 identical)")
    return recon


# =============================================================================
# README_clean.md
# =============================================================================
BI_TYPE = {"int": "Whole Number", "float": "Decimal Number", "date": "Date",
           "bool": "True/False", "str": "Text"}
FLAG_DOC = {
    "risk_profile_imputed_flag": ("dim_client", "1 = risk_profile was blank and filled from the "
                                  "benchmark the firm assigned (log #7)"),
    "aum_floor_flag": ("fact_portfolio_snapshot, fact_fees", "1 = AUM / aum_basis exactly $1,000, "
                       "a system floor - exclude from AUM and return averages (log #9)"),
    "return_outlier_flag": ("fact_portfolio_snapshot", "1 = return_ytd outside -100%..+100% YTD "
                            "(log #10)"),
    "alpha_outlier_flag": ("fact_portfolio_snapshot", "1 = active_alpha_ytd outside -100%..+100% "
                           "YTD, including rows whose return is in range (log #10)"),
    "non_trading_day_flag": ("fact_transactions", "1 = Buy/Sell dated on a weekend / non-trading "
                             "day (log #11)"),
    "txn_after_close_flag": ("fact_transactions", "1 = inactive client transacting after its last "
                             "snapshot month (log #12)"),
}


def write_readme(D: dict, A: Audit, recon: pd.DataFrame, info: dict) -> None:
    L = ["# Wildcat Capital - clean data (HW4)", "",
         "Generated by `scripts/02_clean.py`. Every number below is computed by the script on the "
         "current run. Raw files are untouched; every change is in `data/audit/`.", "",
         "## Files and row counts", "",
         "| Table | Raw rows | Clean rows | Quarantined |", "|---|---:|---:|---:|"]
    for _, r in recon.iterrows():
        L.append(f"| `{r.table}.csv` | {r.raw_rows:,} | {r.clean_rows:,} | {r.quarantined_rows:,} |")
    L += ["", "## Relationships (star schema)", "",
          "All relationships are many-to-one (fact/child -> dimension), single direction.", "",
          "| From (many) | To (one) | Notes |", "|---|---|---|"]
    for child, ccol, parent, pcol, nullable, kind in P.FOREIGN_KEYS:
        note = "nullable: blank on Deposit/Withdrawal/Advisory Fee rows" if nullable else ""
        if kind == "date":
            note = "date key - mark `dim_date` as the date table"
        L.append(f"| `{child}.{ccol}` | `{parent}.{pcol}` | {note} |")
    L += ["", "`fact_portfolio_snapshot` and `fact_fees` are monthly (dated the 1st); "
          "`fact_transactions` is daily.", "",
          "## Column types to set in the BI tool", "",
          "Read every CSV as text first, then set these types. **Do not let the tool auto-detect**: "
          "it drops ZIP-code leading zeros and turns the account type `529` into a number.", ""]
    for t in TABLES:
        cols = P.SCHEMA[t]["columns"]
        parts = []
        for c in D[t].columns:
            if c in cols:
                typ = BI_TYPE[cols[c]["type"]]
                if c == "zip_code":
                    typ = "Text (keeps leading zeros)"
                elif c in ("account_type", "primary_account_type"):
                    typ = "Text ('529' is a plan name, not a number)"
                if cols[c]["type"] == "bool" and cols[c].get("true") == ["1"]:
                    typ = "Whole Number 0/1 (or True/False)"
                if c == P.SCHEMA[t]["pk"]:
                    typ += ", primary key"
            else:
                typ = "Whole Number 0/1 (flag)"
            parts.append(f"`{c}` {typ}")
        L.append(f"- **{t}**: " + "; ".join(parts))
    L += ["", "Percent-type columns (`return_ytd`, `benchmark_return_ytd`, `active_alpha_ytd`, "
          "`cash_pct`) are decimals (0.05 = 5%) - format as Percentage, do not divide again.", "",
          "## Flag columns added", "", "| Column | Table(s) | Rows = 1 | Meaning |", "|---|---|---:|---|"]
    for col, (tables, meaning) in FLAG_DOC.items():
        cnt = sum(int(D[t][col].astype(int).sum()) for t in TABLES if col in D[t].columns)
        L.append(f"| `{col}` | {tables} | {cnt:,} | {meaning} |")
    L += ["", "## Quarantined rows", "", "| File | Table | Rows | Reason |", "|---|---|---:|---|"]
    for name, (t, q) in A.quarantine.items():
        L.append(f"| `data/audit/{name}.csv` | {t} | {len(q):,} | {q['quarantine_reason'].iloc[0]} |")
    off, tot = info["fee_mismatch"]
    L += ["", "## Known limitations (chosen not to fix)", "",
          f"1. **Fee revenue: two sources disagree.** {off:,} of {tot:,} client-years of Advisory Fee "
          "debits in `fact_transactions` differ from `fact_fees` by >10%. *Reason:* `fact_fees` "
          "reconciles 100% to `aum_basis x bps / 10,000 / 12` and to snapshot AUM, so it is the "
          "system of record; forcing the debits to match would invent data.",
          f"2. **Dividend `amount` = full position value** ({info['dividend_full_value']:,} rows). "
          "*Reason:* consistent source definition; the real dividend cannot be recovered. Do not "
          "sum it as income.",
          f"3. **No market holidays in `dim_date`** ({info['holidays']} weekday Jan 1 / Jul 4 / "
          "Dec 25 dates are trading days). *Reason:* a partial holiday fix would be misleading; "
          "`non_trading_day_flag` therefore only catches weekends.",
          "4. **Floor AUM and extreme returns are flagged, not changed.** *Reason:* the true "
          "values cannot be recovered; filter on `aum_floor_flag` / `return_outlier_flag` / "
          "`alpha_outlier_flag`.",
          "5. **Trades on weekends and post-close transactions are flagged, not moved or "
          "deleted.** *Reason:* the true dates cannot be recovered and deleting would distort volumes.",
          "6. **Imputed risk profiles** are best-evidence, not confirmed by the client. *Reason:* "
          "filled only where the assigned benchmark maps to exactly one profile; "
          "`risk_profile_imputed_flag` marks them.",
          f"7. **Snapshot months without a fee row** ({info['snap_no_fee']:,}). *Reason:* "
          "waivers or billing gaps are plausible and there is nothing to fill from.",
          "8. **Legitimate blanks kept**: ETF `sector` "
          f"({info['etf_sector_blank']}), stock `expense_ratio_bps` ({info['stock_er_blank']}), "
          f"cash-flow `security_id`/`shares`/`price` ({info['cashflow_blank']:,}). *Reason:* "
          "the value does not exist for those rows; filling would invent data.",
          f"9. **Unused dimension members** (benchmark_id {', '.join(info['unused_benchmarks'])}, "
          "dates with no activity). *Reason:* reference data, normal in a star schema.",
          "10. **Same-name clients are not merged** (e.g. Deborah Miller, 1626 / 2060). "
          "*Reason:* different city, ZIP, advisor and history - two different people."]
    (CLEAN_DIR / "README_clean.md").write_text("\n".join(L) + "\n", encoding="utf-8")


# =============================================================================
# DRIVER
# =============================================================================
def find_data_dir(arg: str | None) -> Path:
    if arg:
        d = Path(arg)
        if not d.is_dir():
            sys.exit(f"--data-dir not found: {d}")
    else:
        d = next((x for x in P.DEFAULT_DATA_DIRS
                  if x.is_dir() and all((x / f"{t}.csv").exists() for t in TABLES)), None)
        if d is None:
            sys.exit("\nCould not find all 8 Wildcat Capital CSV files. Looked in:\n    "
                     + "\n    ".join(str(x) for x in P.DEFAULT_DATA_DIRS)
                     + '\n\nPoint the script at them, e.g.\n'
                     '    python scripts/02_clean.py --data-dir "C:\\path\\to\\Wildcat Capital"\n')
    missing = [t for t in TABLES if not (d / f"{t}.csv").exists()]
    if missing:
        sys.exit(f"Missing raw files in {d}: {', '.join(missing)}")
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description="Clean the Wildcat Capital tables.")
    ap.add_argument("--data-dir", help="folder with the 8 raw CSVs (default: auto-detect)")
    args = ap.parse_args()

    data_dir = find_data_dir(args.data_dir)
    raw_paths = {t: data_dir / f"{t}.csv" for t in TABLES}
    assert CLEAN_DIR.resolve() != data_dir.resolve(), "clean folder must differ from raw folder"
    hashes = {t: sha256(p) for t, p in raw_paths.items()}
    raw = {t: P.read_table(p) for t, p in raw_paths.items()}
    D = {t: df.copy() for t, df in raw.items()}
    print(f"Raw data: {data_dir}")
    for t in TABLES:
        print(f"  {t:<26} {len(raw[t]):>8,} rows")

    A = Audit()
    steps = iter(range(1, 100))
    # 1) formats first, so later joins and reconciliations run on clean values
    fix_security_id_format(D, A, next(steps))
    fix_return_percent_strings(D, A, next(steps))
    fix_state_names(D, A, next(steps))
    fix_certification_placeholder(D, A, next(steps))
    # 2) quarantine (duplicates before orphans, so we can prove we created no orphans)
    dup_ids = quarantine_duplicate_clients(D, A, next(steps))
    quarantine_orphan_transactions(D, A, next(steps), dup_ids)
    # 3) value corrections, reconciled
    fix_negative_buy_shares(D, A, next(steps))
    # 4) fill (after duplicates are gone)
    fill_risk_profile(D, A, next(steps))
    # 5) flags last - they depend on clean keys and joins
    add_aum_floor_flags(D, A, next(steps))
    add_return_outlier_flag(D, A, next(steps))
    add_alpha_outlier_flag(D, A, next(steps))
    add_non_trading_day_flag(D, A, next(steps))
    add_txn_after_close_flag(D, A, next(steps))
    # 6) documented "leave as is" items
    info = document_left_as_is(D, A)

    recon = final_checks(raw, D, A, raw_paths, hashes)

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    for t in TABLES:
        D[t].to_csv(CLEAN_DIR / f"{t}.csv", index=False, encoding="utf-8")
    pd.DataFrame(A.log).to_csv(AUDIT_DIR / "cleaning_log.csv", index=False, encoding="utf-8")
    changes = (pd.concat(A.changes, ignore_index=True) if A.changes else
               pd.DataFrame(columns=["table", "key_column", "key", "column", "old_value", "new_value"]))
    changes.to_csv(AUDIT_DIR / "value_changes.csv", index=False, encoding="utf-8")
    for name, (_t, q) in A.quarantine.items():
        q.to_csv(AUDIT_DIR / f"{name}.csv", index=False, encoding="utf-8")
    write_readme(D, A, recon, info)

    # read back what was written: the files on disk must match what we verified
    for t in TABLES:
        back = P.read_table(CLEAN_DIR / f"{t}.csv")
        assert back.shape == D[t].shape and back.equals(D[t].reset_index(drop=True)), \
            f"{t}: file on disk differs from the verified table"

    print(f"\nSaved {len(TABLES)} clean tables -> {CLEAN_DIR}")
    print(f"Saved audit trail -> {AUDIT_DIR}  ({len(A.log)} log rows, {len(changes):,} value changes, "
          f"{len(A.quarantine)} quarantine files)")
    print(f"Saved {CLEAN_DIR / 'README_clean.md'}")
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
