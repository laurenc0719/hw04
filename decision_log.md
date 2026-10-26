# Decision Log — HW4 Wildcat Capital

Evidence comes from `reports/dq_summary_raw.csv` (output of `scripts/01_profile.py`) plus the follow-up checks noted in each entry. Entries 1–14 are defects. Entries 15–24 are legitimate structure or known limitations I am deliberately leaving alone.

---

## Defects

### 1. Duplicate clients with altered surnames
| Field | Entry |
|---|---|
| **Table / column** | `dim_client` (rows `client_id` 2501–2525) |
| **Evidence** | Near-duplicate check flagged 25 pairs. Every one of the 25 rows matches an existing client in **every column except `last_name`**. 23 surnames are truncated (Taylor → "Taylo", Martin → "Marti"); 2 have an extra letter (Lee → "Leen", Pace → "Pacen"). 0 rows in any fact table reference these 25 IDs. |
| **Dimension violated** | Uniqueness, Accuracy |
| **Decision** | **Quarantine** the 25 rows to `quarantine_dim_client_duplicates.csv`, recording the original `client_id` each one duplicates |
| **Justification** | One real person counted twice inflates client counts and AUM by tier/state. Removing them breaks no joins because no fact rows point to them. |
| **Risk** | If any are real, separate clients, we undercount by up to 25 clients. Unlikely: identical age, ZIP, city, initial AUM to the cent, and onboarding date. |

### 2. Integer keys stored as decimals
| Field | Entry |
|---|---|
| **Table / column** | `fact_transactions.security_id` |
| **Evidence** | All 197,175 non-blank values are written like `"45.0"`. A text join to `dim_security` matches 0%; after converting to integer it matches 100%. |
| **Dimension violated** | Validity, Referential Integrity |
| **Decision** | **Convert** to integer text (`"45.0"` → `"45"`) |
| **Justification** | The ID is right but the format is wrong. Without the fix, every trade drops out of a security join and holdings by asset class can't be built. |
| **Risk** | Low: the 100% post-conversion match rate verifies it. If a value had a real fractional part, an `assert` stops the script. |

### 3. Transactions for clients that don't exist
| Field | Entry |
|---|---|
| **Table / column** | `fact_transactions.client_id` |
| **Evidence** | 200 rows (200 distinct IDs, range 2702–3192) have no match in `dim_client`, even after key normalisation. Mix: 58 Buy, 38 Dividend, 35 Sell, 25 Deposit, 24 Advisory Fee, 20 Withdrawal. Total amount $10.9M. |
| **Dimension violated** | Referential Integrity |
| **Decision** | **Quarantine** to `quarantine_fact_transactions_orphans.csv` |
| **Justification** | Nothing tells us who these clients are, so we can't attach them without inventing data. Kept in the model, they show up under "(Blank)" and distort per-client metrics. |
| **Risk** | $10.9M of activity leaves the clean fact. If the client records turn up later, these rows must be restored from quarantine. |

### 4. Buys with negative shares
| Field | Entry |
|---|---|
| **Table / column** | `fact_transactions.shares` |
| **Evidence** | 836 Buy rows have shares < 0 (0 Sells do). For all 836, `abs(shares) × price` equals `amount` within $0.01. |
| **Dimension violated** | Validity, Consistency |
| **Decision** | **Convert** shares to the absolute value; log every change in `value_changes.csv` |
| **Justification** | The direction comes from `txn_type`, and `amount` (always positive) reconciles once the sign is flipped. That proves the sign is the error and the amount is right. |
| **Risk** | If the negative sign was meant to mean "reversal/cancel", we'd count 836 cancelled buys as real. No reversal txn_type exists, so that's unlikely. |

### 5. Returns stored as percent strings
| Field | Entry |
|---|---|
| **Table / column** | `fact_portfolio_snapshot.return_ytd` |
| **Evidence** | 300 values like `"-0.64%"` fail to parse as numbers; all the others are decimals (`0.019504`). After stripping `%` and dividing by 100, `active_alpha = return − benchmark` holds within 0.00005 for all 300. |
| **Dimension violated** | Validity, Consistency |
| **Decision** | **Convert** (strip `%`, ÷100) |
| **Justification** | Same quantity, different format. The alpha identity verifies the conversion, so no guessing. |
| **Risk** | Percent strings carry only 2 decimals, so these 300 values are less precise (±0.00005). That's immaterial for reporting. |

### 6. State names instead of codes
| Field | Entry |
|---|---|
| **Table / column** | `dim_client.state` |
| **Evidence** | 78 rows use full names: Pennsylvania 25, Florida 15, New York 14, New Jersey 13, California 5, Illinois 4, Texas 2. The other 2,447 use USPS codes. |
| **Dimension violated** | Consistency, Validity |
| **Decision** | **Standardize** to two-letter USPS codes |
| **Justification** | "PA" and "Pennsylvania" split into two groups, so state-level AUM is understated. The mapping is one-to-one and unambiguous. |
| **Risk** | Minimal. A misspelled state name wouldn't map; an `assert` confirms all 78 map to valid codes. |

### 7. Blank risk profile
| Field | Entry |
|---|---|
| **Table / column** | `dim_client.risk_profile` |
| **Evidence** | 76 blanks. One is a duplicate clone (#1). The other 75 all have snapshots, and every one of them uses `benchmark_id = 3`. Across the data, benchmark 3 is used **only** by Balanced clients (38,960 rows; 0 exceptions), and risk_profile → benchmark is consistent for every profile. |
| **Dimension violated** | Completeness |
| **Decision** | **Fill** with "Balanced" and add flag `risk_profile_imputed = 1` |
| **Justification** | The benchmark the firm actually assigned reveals the profile. That's reconciling against another table, not guessing, and the flag keeps it transparent. |
| **Risk** | If benchmark 3 is also a default for "not yet assessed", we've mislabelled up to 75 clients. The flag lets anyone exclude them. |

### 8. Text placeholder instead of a value
| Field | Entry |
|---|---|
| **Table / column** | `dim_advisor.certifications` |
| **Evidence** | 6 advisors have the literal string `"None"`. Default `pd.read_csv` would turn it into NaN. |
| **Dimension violated** | Validity, Consistency |
| **Decision** | **Standardize** to `"No Certification"` |
| **Justification** | It means "has no certification", which is a real answer, not missing data. An explicit label keeps that meaning and survives any tool's null handling. |
| **Risk** | If "None" actually meant "unknown", we're overstating certainty for 6 advisors. |

### 9. AUM floor value of exactly $1,000
| Field | Entry |
|---|---|
| **Table / column** | `fact_portfolio_snapshot.aum`; `fact_fees.aum_basis`, `fact_fees.amount` |
| **Evidence** | `aum = 1000.0` appears 420 times across 92 clients (a continuous measure shouldn't repeat like that). The same rows drive 386 `aum_basis = 1000.0` fee rows with fees of about $0.75. 68 of the snapshot rows show `return_ytd = 0.0`. |
| **Dimension violated** | Accuracy/Plausibility |
| **Decision** | **Flag** `aum_floor_flag = 1` (snapshots and fees) |
| **Justification** | Looks like a system floor/placeholder rather than a real balance, but the true value can't be recovered. Flagging lets analysts exclude these rows from AUM and return averages. |
| **Risk** | If some accounts really were drawn down to $1,000, the flag over-warns. No data is changed either way. |

### 10. Implausible YTD returns
| Field | Entry |
|---|---|
| **Table / column** | `fact_portfolio_snapshot.return_ytd`, `active_alpha_ytd` |
| **Evidence** | 976 rows (144 clients) have `return_ytd > 1` (more than +100% YTD, max 94.67 = +9,467%). The alpha identity still holds on these rows. 52% of these clients also hit the $1,000 floor (#9), so the return is likely computed off a tiny base. |
| **Dimension violated** | Accuracy/Plausibility |
| **Decision** | **Flag** `return_outlier_flag = 1` |
| **Justification** | The rows are internally consistent, so I can't say which input is wrong. Flagging stops them from skewing average return without deleting history. |
| **Risk** | Averages computed without the flag will be heavily skewed. If any are real, flagging alone costs nothing. |

### 11. Trades on non-trading days
| Field | Entry |
|---|---|
| **Table / column** | `fact_transactions.txn_date` × `dim_date.is_trading_day` |
| **Evidence** | 40,649 of 143,311 Buy/Sell rows (28%) fall on weekends (`is_trading_day = 0`). |
| **Dimension violated** | Accuracy/Plausibility, Consistency |
| **Decision** | **Flag** `non_trading_day_flag = 1` |
| **Justification** | Markets are closed, so the date is probably a booking date, not the trade date. The true trade date can't be recovered. |
| **Risk** | Moving or deleting 28% of trades would distort volume. Flagging keeps the totals intact. |

### 12. Inactive clients still transacting
| Field | Entry |
|---|---|
| **Table / column** | `fact_transactions` × `dim_client.is_active` × `fact_portfolio_snapshot` |
| **Evidence** | 2,410 transactions belong to `is_active = False` clients and are dated after that client's last snapshot month (the account looks closed in the snapshot fact). |
| **Dimension violated** | Consistency (cross-table) |
| **Decision** | **Flag** `txn_after_close_flag = 1` |
| **Justification** | Either the close date or the transactions are wrong, and the data can't say which. Flagging surfaces it for operations. |
| **Risk** | If they're legitimate wind-down transfers, the flag is a false alarm. Harmless. |

### 13. Advisory fee debits don't reconcile to the fee fact
| Field | Entry |
|---|---|
| **Table / column** | `fact_transactions` (txn_type = Advisory Fee) vs `fact_fees.amount` |
| **Evidence** | At client-year level, 9,350 of 10,396 client-years differ by more than 10% (ratio median 0.80, range 0.05–316). By contrast, `fact_fees` reconciles 100% to `aum_basis × bps / 10,000 / 12` and 100% to snapshot AUM. |
| **Dimension violated** | Consistency (cross-table) |
| **Decision** | **Leave as is**; document `fact_fees` as the system of record for fee revenue |
| **Justification** | `fact_fees` is verified by two independent reconciliations; the debits are not. Forcing the debits to match would invent data. |
| **Risk** | Anyone summing fee revenue from transactions gets a different number. Covered in the README limitations. |

### 14. Market holidays marked as trading days
| Field | Entry |
|---|---|
| **Table / column** | `dim_date.is_trading_day` |
| **Evidence** | 9 weekday Jan 1 / Jul 4 / Dec 25 dates are flagged `1`. 0 weekday dates are flagged `0`, so the calendar has no holiday logic at all. |
| **Dimension violated** | Accuracy |
| **Decision** | **Leave as is**; document as a known limitation |
| **Justification** | Fixing only 3 fixed holidays would give a half-correct calendar (Thanksgiving, Good Friday, etc. would still be wrong). A proper exchange calendar is out of scope. |
| **Risk** | Trading-day counts are overstated by roughly 9–10 days per year, and #11 under-flags trades placed on holidays. |

---

## Legitimate structure / leave as is

### 15. Dividend amount equals full position value
| Field | Entry |
|---|---|
| **Table / column** | `fact_transactions.amount` (Dividend) |
| **Evidence** | All 53,864 Dividend rows have `amount = shares × price`, i.e. a 100% "yield". |
| **Dimension violated** | Accuracy/Plausibility (suspected) |
| **Decision** | **Leave as is**; document |
| **Justification** | It's consistent across every row, so it's how the source defines the field, not a random defect. The true dividend can't be recovered. |
| **Risk** | Summing dividends as income overstates income massively. The README warns against it. |

### 16. Blank sector for ETFs
| Field | Entry |
|---|---|
| **Table / column** | `dim_security.sector` |
| **Evidence** | 219 blanks = exactly the 219 ETFs; 0 non-ETFs are blank. |
| **Dimension violated** | None (structure) |
| **Decision** | **Leave as is** |
| **Justification** | Funds span sectors, so blank is correct. Filling it would invent data. |
| **Risk** | Shows as "(Blank)" in visuals; relabel it in the BI layer if needed. |

### 17. Blank expense ratio for stocks
| Field | Entry |
|---|---|
| **Table / column** | `dim_security.expense_ratio_bps` |
| **Evidence** | 281 blanks = exactly the 281 non-ETFs; all 219 ETFs have one (3–95 bps). |
| **Dimension violated** | None (structure) |
| **Decision** | **Leave as is** (do not fill with 0) |
| **Justification** | Stocks don't have expense ratios. A 0 would wrongly drag down average fund cost. |
| **Risk** | None, as long as averages are taken over ETFs only. |

### 18. Blank security/shares/price on cash flows
| Field | Entry |
|---|---|
| **Table / column** | `fact_transactions.security_id`, `shares`, `price` |
| **Evidence** | 101,597 blanks = exactly the Deposit + Withdrawal + Advisory Fee rows. 0 Buy/Sell/Dividend rows are missing them. |
| **Dimension violated** | None (structure) |
| **Decision** | **Leave as is** |
| **Justification** | Cash movements don't involve a security. This is a nullable FK by design. |
| **Risk** | None. The RI checks treat these blanks as allowed. |

### 19. Snapshot dated before the client's start date
| Field | Entry |
|---|---|
| **Table / column** | `fact_portfolio_snapshot.snapshot_date` vs `dim_client.client_since` |
| **Evidence** | 930 rows are dated before `client_since`, but all are in the **same month** (snapshots are dated the 1st). 0 are in an earlier month. |
| **Dimension violated** | None (grain) |
| **Decision** | **Leave as is** |
| **Justification** | This is a month-grain fact: the snapshot represents the onboarding month. |
| **Risk** | None. |

### 20. Snapshot months with no fee
| Field | Entry |
|---|---|
| **Table / column** | `fact_portfolio_snapshot` vs `fact_fees` |
| **Evidence** | 7,350 client-months have a snapshot but no fee row. The reverse is 0: every fee has a matching snapshot with equal AUM. |
| **Dimension violated** | Completeness (possible) |
| **Decision** | **Leave as is**; document |
| **Justification** | Fee waivers or billing gaps are plausible business events, and there's nothing to fill from. |
| **Risk** | Fee yield per client-month is understated if these were genuinely missed bills. |

### 21. Same name, different clients
| Field | Entry |
|---|---|
| **Table / column** | `dim_client` (1626 and 2060, "Deborah Miller") |
| **Evidence** | Flagged by the near-duplicate check, but they have a different city, state, ZIP, onboarding date and advisor. Each has its own 60 snapshots. |
| **Dimension violated** | None |
| **Decision** | **Leave as is** |
| **Justification** | Two different people who share a common name. Merging them would destroy real history. |
| **Risk** | None. |

### 22. Codes that look numeric but are text
| Field | Entry |
|---|---|
| **Table / column** | `dim_client.zip_code`; `primary_account_type` / `account_type` = "529" |
| **Evidence** | 232 ZIP codes have leading zeros (e.g. "03897"). "529" is a plan name, not a number. |
| **Dimension violated** | None (if kept as text) |
| **Decision** | **Leave as is**; set as **Text** type in the BI tool (documented in README_clean) |
| **Justification** | Numeric conversion drops ZIP leading zeros and turns a category into a number. |
| **Risk** | A BI tool that auto-detects types will break these; documented. |

### 23. Repeated security names
| Field | Entry |
|---|---|
| **Table / column** | `dim_security.name` |
| **Evidence** | 3 names appear twice ("Jones Inc Fund", "Davis PLC Fund", "Graham Group Inc."), each with different tickers and asset classes. |
| **Dimension violated** | None |
| **Decision** | **Leave as is** |
| **Justification** | Distinct products from the same issuer. The ticker is the natural key, and it's unique. |
| **Risk** | Visuals grouped by name would merge them; use ticker. |

### 24. Minor formatting and unused dimension members
| Field | Entry |
|---|---|
| **Table / column** | `fact_portfolio_snapshot.active_alpha_ytd`; `dim_benchmark`; `dim_date` |
| **Evidence** | 756 alpha values are in scientific notation (`"2e-05"`) but parse correctly. `benchmark_id` 6 (MSCI Emerging Markets) is never used by any security or snapshot. 1 date in `dim_date` has no facts. |
| **Dimension violated** | None (INFO only) |
| **Decision** | **Leave as is** |
| **Justification** | The numbers are already correct once typed. Unused dimension members are normal in a star schema (the calendar and benchmark lists are reference data). |
| **Risk** | None. |

---

## Planned cleaning order (for 02_clean.py)
1. **Format fixes first** (#2 key format, #5 percent strings, #6 states, #8 placeholder), so the later joins and reconciliations run on clean types.
2. **Quarantine** (#1 duplicate clients, #3 orphan transactions), then prove `raw rows = clean rows + quarantined rows` for each table.
3. **Value corrections** (#4 sign flip), verified against `amount`.
4. **Fill** (#7), verified against the snapshot benchmark, *after* #1 so clones aren't filled.
5. **Flags** (#9–#12) last, because they depend on the cleaned keys and joins.
