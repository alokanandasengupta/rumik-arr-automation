#!/usr/bin/env python3
"""
Replaces today's flat, backfilled Minute3Gateway (and Intraday10min) rows with genuine
point-in-time reconstruction for Razorpay (state_history for active count, the cumulative
customer_id-restricted payment series for AOV) -- same technique validated in
export_csv_razorpay_timevarying.py, applied directly to the live sheet via the normal
upsert_rows() path instead of a CSV.

Cashfree/Paytm stay static (current values) -- no history table exists for those.

Run: python3 fix_today_timevarying.py [--dry-run]
"""
import bisect
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync_arr as s

s.DRY_RUN = "--dry-run" in sys.argv
s.save_state = lambda state: None  # this script must never touch arr_sync_state.json

today_date_str = datetime.now(s.IST).strftime("%Y-%m-%d")
TODAY = datetime.now(s.IST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
NOW = datetime.now(s.IST).replace(second=0, microsecond=0, tzinfo=None)

print(f"Reconstructing {today_date_str} from midnight to {NOW.strftime('%H:%M')}...")

# --- Razorpay active count, per minute (real reconstruction) ---
baseline_sql = f"""
with events as (
  select rs.id as sub_id, (e->>'status') as status, (e->>'timestamp')::timestamptz as event_ts,
         lead((e->>'timestamp')::timestamptz) over (partition by rs.id order by (e->>'timestamp')::timestamptz) as next_ts
  from razorpay_subscriptions rs, jsonb_array_elements(rs.state_history) as e
)
select count(*) filter (where status='active') as active_at_midnight
from events
where event_ts <= ('{today_date_str}'::date at time zone 'Asia/Kolkata')
  and (next_ts is null or next_ts > ('{today_date_str}'::date at time zone 'Asia/Kolkata'))
"""
baseline_active = s.mb_query(baseline_sql)[0]["active_at_midnight"]

deltas_sql = f"""
with events as (
  select rs.id as sub_id, (e->>'status') as status, (e->>'timestamp')::timestamptz as event_ts,
         lead((e->>'timestamp')::timestamptz) over (partition by rs.id order by (e->>'timestamp')::timestamptz) as next_ts
  from razorpay_subscriptions rs, jsonb_array_elements(rs.state_history) as e
),
deltas as (
  select date_trunc('minute', event_ts at time zone 'Asia/Kolkata') as minute_ist, 1 as delta
  from events where status = 'active' and event_ts >= ('{today_date_str}'::date at time zone 'Asia/Kolkata')
  union all
  select date_trunc('minute', next_ts at time zone 'Asia/Kolkata') as minute_ist, -1 as delta
  from events where status = 'active' and next_ts is not null
    and next_ts >= ('{today_date_str}'::date at time zone 'Asia/Kolkata')
)
select minute_ist, sum(delta) as delta from deltas group by 1 order by 1
"""
delta_rows = s.mb_query(deltas_sql)
deltas_by_minute = {s.parse_ts(r["minute_ist"]): int(r["delta"]) for r in delta_rows}
print(f"Baseline active at midnight: {baseline_active}, {len(delta_rows)} change-points today")

# --- Genuine cumulative AOV (all-time, active-customer-restricted), as-of each minute ---
aov_sql = """
with active_customers as (
  select distinct entity_data->>'customer_id' as customer_id
  from razorpay_subscriptions
  where current_status = 'active' and entity_data->>'customer_id' is not null
),
payments as (
  select to_timestamp((p.entity_data->>'created_at')::bigint) as ts,
         (p.entity_data->>'amount')::numeric/100.0 as amount
  from razorpay_payments p
  join active_customers a on p.entity_data->>'customer_id' = a.customer_id
  where (p.entity_data->>'status') = 'captured'
    and (p.entity_data->>'amount')::numeric > 100
    and (p.entity_data->>'amount')::numeric <= 99900
)
select ts, sum(amount) over (order by ts) / count(*) over (order by ts) as cumulative_aov
from payments order by ts
"""
aov_rows = s.mb_query(aov_sql)
aov_timestamps = [s.parse_ts(r["ts"]) for r in aov_rows]
aov_values = [float(r["cumulative_aov"]) for r in aov_rows]


def aov_as_of(minute_naive):
    idx = bisect.bisect_right(aov_timestamps, minute_naive) - 1
    return aov_values[idx] if idx >= 0 else 0.0


# --- Cashfree/Paytm current state (static) ---
gw_state = s.fetch_gateway_state()
cashfree_active = gw_state["cashfree_recurring"]["active_subscribers"]
cashfree_aov = gw_state["cashfree_recurring"]["avg_mrr_per_subscriber"]
paytm_active = gw_state["paytm_recurring"]["active_subscribers"]
paytm_aov = gw_state["paytm_recurring"]["avg_mrr_per_subscriber"]
static_mrr = cashfree_active * cashfree_aov + paytm_active * paytm_aov
static_active = cashfree_active + paytm_active

# --- Today's real per-minute Razorpay payments (for the Payments/Revenue/CumRevenue columns) ---
payments_sql = f"""
select date_trunc('minute', to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata') as minute_ist,
       count(*) as n, sum((entity_data->>'amount')::numeric)/100.0 as total_amount
from razorpay_payments
where (entity_data->>'status') = 'captured'
  and (to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata')::date = '{today_date_str}'::date
group by 1 order by 1
"""
payment_rows = s.mb_query(payments_sql)
payments_by_minute = {s.parse_ts(r["minute_ist"]): (int(r["n"]), float(r["total_amount"])) for r in payment_rows}

# --- Build Minute3Gateway rows for the whole day ---
minute_rows, minute_keys = [], []
running_active = baseline_active
running_revenue = 0.0
minute_cursor = TODAY
per_minute_snapshot = {}  # minute -> (razorpay_active, razorpay_aov) for reuse in Intraday10min
while minute_cursor <= NOW:
    running_active += deltas_by_minute.get(minute_cursor, 0)
    n, total = payments_by_minute.get(minute_cursor, (0, 0.0))
    running_revenue += total
    razorpay_aov = aov_as_of(minute_cursor)
    per_minute_snapshot[minute_cursor] = (running_active, razorpay_aov)

    total_active = running_active + static_active
    total_mrr = running_active * razorpay_aov + static_mrr
    avg_mrr_combined = (total_mrr / total_active) if total_active else 0
    mrr, arr, mrr_usd, arr_usd = s.mrr_row_values(total_active, avg_mrr_combined)

    key = minute_cursor.strftime("%Y-%m-%d %H:%M")
    minute_rows.append([
        key, n, n, round(total, 2), round(running_revenue, 2),
        total_active, 0, 0,
        round(avg_mrr_combined, 2), mrr, arr, mrr_usd, arr_usd,
    ])
    minute_keys.append(key)
    minute_cursor += timedelta(minutes=1)

print(f"Built {len(minute_rows)} Minute3Gateway rows")
s.upsert_rows("Minute3Gateway", "Time (1-min)", s.MINUTE_HEADERS, minute_keys, minute_rows)
print("Minute3Gateway updated.")

# --- Build Intraday10min rows (per-gateway) for the whole day, using the same per-minute snapshot ---
intraday_rows, intraday_keys = [], []
bucket_cursor = TODAY
while bucket_cursor <= NOW.replace(minute=(NOW.minute // 10) * 10):
    bucket_end = bucket_cursor + timedelta(minutes=10)
    key = bucket_cursor.strftime("%Y-%m-%d %H:%M")

    # snapshot at the end of this bucket (last minute in it that we have data for)
    snap_minute = min(bucket_end - timedelta(minutes=1), NOW)
    razorpay_active, razorpay_aov = per_minute_snapshot.get(snap_minute, (baseline_active, aov_as_of(snap_minute)))

    bucket_payments = sum(payments_by_minute.get(bucket_cursor + timedelta(minutes=m), (0, 0.0))[0] for m in range(10))
    bucket_revenue = sum(payments_by_minute.get(bucket_cursor + timedelta(minutes=m), (0, 0.0))[1] for m in range(10))
    cum_revenue = sum(v[1] for k, v in payments_by_minute.items() if TODAY <= k < bucket_end)

    for label, provider in s.GATEWAYS:
        if provider == "razorpay_caw_recurring":
            active, aov = razorpay_active, razorpay_aov
            payments, revenue, cum = bucket_payments, bucket_revenue, cum_revenue
        else:
            active = gw_state[provider]["active_subscribers"]
            aov = gw_state[provider]["avg_mrr_per_subscriber"]
            payments, revenue, cum = 0, 0.0, 0.0
        mrr, arr, mrr_usd, arr_usd = s.mrr_row_values(active, aov)
        intraday_rows.append([
            key, label, payments, payments, round(revenue, 2), round(cum, 2),
            active, round(aov, 2), mrr, arr, mrr_usd, arr_usd,
        ])
    intraday_keys.append(key)
    bucket_cursor = bucket_end

print(f"Built {len(intraday_rows)} Intraday10min rows ({len(intraday_keys)} buckets)")
s.upsert_rows("Intraday10min", "Time (10-min bucket start)", s.INTRADAY_HEADERS, intraday_keys, intraday_rows)
print("Intraday10min updated.")

print("\nDone. arr_sync_state.json was NOT touched.")
