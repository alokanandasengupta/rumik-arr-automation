#!/usr/bin/env python3
"""
Time-varying Razorpay reconstruction for Minute3Gateway, today, per minute:

  - Active Subscribers: real reconstruction from razorpay_subscriptions.state_history
    (same technique as the August historical backfill, at minute granularity).
  - Avg MRR per Subscriber (AOV): the customer_id-restricted logic (avg of captured payments
    from the currently-active-subscriber population, excl Rs 1 and >Rs 999) -- but as a genuine
    ALL-TIME cumulative average evaluated as-of each minute, not "since midnight" (an earlier
    version of this script wrongly reset the average to 0 at midnight, which collapsed it to a
    meaningless ~50-90 given only ~4 real transactions land on this population per day; the fix
    is to carry forward the full historical cumulative average and only nudge it when a new
    payment from this population actually lands).

Cashfree/Paytm stay as static "current" values -- mandates has no status-history table, so a true
per-minute reconstruction isn't possible for those two (see ARR_MRR_logic.md).

Read-only against Metabase, does not touch the Google Sheet or arr_sync_state.json.
Run: python3 export_csv_razorpay_timevarying.py
Output: csv_export/minute3gateway_razorpay_timevarying.csv
"""
import bisect
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync_arr as s

OUT_DIR = Path(__file__).parent / "csv_export"
OUT_DIR.mkdir(exist_ok=True)
s.save_state = lambda state: None

today_date_str = datetime.now(s.IST).strftime("%Y-%m-%d")
TODAY = datetime.now(s.IST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
NOW = datetime.now(s.IST).replace(second=0, microsecond=0, tzinfo=None)

print(f"Reconstructing Razorpay's per-minute state for {today_date_str}, midnight to {NOW.strftime('%H:%M')}...")

# --- 1. Active subscriber count, per minute, real reconstruction ---
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
print(f"Baseline active count as of midnight: {baseline_active}")

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
print(f"{len(delta_rows)} active-count change-points today")

# --- 2. Genuine cumulative AOV (all-time basis, active-customer-restricted), evaluated as-of each minute ---
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
print(f"{len(aov_rows)} historical payments underlie the cumulative AOV series "
      f"(spans {aov_timestamps[0]} to {aov_timestamps[-1]}, current value {aov_values[-1]:.2f})")


def aov_as_of(minute_naive):
    """Last known cumulative AOV at or before this minute (forward-fill lookup)."""
    idx = bisect.bisect_right(aov_timestamps, minute_naive) - 1
    return aov_values[idx] if idx >= 0 else 0.0


# --- 3. Cashfree/Paytm current state (static -- no history table available) ---
gw_state = s.fetch_gateway_state()
cashfree_active = gw_state["cashfree_recurring"]["active_subscribers"]
cashfree_aov = gw_state["cashfree_recurring"]["avg_mrr_per_subscriber"]
paytm_active = gw_state["paytm_recurring"]["active_subscribers"]
paytm_aov = gw_state["paytm_recurring"]["avg_mrr_per_subscriber"]
static_mrr = cashfree_active * cashfree_aov + paytm_active * paytm_aov
static_active = cashfree_active + paytm_active
print(f"Cashfree (static, current): active={cashfree_active} aov={cashfree_aov:.2f}")
print(f"Paytm (static, current): active={paytm_active} aov={paytm_aov:.2f}")

# --- 4. Today's per-minute payments/revenue (real historical facts, for the bucket-level columns) ---
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

# --- 5. Build the per-minute rows ---
rows = []
running_active = baseline_active
running_revenue = 0.0
minute_cursor = TODAY
while minute_cursor <= NOW:
    running_active += deltas_by_minute.get(minute_cursor, 0)
    n, total = payments_by_minute.get(minute_cursor, (0, 0.0))
    running_revenue += total
    razorpay_aov = aov_as_of(minute_cursor)

    total_active = running_active + static_active
    total_mrr = running_active * razorpay_aov + static_mrr
    avg_mrr_combined = (total_mrr / total_active) if total_active else 0
    mrr, arr, mrr_usd, arr_usd = s.mrr_row_values(total_active, avg_mrr_combined)

    rows.append([
        minute_cursor.strftime("%Y-%m-%d %H:%M"),
        n, n, round(total, 2), round(running_revenue, 2),
        total_active, 0, 0,  # churned/resumed this minute -- not reconstructed here
        round(avg_mrr_combined, 2), mrr, arr, mrr_usd, arr_usd,
    ])
    minute_cursor += timedelta(minutes=1)

out_path = OUT_DIR / "minute3gateway_razorpay_timevarying.csv"
with open(out_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(s.MINUTE_HEADERS)
    w.writerows(rows)

print(f"\nWrote {len(rows)} rows to {out_path}")
print("Google Sheet and arr_sync_state.json were NOT touched.")
print(f"\nFirst row: {rows[0]}")
print(f"Last row:  {rows[-1]}")
