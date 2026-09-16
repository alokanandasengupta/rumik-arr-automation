#!/usr/bin/env python3
"""
Day-wise, per-gateway Active Subscribers + AOV for August 2026, using the same validated
methodology as fetch_gateway_state() in sync_arr.py, reconstructed point-in-time for each day.

Razorpay: TRUE point-in-time reconstruction from razorpay_subscriptions.state_history (exact
status as of 23:59:59 IST each day) and razorpay_payments (customer_id-restricted AOV, amount
> Rs 1 and <= Rs 999, captured only, as of that same cutoff).

Cashfree/Paytm: no state-history table exists for mandates (only created_at/updated_at) -- per
ARR_MRR_logic.md section 6, the best reproducible proxy is: a mandate counts as active as of day
D if it's currently in the active set, OR if it's currently excluded but its updated_at (the
status-flip timestamp) is AFTER day D (i.e. it was still active as of D). AOV as of day D uses
payment_attempts up to that day for that active-as-of-D population, excluding Rs 1 and >Rs 999,
same join paths as fetch_gateway_state().

IMPORTANT: Metabase silently truncates any query's result to 2000 rows -- razorpay_subscriptions
alone has 129k rows. Every large fetch below is paginated (ORDER BY <stable id> LIMIT/OFFSET) via
mb_query_all() to actually retrieve everything instead of silently working from the first 2000.

Read-only against Metabase. Does not touch the Google Sheet. Writes august_pg_active_aov.csv.
Run: python3 august_daywise_pg_active_aov.py
"""
import csv
import json as _json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import sync_arr as s

IST = s.IST


def mb_query_raises(sql):
    """sync_arr.mb_query() calls die() -> sys.exit(1) on a network failure, which SystemExit
    isn't an Exception subclass and so can't be retried by a normal try/except -- this mirrors
    its logic but raises a plain RuntimeError instead, so mb_query_retrying() below can actually
    catch and retry it. A larger read timeout too (180s vs the original 90s): these paginated
    fetches return much bigger pages than sync_arr's normal live queries."""
    payload = _json.dumps({"database": s.MB_DATABASE_ID, "type": "native", "native": {"query": sql}}).encode()
    req = urllib.request.Request(
        f"{s.MB_URL}/api/dataset", data=payload, method="POST",
        headers={"X-API-Key": s.MB_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180, context=s.SSL_CONTEXT) as resp:
            body = _json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"Metabase request failed: {e}") from e
    if "data" not in body:
        raise RuntimeError(f"Metabase query error: {body.get('error', body)}")
    cols = [c["name"] for c in body["data"]["cols"]]
    return [dict(zip(cols, row)) for row in body["data"]["rows"]]
DATES = [datetime(2026, 8, 1) + timedelta(days=i) for i in range(31)]


def eod_ist(d):
    """23:59:59.999999 IST on date d, as a naive UTC datetime for comparison against timestamps
    normalized to UTC below."""
    return datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=IST).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


CUTOFFS = [eod_ist(d) for d in DATES]
DATE_STRS = [d.strftime("%Y-%m-%d") for d in DATES]

PAGE_SIZE = 2000


def mb_query_retrying(sql, attempts=4):
    """A single flaky page (VPN hiccup, one slow Metabase response) shouldn't blow away several
    minutes of prior pagination progress -- retry a handful of times with backoff before giving
    up for real."""
    import time
    for attempt in range(1, attempts + 1):
        try:
            return mb_query_raises(sql)
        except Exception as e:
            if attempt == attempts:
                raise
            wait = 5 * attempt
            print(f"    (query failed: {e}; retrying in {wait}s, attempt {attempt}/{attempts})")
            time.sleep(wait)


def mb_query_all(select_sql, order_col, page_size=PAGE_SIZE):
    """select_sql must be a complete query (CTEs/joins/where all fine) with NO trailing
    order/limit/offset -- this wraps it as a subquery and pages through the full result set."""
    results = []
    offset = 0
    while True:
        page_sql = f"select * from ({select_sql}) _pg order by {order_col} limit {page_size} offset {offset}"
        page = mb_query_retrying(page_sql)
        results.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        print(f"    fetched {offset} rows so far...")
    return results


import re


def robust_parse_ts(value):
    """mandates.created_at/updated_at come back as ISO strings carrying their own real UTC
    offset (e.g. '...+05:30'), unlike sync_arr.parse_ts()'s inputs which are pre-converted to
    IST wall-clock via SQL 'AT TIME ZONE' and safe to strip tzinfo from directly. Some also have
    non-standard fractional-second precision (5 digits instead of 3/6) that Python 3.10's strict
    datetime.fromisoformat() rejects outright. This pads/truncates the fractional part to exactly
    6 digits, then converts to naive UTC (matching CUTOFFS' own representation below) instead of
    just stripping tzinfo -- stripping without converting would silently misalign IST-offset
    mandate timestamps against UTC cutoffs by 5.5 hours."""
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo("UTC")).replace(tzinfo=None) if value.tzinfo else value
    v = value.replace("Z", "+00:00")
    m = re.match(r"^(.*?)(?:\.(\d+))?([+-]\d{2}:\d{2})?$", v)
    if m and m.group(2):
        base, frac, tz = m.groups()
        frac = (frac + "000000")[:6]
        v = f"{base}.{frac}{tz or ''}"
    return datetime.fromisoformat(v).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


CACHE_DIR = Path(__file__).parent / ".august_cache"
CACHE_DIR.mkdir(exist_ok=True)


def cached_mb_query_all(cache_name, select_sql, order_col):
    cache_file = CACHE_DIR / f"{cache_name}.json"
    if cache_file.exists():
        print(f"  (using cached {cache_file.name})")
        return _json.loads(cache_file.read_text())
    rows = mb_query_all(select_sql, order_col)
    cache_file.write_text(_json.dumps(rows))
    return rows


print("Fetching Razorpay subscriptions (state_history + entity_data)...")
rzp_subs = cached_mb_query_all("rzp_subs", """
    select id, current_status, entity_data->>'customer_id' as customer_id,
           (entity_data->>'created_at')::bigint as created_epoch, state_history::text as state_history
    from razorpay_subscriptions
""", "id")
print(f"  {len(rzp_subs)} subscriptions")

print("Fetching Razorpay captured payments (customer_id, amount, created_at)...")
rzp_payments = cached_mb_query_all("rzp_payments", """
    select id, entity_data->>'customer_id' as customer_id,
           (entity_data->>'amount')::numeric / 100.0 as amount,
           (entity_data->>'created_at')::bigint as created_epoch
    from razorpay_payments
    where entity_data->>'status' = 'captured'
      and (entity_data->>'amount')::numeric > 100
      and (entity_data->>'amount')::numeric <= 99900
""", "id")
print(f"  {len(rzp_payments)} qualifying payments")


def parse_events(sub):
    raw = sub["state_history"]
    if not raw:
        return []
    try:
        events = _json.loads(raw)
    except (TypeError, ValueError):
        return []
    out = []
    for e in events:
        ts = e.get("timestamp")
        status = e.get("status")
        if not ts or not status:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        except ValueError:
            continue
        out.append((dt, status))
    out.sort(key=lambda x: x[0])
    return out


print("Reconstructing per-subscription status timelines...")
sub_timelines = []  # (customer_id, sorted [(ts, status), ...], created_dt)
for sub in rzp_subs:
    events = parse_events(sub)
    created_dt = datetime.utcfromtimestamp(sub["created_epoch"]) if sub["created_epoch"] else None
    sub_timelines.append((sub["customer_id"], events, created_dt))


def status_as_of(events, created_dt, cutoff):
    """Latest known status at/before cutoff; if no event yet, assume active from creation
    (Razorpay subscriptions start active on mandate registration; state_history logs the
    transitions AWAY from that initial state)."""
    status = None
    for ts, st in events:
        if ts <= cutoff:
            status = st
        else:
            break
    if status is not None:
        return status
    if created_dt and created_dt <= cutoff:
        return "active"
    return None


print("Computing Razorpay active-customer sets per day (walks all subscriptions x 31 days)...")
active_customers_by_day = []
for cutoff in CUTOFFS:
    active_set = set()
    for customer_id, events, created_dt in sub_timelines:
        if customer_id and status_as_of(events, created_dt, cutoff) == "active":
            active_set.add(customer_id)
    active_customers_by_day.append(active_set)

print("Computing Razorpay AOV per day (payments up to each cutoff, restricted to that day's active customers)...")
rzp_results = {}
for i, cutoff in enumerate(CUTOFFS):
    active_set = active_customers_by_day[i]
    amounts = [p["amount"] for p in rzp_payments
               if p["customer_id"] in active_set and p["created_epoch"] and
               datetime.utcfromtimestamp(p["created_epoch"]) <= cutoff]
    aov = (sum(amounts) / len(amounts)) if amounts else 0.0
    rzp_results[DATE_STRS[i]] = {"active": len(active_set), "aov": round(aov, 2)}
    print(f"  {DATE_STRS[i]}: active={len(active_set)} aov={aov:.2f} (n_payments={len(amounts)})")

# ---------------------------------------------------------------------------
# Cashfree / Paytm -- approximate reconstruction (no history table available)
# ---------------------------------------------------------------------------

def cashfree_paytm_daywise(provider_account, gateway_label):
    print(f"Fetching {gateway_label} mandates (paginated)...")
    mandates = cached_mb_query_all(f"mandates_{provider_account}", f"""
        select distinct on (user_id) user_id, id as mandate_id, status, created_at, updated_at
        from mandates
        where provider_account = '{provider_account}'
        order by user_id, created_at desc
    """, "user_id")
    print(f"  {len(mandates)} latest-mandate rows")

    if provider_account == "cashfree_recurring":
        payments = cached_mb_query_all("cashfree_payments", """
            select pa.id, i.user_id, pa.amount_paise, pa.completed_at
            from payment_attempts pa
            join invoices i on i.charge_id = pa.charge_id
            where pa.provider_account = 'cashfree_recurring' and pa.status = 'succeeded'
              and pa.amount_paise != 100 and pa.amount_paise <= 99900
        """, "id")
    else:
        payments = cached_mb_query_all("paytm_payments", """
            select pa.id, bp.user_id, pa.amount_paise, pa.completed_at
            from payment_attempts pa
            join paytm_billing_plans bp on bp.last_attempt_id = pa.id
            where pa.provider_account = 'paytm_recurring' and pa.status = 'succeeded'
              and pa.amount_paise != 100 and pa.amount_paise <= 99900
        """, "id")
    print(f"  {len(payments)} qualifying payments")

    # Pre-parse every timestamp exactly once instead of re-parsing per (mandate, day) pair --
    # 31 cutoffs x tens of thousands of mandates would otherwise re-run the same regex-based
    # parse tens of thousands of times over.
    parsed_mandates = []
    for m in mandates:
        created_dt = robust_parse_ts(m["created_at"]) if m["created_at"] else None
        updated_dt = robust_parse_ts(m["updated_at"]) if m["updated_at"] else None
        parsed_mandates.append((m["user_id"], m["status"], created_dt, updated_dt))

    parsed_payments = []
    for p in payments:
        completed_dt = robust_parse_ts(p["completed_at"]) if p["completed_at"] else None
        parsed_payments.append((p["user_id"], p["amount_paise"] / 100.0, completed_dt))

    results = {}
    for i, cutoff in enumerate(CUTOFFS):
        active_users = set()
        for user_id, status, created_dt, updated_dt in parsed_mandates:
            if created_dt and created_dt > cutoff:
                continue  # mandate didn't exist yet
            if status in ("active", "created", "paused", "expired"):
                active_users.add(user_id)
            elif status in ("revoked", "failed", "authorization_pending"):
                if updated_dt and updated_dt > cutoff:
                    active_users.add(user_id)
        amounts = [amount for user_id, amount, completed_dt in parsed_payments
                   if user_id in active_users and completed_dt and completed_dt <= cutoff]
        aov = (sum(amounts) / len(amounts)) if amounts else 0.0
        results[DATE_STRS[i]] = {"active": len(active_users), "aov": round(aov, 2)}
        print(f"  {DATE_STRS[i]}: active={len(active_users)} aov={aov:.2f} (n_payments={len(amounts)})")
    return results


cashfree_results = cashfree_paytm_daywise("cashfree_recurring", "Cashfree")
paytm_results = cashfree_paytm_daywise("paytm_recurring", "Paytm")

# ---------------------------------------------------------------------------
# Write CSV
# ---------------------------------------------------------------------------
OUT = Path(__file__).parent / "csv_export" / "august_pg_active_aov.csv"
OUT.parent.mkdir(exist_ok=True)
with open(OUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Payment Gateway", "Active Subscribers", "Avg MRR per Subscriber (Rs) / AOV"])
    for d in DATE_STRS:
        w.writerow([d, "Cashfree", cashfree_results[d]["active"], cashfree_results[d]["aov"]])
        w.writerow([d, "Paytm", paytm_results[d]["active"], paytm_results[d]["aov"]])
        w.writerow([d, "Razorpay", rzp_results[d]["active"], rzp_results[d]["aov"]])

print(f"\nWrote {OUT}")
print("Google Sheet was NOT touched -- this is read-only against Metabase.")
