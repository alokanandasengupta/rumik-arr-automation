#!/usr/bin/env python3
"""
Re-does the Aug 1 - Sep 5 Razorpay reconciliation with the CORRECTED AOV formula
(customer_id-restricted to the active population, not company-wide razorpay_payments).

Active Subscribers and New/Churned counts are unchanged from the original August reconciliation
(state_history reconstruction, unaffected by the AOV formula change) -- only AOV, and everything
downstream of it (MRR, ARR, USD, New MRR Added, MRR Churned, Net MRR Change), gets recomputed.

Cashfree/Paytm rows are untouched, same as before.

Run once: python3 reconcile_september_v2.py [--dry-run]
"""
import bisect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync_arr as s

DRY_RUN = "--dry-run" in sys.argv

active_by_day = {r[0][:10]: r[1] for r in json.load(open("/tmp/active_by_day_v2.json"))}
new_churned_by_day = {r[0][:10]: (r[1], r[2]) for r in json.load(open("/tmp/new_churned_by_day_v2.json"))}

# genuine cumulative AOV series (customer_id-restricted, all-time) -- as-of-day-D lookups via bisect
aov_series = json.load(open("/tmp/cumulative_aov_series.json"))
aov_timestamps = [s.parse_ts(r[0]) for r in aov_series]
aov_values = [float(r[1]) for r in aov_series]


def aov_as_of_end_of_day(date_str):
    """Cumulative AOV as of 23:59:59 on this date (last known value at/before that instant)."""
    from datetime import datetime, timedelta
    cutoff = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    idx = bisect.bisect_right(aov_timestamps, cutoff) - 1
    return aov_values[idx] if idx >= 0 else 0.0


JUL31_ACTIVE = active_by_day["2026-07-31"]
JUL31_AOV = aov_as_of_end_of_day("2026-07-31")

DATES = sorted(d for d in active_by_day if d >= "2026-08-01")

prev_mrr, prev_active, prev_aov = JUL31_AOV * JUL31_ACTIVE, JUL31_ACTIVE, JUL31_AOV

rows_by_date = {}
for d in DATES:
    active = active_by_day[d]
    aov = aov_as_of_end_of_day(d)
    mrr, arr, mrr_usd, arr_usd = s.mrr_row_values(active, aov)
    new_n, churned_n = new_churned_by_day[d]
    new_mrr_added = round(new_n * aov, 2)
    mrr_churned = round(churned_n * prev_aov, 2)
    net_mrr_change = round(mrr - prev_mrr, 2)
    net_sub_change = active - prev_active

    rows_by_date[d] = [mrr, new_mrr_added, mrr_churned, net_mrr_change, active,
                        net_sub_change, round(aov, 2), mrr, arr, mrr_usd, arr_usd]

    prev_mrr, prev_active, prev_aov = mrr, active, aov

print(f"Built {len(rows_by_date)} corrected Razorpay rows ({DATES[0]} to {DATES[-1]})")
print(f"July 31 baseline AOV (for Aug 1's day-over-day calc): {JUL31_AOV:.2f}")

if DRY_RUN:
    for d in DATES[:3] + DATES[-3:]:
        print(d, rows_by_date[d])
    sys.exit(0)

ss = s.sheets_client()
ws = ss.worksheet("Sheet 1")
all_values = ws.get_all_values()
headers = all_values[0]
date_idx, gw_idx = headers.index("Date"), headers.index("Payment Gateway")

target_rows = {}
for i, row in enumerate(all_values[1:], start=2):
    if len(row) > gw_idx and row[gw_idx] == "Razorpay" and row[date_idx] in rows_by_date:
        target_rows[row[date_idx]] = i

missing = set(rows_by_date) - set(target_rows)
if missing:
    print(f"WARNING: no existing Razorpay row found for {len(missing)} dates (skipped): {sorted(missing)}")

print(f"Matched {len(target_rows)} existing Razorpay rows to update")

data = [{"range": f"C{row_num}:M{row_num}", "values": [rows_by_date[d]]} for d, row_num in target_rows.items()]

CHUNK = 40
for i in range(0, len(data), CHUNK):
    chunk = data[i:i + CHUNK]
    ws.batch_update(chunk, value_input_option="RAW")
    print(f"  wrote rows {i+1}-{i+len(chunk)} of {len(data)}")

print("Done.")
