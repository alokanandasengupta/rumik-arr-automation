#!/usr/bin/env python3
"""
Exports clean, freshly-computed CSVs for all 3 tabs (Sheet1, Intraday10min, Minute3Gateway),
covering today (IST) from midnight to now, using the exact same logic as sync_arr.py.

Does NOT touch the Google Sheet at all — read-only against Metabase, writes local CSV files only.
Use this as a trustworthy reference to compare against the live sheet (which has an unresolved
issue with a second, unidentified writer producing conflicting values on some rows).

Run: python3 export_csv.py
Output: csv_export/sheet1.csv, csv_export/intraday10min.csv, csv_export/minute3gateway.csv
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync_arr as s

OUT_DIR = Path(__file__).parent / "csv_export"
OUT_DIR.mkdir(exist_ok=True)

midnight_today = datetime.now(s.IST).replace(hour=0, minute=0, second=0, microsecond=0)
lookback_minutes = int((datetime.now(s.IST) - midnight_today).total_seconds() // 60) + 1

print(f"Computing fresh values for today ({midnight_today.strftime('%Y-%m-%d')}), "
      f"midnight to now ({lookback_minutes} minutes)...")

gw_state = s.fetch_gateway_state()
print("Current gateway state:")
for label, p in s.GATEWAYS:
    gs = gw_state[p]
    print(f"  {label}: active={gs['active_subscribers']} aov={gs['avg_mrr_per_subscriber']:.2f}")

minute_series = s.fetch_today_minute_series()
distinct_payers = s.fetch_recent_distinct_payers(lookback_minutes)
churn_by_min, resume_by_min = s.fetch_minute_churn_resume(lookback_minutes)

# --- Sheet1 (today's row only) ---
# build_sheet1_rows() normally persists to arr_sync_state.json (save_state()) -- neutralize that
# here so this export is strictly read-only against everything except the CSV files below.
s.save_state = lambda state: None
state = s.load_state()
today_str, sheet1_rows = s.build_sheet1_rows(gw_state, state)
with open(OUT_DIR / "sheet1.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(s.SHEET1_HEADERS)
    w.writerows(sheet1_rows)
print(f"Wrote {len(sheet1_rows)} rows to csv_export/sheet1.csv")

# --- Intraday10min (every 10-min bucket since midnight) ---
bucket_keys, intraday_rows = s.build_intraday10min_rows(gw_state, minute_series, distinct_payers, since=midnight_today)
with open(OUT_DIR / "intraday10min.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(s.INTRADAY_HEADERS)
    w.writerows(intraday_rows)
print(f"Wrote {len(intraday_rows)} rows ({len(bucket_keys)} buckets) to csv_export/intraday10min.csv")

# --- Minute3Gateway (every minute since midnight) ---
minute_keys, minute_rows, _, _, _ = s.build_minute3gateway_rows(
    gw_state, minute_series, distinct_payers, churn_by_min, resume_by_min, since=midnight_today
)
with open(OUT_DIR / "minute3gateway.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(s.MINUTE_HEADERS)
    w.writerows(minute_rows)
print(f"Wrote {len(minute_rows)} rows to csv_export/minute3gateway.csv")

print(f"\nDone. Files in: {OUT_DIR}")
print("Note: the Google Sheet itself was NOT touched — this only reads from Metabase and writes local CSVs.")
