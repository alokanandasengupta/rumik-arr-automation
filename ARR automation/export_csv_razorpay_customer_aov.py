#!/usr/bin/env python3
"""
Variant of export_csv.py: same everything, EXCEPT Razorpay's AOV is swapped from the live
formula's "company-wide captured razorpay_payments average" to a narrower, more precise figure —
the average of ONLY the 27,179 (or whatever it is right now) active subscribers' OWN payments,
linked via Razorpay's own customer_id (razorpay_subscriptions.customer_id = razorpay_payments.customer_id).

Cashfree/Paytm are untouched (still mandate-based, as in the live formula). Everything downstream
(MRR = AOV x active, ARR = MRR x 12, USD conversion, the blended Minute3Gateway average) recomputes
automatically from this one substituted number, using the same functions as the live script.

Read-only against Metabase, does not touch the Google Sheet or arr_sync_state.json.
Run: python3 export_csv_razorpay_customer_aov.py
Output: csv_export/minute3gateway_razorpay_customer_aov.csv
"""
import csv
import json
import subprocess
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync_arr as s

OUT_DIR = Path(__file__).parent / "csv_export"
OUT_DIR.mkdir(exist_ok=True)
s.save_state = lambda state: None  # this export must never write local state either

RAZORPAY_CUSTOMER_AOV_SQL = """
with active_customers as (
  select distinct entity_data->>'customer_id' as customer_id
  from razorpay_subscriptions
  where current_status = 'active' and entity_data->>'customer_id' is not null
)
select
  count(distinct a.customer_id) as customers_matched,
  count(*) as payments_matched,
  avg((p.entity_data->>'amount')::numeric / 100.0) as aov
from active_customers a
join razorpay_payments p on p.entity_data->>'customer_id' = a.customer_id
where (p.entity_data->>'status') = 'captured'
  and (p.entity_data->>'amount')::numeric > 100
  and (p.entity_data->>'amount')::numeric <= 99900
"""

midnight_today = datetime.now(s.IST).replace(hour=0, minute=0, second=0, microsecond=0)
lookback_minutes = int((datetime.now(s.IST) - midnight_today).total_seconds() // 60) + 1

print(f"Computing fresh values for today ({midnight_today.strftime('%Y-%m-%d')}), "
      f"midnight to now ({lookback_minutes} minutes)...")

gw_state = s.fetch_gateway_state()
razorpay_before = gw_state["razorpay_caw_recurring"]["avg_mrr_per_subscriber"]

row = s.mb_query(RAZORPAY_CUSTOMER_AOV_SQL)[0]
razorpay_after = float(row["aov"])
gw_state["razorpay_caw_recurring"]["avg_mrr_per_subscriber"] = razorpay_after

print(f"Razorpay AOV swapped: {razorpay_before:.2f} (company-wide) -> {razorpay_after:.2f} "
      f"(active subscribers' own payments, {row['customers_matched']} customers matched, "
      f"{row['payments_matched']} payments)")
print("Full gateway state used for this export:")
for label, p in s.GATEWAYS:
    gs = gw_state[p]
    print(f"  {label}: active={gs['active_subscribers']} aov={gs['avg_mrr_per_subscriber']:.2f}")

minute_series = s.fetch_today_minute_series()
distinct_payers = s.fetch_recent_distinct_payers(lookback_minutes)
churn_by_min, resume_by_min = s.fetch_minute_churn_resume(lookback_minutes)

minute_keys, minute_rows, _, _, _ = s.build_minute3gateway_rows(
    gw_state, minute_series, distinct_payers, churn_by_min, resume_by_min, since=midnight_today
)
out_path = OUT_DIR / "minute3gateway_razorpay_customer_aov.csv"
with open(out_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(s.MINUTE_HEADERS)
    w.writerows(minute_rows)

print(f"\nWrote {len(minute_rows)} rows to {out_path}")
print("Google Sheet and arr_sync_state.json were NOT touched.")
