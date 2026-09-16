#!/usr/bin/env python3
"""
Minute3Gateway has no rows between 2026-09-05 20:08 and 2026-09-05 23:59 -- a ~4 hour outage
(Metabase/Google connectivity) meant every sync attempt in that window failed, so the tab jumps
straight from 20:07 to the next morning. This backfills exactly that gap using the same per-
minute mechanics as build_minute3gateway_rows() in sync_arr.py, seeded from the last good row
(2026-09-05 20:07: Active Subscribers=30241, Avg MRR per Subscriber=557.11, Cumulative Revenue=
55288) and walking forward through Sep 5's real payment/churn/resume events for the rest of the
day. AOV is held at the seed's value through the gap (the live automation itself only ever
recomputes it once per cycle and applies it uniformly across that cycle's window, so this matches
normal behavior for a range no cycle ever covered).

After appending the gap rows, calls the same apply_minute3gateway_active_formula() and verify_
and_backfill_formula_columns() the live automation uses, so column P picks up the newly-filled
rows via the exact same drag-down chain -- "P column does the entire work" per your instruction,
nothing bespoke.

Run: python3 backfill_sep5_gap.py [--dry-run]
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync_arr as s

DRY_RUN = "--dry-run" in sys.argv

GAP_START = datetime(2026, 9, 5, 20, 8)
GAP_END = datetime(2026, 9, 5, 23, 59)

SEED_ACTIVE = 30241
SEED_AOV = 557.11
SEED_CUMULATIVE = 55288.0

print(f"Backfilling Minute3Gateway from {GAP_START} to {GAP_END} (IST, inclusive)...")


def fetch_minute_series_for_day(date_str):
    sql = f"""
    select date_trunc('minute', completed_at at time zone 'Asia/Kolkata') as minute_ist,
           count(*) as payments, sum(amount_paise) / 100.0 as revenue
    from payment_attempts
    where status = 'succeeded' and provider_account in ('cashfree_recurring', 'paytm_recurring')
      and (completed_at at time zone 'Asia/Kolkata')::date = '{date_str}'::date
    group by 1
    union all
    select date_trunc('minute', to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata') as minute_ist,
           count(*) as payments, sum((entity_data->>'amount')::numeric) / 100.0 as revenue
    from razorpay_payments
    where (entity_data->>'status') = 'captured'
      and (to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata')::date = '{date_str}'::date
    group by 1
    """
    rows = s.mb_query(sql)
    by_minute = {}
    for r in rows:
        m = s.parse_ts(r["minute_ist"])
        d = by_minute.setdefault(m, {"payments": 0, "revenue": 0.0})
        d["payments"] += int(r["payments"] or 0)
        d["revenue"] += float(r["revenue"] or 0)
    return by_minute


def fetch_distinct_payers_for_day(date_str):
    sql = f"""
    with recent_users as (
      select date_trunc('minute', pa.completed_at at time zone 'Asia/Kolkata') as minute_ist,
             i.user_id::text as payer_key
      from payment_attempts pa
      join invoices i on i.charge_id = pa.charge_id
      where pa.status='succeeded' and pa.provider_account='cashfree_recurring'
        and (pa.completed_at at time zone 'Asia/Kolkata')::date = '{date_str}'::date
      union all
      select date_trunc('minute', pa.completed_at at time zone 'Asia/Kolkata'), bp.user_id::text
      from payment_attempts pa
      join paytm_billing_plans bp on bp.last_attempt_id = pa.id
      where pa.status='succeeded' and pa.provider_account='paytm_recurring'
        and (pa.completed_at at time zone 'Asia/Kolkata')::date = '{date_str}'::date
      union all
      select date_trunc('minute', to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata'),
             entity_data->>'customer_id'
      from razorpay_payments
      where (entity_data->>'status') = 'captured'
        and (to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata')::date = '{date_str}'::date
    )
    select minute_ist, count(distinct payer_key) as distinct_payers
    from recent_users
    group by 1
    """
    rows = s.mb_query(sql)
    return {s.parse_ts(r["minute_ist"]): int(r["distinct_payers"]) for r in rows}


def fetch_churn_resume_for_day(date_str):
    churn_sql = f"""
    select date_trunc('minute', updated_at at time zone 'Asia/Kolkata') as minute_ist, count(*) as n
    from mandates
    where (updated_at at time zone 'Asia/Kolkata')::date = '{date_str}'::date
      and created_at < updated_at - interval '1 minute'
      and status in {s.CHURNED_STATUSES_SQL}
    group by 1
    """
    resume_sql = f"""
    select date_trunc('minute', updated_at at time zone 'Asia/Kolkata') as minute_ist, count(*) as n
    from mandates
    where (updated_at at time zone 'Asia/Kolkata')::date = '{date_str}'::date
      and created_at < updated_at - interval '1 minute'
      and status in {s.ACTIVE_STATUSES_SQL}
    group by 1
    """
    churn = {s.parse_ts(r["minute_ist"]): int(r["n"]) for r in s.mb_query(churn_sql)}
    resume = {s.parse_ts(r["minute_ist"]): int(r["n"]) for r in s.mb_query(resume_sql)}
    return churn, resume


print("Fetching Sep 5 payment series, distinct payers, churn/resume...")
minute_series = fetch_minute_series_for_day("2026-09-05")
payers = fetch_distinct_payers_for_day("2026-09-05")
churn_by_min, resume_by_min = fetch_churn_resume_for_day("2026-09-05")
print(f"  {len(minute_series)} minutes with payment activity, {len(payers)} minutes with payers, "
      f"{len(churn_by_min)} churn minutes, {len(resume_by_min)} resume minutes (full day)")

rows = []
keys = []
cumulative = SEED_CUMULATIVE
active = SEED_ACTIVE
minute_cursor = GAP_START
while minute_cursor <= GAP_END:
    data = minute_series.get(minute_cursor, {"payments": 0, "revenue": 0.0})
    churn = churn_by_min.get(minute_cursor, 0)
    resume = resume_by_min.get(minute_cursor, 0)
    active += resume - churn
    cumulative += data["revenue"]
    mrr, arr, mrr_usd, arr_usd = s.mrr_row_values(active, SEED_AOV)
    key = minute_cursor.strftime("%Y-%m-%d %H:%M")
    rows.append([
        key, data["payments"], payers.get(minute_cursor, 0), round(data["revenue"], 2),
        round(cumulative, 2), active, churn, resume, SEED_AOV, mrr, arr, mrr_usd, arr_usd, 0, 0,
    ])
    keys.append(key)
    minute_cursor += timedelta(minutes=1)

print(f"Built {len(rows)} rows, {keys[0]} to {keys[-1]}")
print("First 3:", rows[:3])
print("Last 3:", rows[-3:])
total_churn = sum(r[6] for r in rows)
total_resume = sum(r[7] for r in rows)
print(f"Total churn={total_churn} resume={total_resume} net={total_resume - total_churn} "
      f"-> active goes from {SEED_ACTIVE} to {rows[-1][5]}")

if DRY_RUN:
    print("\nDRY RUN — not writing.")
    sys.exit(0)

import gspread

ws = s.get_or_create_worksheet("Minute3Gateway", s.MINUTE_HEADERS)
ws.append_rows(rows, value_input_option="RAW")
print(f"Appended {len(rows)} rows.")

last_row = len(ws.col_values(1))
ws.sort((1, "asc"), (2, "asc"), range=f"A2:{gspread.utils.rowcol_to_a1(last_row, len(s.MINUTE_HEADERS))}")
print("Sorted.")

last_row = s.apply_minute3gateway_active_formula(ws)
print(f"apply_minute3gateway_active_formula done, last_row={last_row}")
if last_row is not None:
    s.verify_and_backfill_formula_columns(ws, last_row)
print("Done.")
