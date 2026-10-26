# Wildcat Capital — Data Cleaning Report
**To:** Portfolio Committee  **Re:** Quality of the data warehouse extract (8 tables, 2020–2024)

## Executive summary
We audited all eight tables in the warehouse extract (555,401 rows) before they feed the committee's reporting model. The data was **usable but not reliable as delivered**: one key could not be joined at all, 200 transactions belonged to clients who do not exist, and 25 clients were recorded twice.

After cleaning:
- **Every one of the 13 table relationships now matches 100%** (the worst was 0% before).
- **Every recomputable amount reconciles**: fees to AUM × rate, trades to shares × price, and alpha to return − benchmark.
- **Only 225 rows (0.04%) were removed**, and none were deleted: they sit in quarantine files with a reason for each.

Issues that could not be fixed without inventing data are **flagged, not changed**. The committee should filter on these flags before averaging AUM or returns, and should treat `fact_fees`, not transaction fee debits, as the source of fee revenue.

## Cleaning summary
| Issue | Table | Rows | Action | How we verified it |
|---|---|---:|---|---|
| Security IDs stored as "45.0" | Transactions | 197,175 | Converted to "45" | Join to securities: 0% → 100% |
| Client records duplicated with altered surnames | Clients | 25 | Quarantined | Identical in every other column; no activity attached |
| Transactions for non-existent clients | Transactions | 200 | Quarantined | No matching client ID; $10.9M of activity set aside |
| Buys recorded with negative shares | Transactions | 835 | Sign corrected | \|shares\| × price = amount on every row |
| Returns stored as text ("-0.64%") | Snapshots | 300 | Recomputed as benchmark + alpha | Agrees with the original text within 0.00005 |
| State names instead of codes | Clients | 78 | Standardized | All map to valid USPS codes |
| Missing risk profile | Clients | 75 | Filled "Balanced" + flag | Among profiled clients, benchmark 3 = Balanced only |
| "None" text in certifications | Advisors | 6 | Relabelled "No Certification" | No placeholder text remains |
| AUM of exactly $1,000 (system floor) | Snapshots / Fees | 420 / 386 | Flagged | Every flagged fee matches a flagged snapshot |
| YTD return beyond ±100% | Snapshots | 977 | Flagged | Return identity still holds; values not recoverable |
| Trades dated on weekends | Transactions | 40,630 | Flagged | Matched against the trading calendar |
| Inactive clients still transacting | Transactions | 2,410 | Flagged | Dated after the client's last snapshot month |

**Before vs after (from the data quality scorecard):**

| Table | Measure | Raw | Clean |
|---|---|---:|---:|
| Transactions | Referential match | 81.95% | 100% |
| Transactions | Type validity | 90.54% | 100% |
| Transactions | Rule pass | 99.93% | 100% |
| Clients | Completeness | 99.785% | 100% |
| Clients | Rule pass | 99.61% | 100% |
| Advisors | Rule pass | 94.00% | 100% |

## Key judgment calls
**1. Duplicate clients were quarantined, not merged.**
- *Justification:* Each of the 25 records matched an existing client in every field except a truncated or altered surname ("Taylo" for "Taylor"), and none had any fees, snapshots or transactions. Keeping them would overstate client counts and AUM by tier and state.
- *Risk:* If any are genuinely separate people, client counts are understated by up to 25. We judge this unlikely given identical age, ZIP code, onboarding date and initial AUM to the cent.

**2. Missing risk profiles were filled from the assigned benchmark.**
- *Justification:* All 75 clients with a blank profile were benchmarked to the 60/40 Blend, which, among clients with a recorded profile, the firm assigns only to Balanced clients (38,960 snapshot rows, no exceptions). Every filled value carries `risk_profile_imputed_flag = 1`.
- *Risk:* If that benchmark is also a default for clients never assessed, up to 75 clients are mislabelled. The flag lets the committee exclude them.

**3. `fact_fees` is the system of record for fee revenue.**
- *Justification:* Fee debits in the transaction table differ from `fact_fees` by more than 10% in 9,350 of 10,396 client-years. `fact_fees`, by contrast, reconciles 100% both to AUM × billing rate and to the monthly portfolio snapshots, so it is the trustworthy source.
- *Risk:* Anyone reporting fee revenue from transactions will get a materially different number.

**4. Implausible values were flagged rather than corrected.**
- *Justification:* 420 snapshots show AUM of exactly $1,000 and 977 show YTD returns beyond ±100%, but the true values cannot be recovered. Changing them would invent data; deleting them would erase history.
- *Risk:* Averages of AUM or return computed **without** filtering on `aum_floor_flag` and `return_outlier_flag` will be badly distorted.

## Limitations
- **Dividend amounts equal full position value** (53,826 rows), not a per-share payout. Do not sum them as income.
- **The calendar has no market holidays** (9 New Year's Day, July 4 and Christmas dates are marked as trading days), so the weekend-trade flag misses trades on holidays.
- **7,350 client-months have a snapshot but no fee.** These may be waivers or billing gaps; there was nothing to fill them from.
- **Statistical outliers are not errors.** 99% of the high-AUM outliers belong to Ultra-High-Net-Worth clients whose balances are real.
- **Legitimate blanks were kept.** ETFs have no sector, stocks have no expense ratio, and cash movements have no security.

---
*AI-assisted report — reviewed for factual accuracy on [10/26/2026] by [Lauren Curley]*
