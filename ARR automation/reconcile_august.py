#!/usr/bin/env python3
"""
One-off historical correction: rebuilds Sheet 1's Razorpay row for every date Aug 1 - Sep 4
using real point-in-time reconstruction from razorpay_subscriptions.state_history (active
status as of end of each day) and razorpay_payments (AOV as of end of each day).

Cashfree/Paytm rows are left completely untouched (no history table exists to reconstruct them
accurately). Only cells C:M of the matching (date, 'Razorpay') row are overwritten — column A/B
and every other row are never touched.

Run once: python3 reconcile_august.py [--dry-run]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync_arr as s

DRY_RUN = "--dry-run" in sys.argv

active_by_day = {r[0][:10]: r[1] for r in json.load(open("/tmp/active_by_day.json"))}
aov_by_day = {r[0][:10]: r[1] for r in json.load(open("/tmp/aov_by_day.json"))}
new_churned_by_day = {r[0][:10]: (r[1], r[2]) for r in json.load(open("/tmp/new_churned_by_day.json"))}
JUL31_AOV = 380.3423722560413
JUL31_ACTIVE = active_by_day["2026-07-31"]

DATES = sorted(d for d in active_by_day if d >= "2026-08-01")

prev_mrr, prev_active, prev_aov = JUL31_AOV * JUL31_ACTIVE, JUL31_ACTIVE, JUL31_AOV

rows_by_date = {}
for d in DATES:
    active = active_by_day[d]
    aov = aov_by_day[d]
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

if DRY_RUN:
    for d in DATES[:3] + DATES[-3:]:
        print(d, rows_by_date[d])
    sys.exit(0)

ss = s.sheets_client()
ws = ss.worksheet("Sheet 1")
all_values = ws.get_all_values()
headers = all_values[0]
date_idx, gw_idx = headers.index("Date"), headers.index("Payment Gateway")

# locate the exact sheet row for each (date, Razorpay) pair
target_rows = {}
for i, row in enumerate(all_values[1:], start=2):
    if len(row) > gw_idx and row[gw_idx] == "Razorpay" and row[date_idx] in rows_by_date:
        target_rows[row[date_idx]] = i

missing = set(rows_by_date) - set(target_rows)
if missing:
    print(f"WARNING: no existing Razorpay row found for {len(missing)} dates (will skip): {sorted(missing)[:5]}...")

print(f"Matched {len(target_rows)} existing Razorpay rows to update")

# batch_update: one value range per row, columns C:M only
data = []
for d, row_num in target_rows.items():
    data.append({
        "range": f"C{row_num}:M{row_num}",
        "values": [rows_by_date[d]],
    })

CHUNK = 40
for i in range(0, len(data), CHUNK):
    chunk = data[i:i + CHUNK]
    ws.batch_update(chunk, value_input_option="RAW")
    print(f"  wrote rows {i+1}-{i+len(chunk)} of {len(data)}")

print("Done.")
