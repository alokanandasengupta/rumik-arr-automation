"""
Daily funnel report — time-insensitive job (see ~/Library/LaunchAgents/com.rumik.dailyfunnel.plist).

The DB is only reachable over VPN, which isn't always on the moment the laptop wakes. So this
script is designed to be triggered repeatedly (every 30 min via launchd StartInterval) and just
no-ops quietly if: (a) it already succeeded today, or (b) the DB isn't reachable right now (no
VPN yet). The first time after midnight that both conditions are met — VPN is up and it hasn't
run yet today — it does the real work, writes the report, and marks itself done for the day.

Produces the Date | Payment Gateway | Distinct Amount (Rs) | Activity | Meta Spend (Rs) | Revenue
table for T-5..T (T = the day it successfully runs), matching DAILY_FUNNEL_REPORT_LOGIC.md exactly.

Maintains a durable full-history cache in .daily_funnel_cache/ (trials, real payments, mandates)
so each run only needs to re-pull a recent refresh window from the DB, not all of history.
"""
import sys, os, json, re, csv, subprocess, traceback, socket
import urllib.error
from datetime import datetime, timedelta, timezone
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".daily_funnel_cache")
LOG_PATH = os.path.join(CACHE, "run_log.txt")
STATE_PATH = os.path.join(CACHE, "last_success_date.txt")
OUT_PATH = os.path.expanduser("~/Desktop/Daily Funnel Report.xlsx")

REFRESH_DAYS = 25   # how far back to re-pull fresh data from the DB each run (covers 15-day mandate gap + settling lag)
TARGET_DAYS = 6      # T-5 .. T inclusive

sys.path.insert(0, HERE)
import sync_arr as s

NETWORK_ERRORS = (TimeoutError, socket.timeout, socket.gaierror, ConnectionError,
                   urllib.error.URLError, OSError)


def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.now().isoformat()} {msg}\n")


def already_ran_today(today_iso):
    if os.path.exists(STATE_PATH):
        return open(STATE_PATH).read().strip() == today_iso
    return False


def mark_success(today_iso):
    with open(STATE_PATH, "w") as f:
        f.write(today_iso)


def mb_query(sql, timeout=400):
    import urllib.request
    payload = json.dumps({"database": s.MB_DATABASE_ID, "type": "native", "native": {"query": sql}}).encode()
    req = urllib.request.Request(
        f"{s.MB_URL}/api/dataset", data=payload, method="POST",
        headers={"X-API-Key": s.MB_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=s.SSL_CONTEXT) as resp:
        body = json.loads(resp.read())
    if "data" not in body:
        raise RuntimeError(f"Metabase query error: {body.get('error', body)}\nSQL:\n{sql}")
    cols = [c["name"] for c in body["data"]["cols"]]
    return [dict(zip(cols, row)) for row in body["data"]["rows"]]


def mb_query_all(select_sql, order_col, page_size=2000):
    all_rows, offset = [], 0
    while True:
        paged = f"select * from ({select_sql}) sub order by {order_col} limit {page_size} offset {offset}"
        rows = mb_query(paged)
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return all_rows


def parse_dt(v):
    if v is None:
        return None
    v = str(v).strip().replace('Z', '+00:00')
    m = re.match(r'^(?P<base>[^.]+)\.(?P<frac>\d+)(?P<tz>[+-]\d{2}:\d{2})?$', v)
    if m:
        frac = (m.group('frac') + '000000')[:6]
        tz = m.group('tz') or ''
        v = f"{m.group('base')}.{frac}{tz}"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def load_json(path, default):
    if os.path.exists(path):
        return json.load(open(path))
    return default


def merge_by_id(old, fresh, id_field):
    merged = {r[id_field]: r for r in old}
    for r in fresh:
        merged[r[id_field]] = r
    return list(merged.values())


def norm(a):
    return int(a) if a == int(a) else a


def to_ddmmyyyy(iso):
    y, m, dd = iso.split('-')
    return f"{dd}/{m}/{y}"


def fetch_attribution(payer_keys, attr_cache):
    missing = [k for k in payer_keys if k and k not in attr_cache]
    for i in range(0, len(missing), 1500):
        batch = missing[i:i + 1500]
        id_list = ','.join(f"'{k}'" for k in batch)
        rows = mb_query(f"select user_id::text as uid, meta_install_attribution as attr from user_profiles where user_id::text in ({id_list})")
        found = {r['uid']: r['attr'] for r in rows}
        for k in batch:
            attr_cache[k] = found.get(k)
    return attr_cache


def parse_attr(attr_raw):
    if not attr_raw:
        return (None, None, None, None)
    try:
        a = json.loads(attr_raw) if isinstance(attr_raw, str) else attr_raw
    except Exception:
        return (None, None, None, None)
    cgid = a.get('campaignGroupId')
    cgname = a.get('campaignGroupName')
    aid = a.get('campaignId')
    aname = a.get('campaignName')
    if not cgid and not aid:
        return (None, None, None, None)
    return (str(cgid) if cgid else None, cgname, str(aid) if aid else None, aname)


def main():
    today = datetime.now().date()
    today_iso = today.isoformat()

    if already_ran_today(today_iso):
        return  # already produced today's report — quiet no-op until tomorrow

    REFRESH_START = (today - timedelta(days=REFRESH_DAYS)).isoformat()
    TODAY = (today + timedelta(days=1)).isoformat()  # exclusive upper bound for BETWEEN-style windows
    TARGET_DATES = {(today - timedelta(days=i)).isoformat() for i in range(TARGET_DAYS)}

    log(f"=== run attempt, today={today}, refresh_start={REFRESH_START}, targets={sorted(TARGET_DATES)}")

    target_min = min(TARGET_DATES)
    candidates_start = (today - timedelta(days=TARGET_DAYS + 14)).isoformat()

    # ---------------- fresh pulls (small window — only what the report needs) ----------------
    log("pulling meta/signup/msg (target window)")
    meta_sql = f"""
    select m.document->>'date' as date, sum((ad->>'spend_paise')::numeric)/100.0 as spend_rs
    from meta_ads_daily m
    cross join lateral jsonb_array_elements(m.document->'campaigns') as campaign
    cross join lateral jsonb_array_elements(campaign->'adsets') as adset
    cross join lateral jsonb_array_elements(adset->'ads') as ad
    where m.document->>'date' >= '{target_min}'
    group by 1
    """
    meta_rows = mb_query(meta_sql)

    signup_sql = f"""
    select u.id::text as uid, (u.created_at at time zone 'Asia/Kolkata')::date::text as day,
           up.meta_install_attribution as attr
    from users u left join user_profiles up on up.user_id = u.id
    where (u.created_at at time zone 'Asia/Kolkata')::date >= '{target_min}'
    """
    signup_rows = mb_query_all(signup_sql, "uid")

    msg_sql = f"""
    with candidates as (
      select id from users where (created_at at time zone 'Asia/Kolkata')::date >= '{candidates_start}'
    )
    select c.id::text as uid, (fm.first_msg at time zone 'Asia/Kolkata')::date::text as day,
           up.meta_install_attribution as attr
    from candidates c
    join lateral (select min(m.timestamp) as first_msg from messages m where m.user_id = c.id and m.role='user') fm on fm.first_msg is not null
    left join user_profiles up on up.user_id = c.id
    where (fm.first_msg at time zone 'Asia/Kolkata')::date >= '{target_min}'
    """
    msg_rows = mb_query_all(msg_sql, "uid", page_size=2000)

    log("pulling campaign/adset spend (target window)")
    camp_spend_sql = f"""
    select m.document->>'date' as date, campaign->>'id' as campaign_id, campaign->>'name' as campaign_name,
           adset->>'id' as adset_id, adset->>'name' as adset_name, (adset->>'spend_paise')::numeric/100.0 as spend_rs
    from meta_ads_daily m
    cross join lateral jsonb_array_elements(m.document->'campaigns') as campaign
    cross join lateral jsonb_array_elements(campaign->'adsets') as adset
    where m.document->>'date' >= '{target_min}'
    """
    camp_spend_rows = mb_query_all(camp_spend_sql, "date")

    log("pulling trial attempts (refresh window)")
    cf_trial_sql = f"""
    select cp._id as rid, ((cp.document->>'payment_time')::timestamptz at time zone 'Asia/Kolkata')::text as paid_at,
           cp.document->>'payment_status' as status,
           coalesce(u1.id::text, u2.id::text) as payer_key
    from cashfree_payments cp
    left join users u1 on u1.email = cp.document->>'customer_email'
    left join users u2 on right(regexp_replace(u2.phone_number, '\\D','','g'), 10) = right(regexp_replace(cp.document->>'customer_phone', '\\D','','g'), 10)
      and cp.document->>'customer_phone' != '9999999999'
    where (cp.document->>'amount_paise')::numeric = 100
      and (cp.document->>'payment_time')::timestamptz >= '{REFRESH_START}'
    """
    cf_trial_fresh = mb_query_all(cf_trial_sql, "rid")

    pt_trial_sql = f"""
    select _id as rid, ((document->>'payment_time')::timestamptz at time zone 'Asia/Kolkata')::text as paid_at,
           document->>'payment_status' as status, document->>'user_id' as payer_key
    from paytm_payments
    where (document->>'amount_paise')::numeric = 100
      and (document->>'payment_time')::timestamptz >= '{REFRESH_START}'
    """
    pt_trial_fresh = mb_query_all(pt_trial_sql, "rid")

    rzp_trial_sql = f"""
    select rp.id as attempt_id, (to_timestamp((rp.entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata')::text as paid_at,
           rp.entity_data->>'status' as status,
           coalesce((regexp_match(rp.entity_data->>'email', '^user-([0-9a-fA-F-]{{36}})@noemail\\.rumik\\.ai$'))[1], u.id::text) as payer_key
    from razorpay_payments rp left join users u on u.email = rp.entity_data->>'email'
    where (rp.entity_data->>'amount')::numeric = 100
      and (to_timestamp((rp.entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata') >= '{REFRESH_START}'
    """
    rzp_trial_fresh = mb_query_all(rzp_trial_sql, "attempt_id")

    log("pulling real (>Rs1) payments (refresh window)")
    cf_real_sql = f"""
    select pa.id as attempt_id, (pa.initiated_at at time zone 'Asia/Kolkata')::text as paid_at,
           pa.amount_paise/100.0 as amount, i.user_id::text as payer_key
    from payment_attempts pa join invoices i on i.charge_id = pa.charge_id
    where pa.provider_account = 'cashfree_recurring' and pa.status = 'succeeded' and pa.amount_paise != 100
      and (pa.initiated_at at time zone 'Asia/Kolkata')::date >= '{REFRESH_START}'
    """
    cf_real_fresh = mb_query_all(cf_real_sql, "attempt_id")

    pt_real_sql = f"""
    select pa.id as attempt_id, (pa.initiated_at at time zone 'Asia/Kolkata')::text as paid_at,
           pa.amount_paise/100.0 as amount, ps.document->>'user_id' as payer_key
    from payment_attempts pa join paytm_subscriptions ps on ps._id = pa.metadata->>'paytm.subscription_id'
    where pa.provider_account = 'paytm_recurring' and pa.status = 'succeeded' and pa.amount_paise != 100
      and (pa.initiated_at at time zone 'Asia/Kolkata')::date >= '{REFRESH_START}'
    """
    pt_real_fresh = mb_query_all(pt_real_sql, "attempt_id")

    rzp_real_sql = f"""
    select rp.id as attempt_id, (to_timestamp((rp.entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata')::text as paid_at,
           (rp.entity_data->>'amount')::numeric/100.0 as amount,
           coalesce((regexp_match(rp.entity_data->>'email', '^user-([0-9a-fA-F-]{{36}})@noemail\\.rumik\\.ai$'))[1], u.id::text) as payer_key
    from razorpay_payments rp left join users u on u.email = rp.entity_data->>'email'
    where (rp.entity_data->>'amount')::numeric != 100 and rp.entity_data->>'status' = 'captured'
      and (to_timestamp((rp.entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata')::date >= '{REFRESH_START}'
    """
    rzp_real_fresh = mb_query_all(rzp_real_sql, "attempt_id")

    log("pulling mandates (refresh window)")
    mandate_cfpt_sql = f"""
    select user_id::text as payer_key, provider_account,
           (created_at at time zone 'Asia/Kolkata')::text as mandate_created_at, status::text as status, max_amount_paise
    from mandates
    where provider_account in ('cashfree_recurring','paytm_recurring')
      and (created_at at time zone 'Asia/Kolkata')::date >= '{REFRESH_START}'
    """
    mandate_cfpt_fresh = mb_query_all(mandate_cfpt_sql, "payer_key")

    mandate_rzp_a_sql = f"""
    select (regexp_match(entity_data->>'customer_email', '^user-([0-9a-fA-F-]{{36}})@noemail\\.rumik\\.ai$'))[1] as payer_key,
           (entity_data->>'created_at')::bigint as created_epoch, entity_data->>'status' as status,
           (entity_data->'notes'->>'expected_price_paise')::numeric as expected_price_paise
    from razorpay_subscriptions
    where entity_data->>'customer_email' ~ '^user-([0-9a-fA-F-]{{36}})@noemail\\.rumik\\.ai$'
      and (to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata')::date >= '{REFRESH_START}'
    """
    mandate_rzp_a_fresh = mb_query_all(mandate_rzp_a_sql, "payer_key")

    mandate_rzp_b_sql = f"""
    select u.id::text as payer_key, (rs.entity_data->>'created_at')::bigint as created_epoch, rs.entity_data->>'status' as status,
           (rs.entity_data->'notes'->>'expected_price_paise')::numeric as expected_price_paise
    from razorpay_subscriptions rs join users u on u.email = rs.entity_data->>'customer_email'
    where (to_timestamp((rs.entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata')::date >= '{REFRESH_START}'
    """
    mandate_rzp_b_fresh = mb_query_all(mandate_rzp_b_sql, "payer_key")

    # ---------------- merge fresh into durable cache ----------------
    log("merging into durable cache")

    def cache_path(name):
        return os.path.join(CACHE, name)

    cf_trial = merge_by_id(load_json(cache_path("trials_cf_base.json"), []), cf_trial_fresh, 'rid')
    pt_trial = merge_by_id(load_json(cache_path("trials_pt_base.json"), []), pt_trial_fresh, 'rid')
    rzp_trial = merge_by_id(load_json(cache_path("trials_rzp_base.json"), []), rzp_trial_fresh, 'attempt_id')
    json.dump(cf_trial, open(cache_path("trials_cf_base.json"), 'w'))
    json.dump(pt_trial, open(cache_path("trials_pt_base.json"), 'w'))
    json.dump(rzp_trial, open(cache_path("trials_rzp_base.json"), 'w'))
    for r in cf_trial: r.setdefault('status', 'SUCCESS'); r['gateway'] = 'Cashfree'
    for r in pt_trial: r.setdefault('status', 'SUCCESS'); r['gateway'] = 'Paytm'
    for r in rzp_trial: r.setdefault('status', 'captured'); r['gateway'] = 'Razorpay'

    cf_real = merge_by_id(load_json(cache_path("real_cf_base.json"), []), cf_real_fresh, 'attempt_id')
    pt_real = merge_by_id(load_json(cache_path("real_pt_base.json"), []), pt_real_fresh, 'attempt_id')
    rzp_real = merge_by_id(load_json(cache_path("real_rzp_base.json"), []), rzp_real_fresh, 'attempt_id')
    json.dump(cf_real, open(cache_path("real_cf_base.json"), 'w'))
    json.dump(pt_real, open(cache_path("real_pt_base.json"), 'w'))
    json.dump(rzp_real, open(cache_path("real_rzp_base.json"), 'w'))
    for r in cf_real: r['gateway'] = 'Cashfree'
    for r in pt_real: r['gateway'] = 'Paytm'
    for r in rzp_real: r['gateway'] = 'Razorpay'

    def mandate_cfpt_key(m):
        return f"{m['payer_key']}|{m['provider_account']}|{m['mandate_created_at']}"
    mandate_cfpt_map = {mandate_cfpt_key(m): m for m in load_json(cache_path("mandate_cfpt_base.json"), [])}
    for m in mandate_cfpt_fresh:
        mandate_cfpt_map[mandate_cfpt_key(m)] = m
    mandate_cfpt = list(mandate_cfpt_map.values())
    json.dump(mandate_cfpt, open(cache_path("mandate_cfpt_base.json"), 'w'))

    def mandate_rzp_key(m):
        return f"{m.get('payer_key')}|{m.get('created_epoch')}"
    rzp_map = {mandate_rzp_key(m): m for m in load_json(cache_path("mandate_rzp_a_base.json"), []) + load_json(cache_path("mandate_rzp_b_base.json"), [])}
    for m in mandate_rzp_a_fresh + mandate_rzp_b_fresh:
        rzp_map[mandate_rzp_key(m)] = m
    mandate_rzp_all = list(rzp_map.values())
    # keep the a/b base files as they were (informational split not needed going forward); persist combined into 'a' file, empty 'b'
    json.dump(mandate_rzp_all, open(cache_path("mandate_rzp_a_base.json"), 'w'))
    json.dump([], open(cache_path("mandate_rzp_b_base.json"), 'w'))

    log(f"cache sizes: cf_trial={len(cf_trial)} pt_trial={len(pt_trial)} rzp_trial={len(rzp_trial)} "
        f"cf_real={len(cf_real)} pt_real={len(pt_real)} rzp_real={len(rzp_real)} "
        f"mandate_cfpt={len(mandate_cfpt)} mandate_rzp={len(mandate_rzp_all)}")

    # ---------------- classification ----------------
    all_trial_attempts = cf_trial + pt_trial + rzp_trial
    for r in all_trial_attempts:
        r['paid_at_dt'] = parse_dt(r.get('paid_at'))
    successful_trials = [r for r in all_trial_attempts if r.get('status') in ('SUCCESS', 'captured', 'succeeded') and r.get('payer_key') and r['paid_at_dt']]

    reals = cf_real + pt_real + rzp_real
    for r in reals:
        r['paid_at_dt'] = parse_dt(r.get('paid_at'))
        r['payer_key'] = str(r['payer_key']) if r.get('payer_key') else None
    reals = [r for r in reals if r['payer_key'] and r['paid_at_dt']]

    real_by_payer_gw = defaultdict(list)
    for r in reals:
        real_by_payer_gw[(r['payer_key'], r['gateway'])].append((r['paid_at_dt'], r['amount']))
    for k in real_by_payer_gw:
        real_by_payer_gw[k].sort(key=lambda x: x[0])

    gw_to_provacct = {'Cashfree': 'cashfree_recurring', 'Paytm': 'paytm_recurring'}
    mandate_cfpt_idx = defaultdict(list)
    for m in mandate_cfpt:
        pk = str(m['payer_key'])
        dt = parse_dt(m.get('mandate_created_at'))
        if dt is None:
            continue
        plan_amt = m['max_amount_paise'] / 100.0 if m.get('max_amount_paise') is not None else None
        mandate_cfpt_idx[(pk, m['provider_account'])].append((dt, m['status'], plan_amt))
    for k in mandate_cfpt_idx:
        mandate_cfpt_idx[k].sort(key=lambda x: x[0])

    IST = timezone(timedelta(hours=5, minutes=30))
    mandate_rzp_idx = defaultdict(list)
    for m in mandate_rzp_all:
        pk = m.get('payer_key')
        ep = m.get('created_epoch')
        if not pk or ep is None:
            continue
        dt = datetime.fromtimestamp(ep, tz=timezone.utc).astimezone(IST).replace(tzinfo=None)
        plan_amt = m['expected_price_paise'] / 100.0 if m.get('expected_price_paise') is not None else None
        mandate_rzp_idx[pk].append((dt, m['status'], plan_amt))
    for k in mandate_rzp_idx:
        mandate_rzp_idx[k].sort(key=lambda x: x[0])

    def nearest_mandate(payer_key, gateway, ref_dt):
        if gateway == 'Razorpay':
            candidates = mandate_rzp_idx.get(payer_key, [])
        else:
            candidates = mandate_cfpt_idx.get((payer_key, gw_to_provacct[gateway]), [])
        if not candidates:
            return None, None, None
        return min(candidates, key=lambda c: abs((c[0] - ref_dt).total_seconds()))

    ACTIVE_LIKE = {'active', 'paused', 'authenticated', 'authorization_pending', 'created', 'pending', 'completed'}
    CANCELLED_LIKE = {'cancelled', 'revoked', 'expired', 'failed'}
    HALTED_LIKE = {'halted'}

    rows_out = []

    intent_agg = defaultdict(lambda: {'intent': 0, 'success': 0})
    for r in all_trial_attempts:
        dt = r.get('paid_at_dt')
        if not dt:
            continue
        d = dt.date().isoformat()
        if d not in TARGET_DATES:
            continue
        gw = r['gateway']
        intent_agg[(d, gw)]['intent'] += 1
        if r.get('status') in ('SUCCESS', 'captured', 'succeeded'):
            intent_agg[(d, gw)]['success'] += 1
    for (d, gw), v in intent_agg.items():
        if v['intent']:
            rows_out.append({'date': d, 'gateway': gw, 'amount': None, 'activity': 'Trial Intent', 'count': v['intent'], 'revenue': 0})
        if v['success']:
            rows_out.append({'date': d, 'gateway': gw, 'amount': 1, 'activity': 'Trial Successful', 'count': v['success'], 'revenue': v['success']})

    revenue_agg = defaultdict(lambda: {'cnt': 0, 'rev': 0.0})
    for (payer_key, gw), lst in real_by_payer_gw.items():
        for i, (paid_at, amount) in enumerate(lst, start=1):
            if i == 1:
                continue
            d = paid_at.date().isoformat()
            if d not in TARGET_DATES:
                continue
            key = (d, gw, norm(amount), 'Renewal')
            revenue_agg[key]['cnt'] += 1
            revenue_agg[key]['rev'] += amount

    trials_sorted = sorted(successful_trials, key=lambda t: t['paid_at_dt'])
    consumed_payments = set()
    mandates_cancelled_agg = defaultdict(lambda: {'cnt': 0})
    mandates_active_agg = defaultdict(lambda: {'cnt': 0})
    campaign_debit_success = []  # {payer_key, date, amount} — First Debit = Debit Success only, shared with the campaign sheet

    for t in trials_sorted:
        payer_key = str(t.get('payer_key')) if t.get('payer_key') else None
        gw = t['gateway']
        trial_dt = t.get('paid_at_dt')
        if not payer_key or not trial_dt:
            continue
        mandate_dt, mandate_status, plan_amt = nearest_mandate(payer_key, gw, trial_dt)
        anchor_dt = mandate_dt if mandate_dt else trial_dt
        ref_dt = mandate_dt if mandate_dt else trial_dt

        real_list = real_by_payer_gw.get((payer_key, gw), [])
        conversion = None
        for paid_at, amount in real_list:
            if paid_at >= ref_dt and (payer_key, gw, paid_at.isoformat()) not in consumed_payments:
                conversion = (paid_at, amount)
                consumed_payments.add((payer_key, gw, paid_at.isoformat()))
                break

        if conversion:
            pay_dt, pay_amt = conversion
            gap_days = (pay_dt - ref_dt).total_seconds() / 86400.0
            if gap_days <= 15:
                d = anchor_dt.date().isoformat()
                if d in TARGET_DATES:
                    key = (d, gw, norm(pay_amt), 'Debit Success')
                    revenue_agg[key]['cnt'] += 1
                    revenue_agg[key]['rev'] += pay_amt
                    campaign_debit_success.append({'payer_key': payer_key, 'date': d, 'amount': norm(pay_amt)})
            else:
                d = pay_dt.date().isoformat()
                if d in TARGET_DATES:
                    key = (d, gw, norm(pay_amt), 'Halted Reactivation')
                    revenue_agg[key]['cnt'] += 1
                    revenue_agg[key]['rev'] += pay_amt
        else:
            d = anchor_dt.date().isoformat()
            if d not in TARGET_DATES:
                continue
            amt_disp = norm(plan_amt) if plan_amt is not None else 1
            if mandate_status in CANCELLED_LIKE or mandate_status in HALTED_LIKE:
                mandates_cancelled_agg[(d, gw, amt_disp)]['cnt'] += 1
            elif mandate_status in ACTIVE_LIKE:
                mandates_active_agg[(d, gw, amt_disp)]['cnt'] += 1

    for (d, gw, amt, act), v in revenue_agg.items():
        rows_out.append({'date': d, 'gateway': gw, 'amount': amt, 'activity': act, 'count': v['cnt'], 'revenue': round(v['rev'], 2)})
    for (d, gw, amt), v in mandates_cancelled_agg.items():
        rows_out.append({'date': d, 'gateway': gw, 'amount': amt, 'activity': 'Mandates Cancelled', 'count': v['cnt'], 'revenue': None})
    for (d, gw, amt), v in mandates_active_agg.items():
        rows_out.append({'date': d, 'gateway': gw, 'amount': amt, 'activity': 'Mandates Active', 'count': v['cnt'], 'revenue': None})

    for m in meta_rows:
        d = str(m['date'])[:10]
        if d in TARGET_DATES:
            rows_out.append({'date': d, 'gateway': '', 'amount': None, 'activity': 'Meta Spends', 'count': round(m['spend_rs'], 2), 'revenue': 0})
    signup_by_day = defaultdict(int)
    for r in signup_rows:
        d = str(r['day'])[:10]
        if d in TARGET_DATES:
            signup_by_day[d] += 1
    for d, n in signup_by_day.items():
        rows_out.append({'date': d, 'gateway': '', 'amount': 0, 'activity': 'Sign Up', 'count': n, 'revenue': 0})

    msg_by_day = defaultdict(int)
    for r in msg_rows:
        d = str(r['day'])[:10]
        if d in TARGET_DATES:
            msg_by_day[d] += 1
    for d, n in msg_by_day.items():
        rows_out.append({'date': d, 'gateway': '', 'amount': 0, 'activity': '1st Message', 'count': n, 'revenue': 0})

    ACTIVITY_ORDER = ['Meta Spends', 'Sign Up', '1st Message', 'Trial Intent', 'Trial Successful',
                      'Debit Success', 'Halted Reactivation', 'Renewal', 'Mandates Cancelled', 'Mandates Active']
    rows_out.sort(key=lambda r: (r['date'], ACTIVITY_ORDER.index(r['activity']) if r['activity'] in ACTIVITY_ORDER else 99,
                                  r['gateway'], r['amount'] if r['amount'] is not None else -1))
    rows_out.sort(key=lambda r: r['date'], reverse=True)

    log(f"rows_out: {len(rows_out)}")

    # ---------------- campaign/ad-set attribution report (second sheet) ----------------
    log("building campaign attribution report")

    UNKNOWN_C, UNKNOWN_A = '(unattributed)', '(unattributed)'

    def key_for(date, c_id, c_name, a_id, a_name):
        return (date, c_id or UNKNOWN_C, c_name or UNKNOWN_C, a_id or UNKNOWN_A, a_name or UNKNOWN_A)

    ATTR_CACHE_PATH = os.path.join(CACHE, "attribution_cache.json")
    attr_cache = load_json(ATTR_CACHE_PATH, {})

    needed_keys = set()
    for r in all_trial_attempts:
        dt = r.get('paid_at_dt')
        if dt and dt.date().isoformat() in TARGET_DATES and r.get('payer_key'):
            needed_keys.add(str(r['payer_key']))
    for c in campaign_debit_success:
        needed_keys.add(c['payer_key'])
    attr_cache = fetch_attribution(needed_keys, attr_cache)
    json.dump(attr_cache, open(ATTR_CACHE_PATH, 'w'))
    log(f"attribution cache: {len(attr_cache)} total, {len(needed_keys)} needed this run")

    camp_agg = defaultdict(lambda: {'spend': 0.0, 'signups': 0, 'msgs': 0, 'trials_init': 0, 'trials_success': 0,
                                     'first_debit': defaultdict(int)})

    for r in camp_spend_rows:
        d = str(r['date'])[:10]
        if d not in TARGET_DATES:
            continue
        k = key_for(d, r['campaign_id'], r['campaign_name'], r['adset_id'], r['adset_name'])
        camp_agg[k]['spend'] += r['spend_rs'] or 0

    for r in signup_rows:
        d = str(r['day'])[:10]
        if d not in TARGET_DATES:
            continue
        cgid, cgname, aid, aname = parse_attr(r.get('attr'))
        k = key_for(d, cgid, cgname, aid, aname)
        camp_agg[k]['signups'] += 1

    for r in msg_rows:
        d = str(r['day'])[:10]
        if d not in TARGET_DATES:
            continue
        cgid, cgname, aid, aname = parse_attr(r.get('attr'))
        k = key_for(d, cgid, cgname, aid, aname)
        camp_agg[k]['msgs'] += 1

    for r in all_trial_attempts:
        dt = r.get('paid_at_dt')
        if not dt:
            continue
        d = dt.date().isoformat()
        if d not in TARGET_DATES:
            continue
        pk = str(r.get('payer_key')) if r.get('payer_key') else None
        cgid, cgname, aid, aname = parse_attr(attr_cache.get(pk) if pk else None)
        k = key_for(d, cgid, cgname, aid, aname)
        camp_agg[k]['trials_init'] += 1
        if r.get('status') in ('SUCCESS', 'captured', 'succeeded'):
            camp_agg[k]['trials_success'] += 1

    for c in campaign_debit_success:
        cgid, cgname, aid, aname = parse_attr(attr_cache.get(c['payer_key']))
        k = key_for(c['date'], cgid, cgname, aid, aname)
        camp_agg[k]['first_debit'][c['amount']] += 1

    log(f"camp_agg rows: {len(camp_agg)}")

    # ---------------- write XLSX ----------------
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Daily Funnel"
    headers = ['Date', 'Payment Gateway', 'Distinct Amount (Rs)', 'Activity', 'Meta Spend (Rs)', 'Revenue']
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"

    settling_cutoff = (today - timedelta(days=8)).isoformat()
    for r in rows_out:
        amt_disp = '' if r['amount'] is None else r['amount']
        rev_disp = '' if r['revenue'] is None else r['revenue']
        ws.append([to_ddmmyyyy(r['date']), r['gateway'], amt_disp, r['activity'], r['count'], rev_disp])

    widths = {1: 12, 2: 15, 3: 16, 4: 20, 5: 16, 6: 12}
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w

    ws2 = wb.create_sheet("Campaign Attribution")
    AMOUNT_COLS = [(999, 'Rs999'), (99, 'Rs99'), (299, 'Rs299'), (699, 'Rs699'),
                   (89, 'Rs89'), (29, 'Rs29'), (499, 'Rs499'), (98, 'Rs98')]
    headers2 = (['Date', 'Campaign Name', 'Campaign ID', 'Ad Set Name', 'Ad Set ID', 'Spend (Rs)',
                 'Signups', '1st Messages', 'Trials Initiated', 'Trials Successful', 'First Debit'] +
                [c[1] for c in AMOUNT_COLS] + ['Other Amount'])
    ws2.append(headers2)
    for cell in ws2[1]:
        cell.font = Font(bold=True)
    ws2.freeze_panes = "A2"

    for k in sorted(camp_agg.keys(), key=lambda x: x[0], reverse=True):
        date, c_id, c_name, a_id, a_name = k
        v = camp_agg[k]
        fd = v['first_debit']
        total_fd = sum(fd.values())
        known = sum(fd.get(amt, 0) for amt, _ in AMOUNT_COLS)
        row = [to_ddmmyyyy(date), c_name, c_id, a_name, a_id, round(v['spend'], 2),
               v['signups'], v['msgs'], v['trials_init'], v['trials_success'], total_fd]
        row += [fd.get(amt, 0) for amt, _ in AMOUNT_COLS]
        row.append(total_fd - known)
        ws2.append(row)

    # Campaign ID / Ad Set ID as text (@) to avoid Excel's float precision loss on 18-digit IDs
    for row in ws2.iter_rows(min_row=2, min_col=3, max_col=3):
        for cell in row:
            cell.number_format = '@'
            cell.alignment = Alignment(horizontal='right')
    for row in ws2.iter_rows(min_row=2, min_col=5, max_col=5):
        for cell in row:
            cell.number_format = '@'
            cell.alignment = Alignment(horizontal='right')

    widths2 = {1: 12, 2: 26, 3: 20, 4: 30, 5: 20, 6: 12, 7: 10, 8: 12, 9: 14, 10: 14, 11: 12}
    for col, w in widths2.items():
        ws2.column_dimensions[get_column_letter(col)].width = w
    for col in range(12, 12 + len(AMOUNT_COLS) + 1):
        ws2.column_dimensions[get_column_letter(col)].width = 9

    wb.save(OUT_PATH)
    log(f"wrote {OUT_PATH} ({len(rows_out)} gateway rows, {len(camp_agg)} campaign rows)")

    subprocess.run(['open', OUT_PATH])
    log("opened file")

    mark_success(today_iso)
    log("=== run success")


if __name__ == "__main__":
    try:
        main()
    except NETWORK_ERRORS as e:
        # Expected when VPN isn't connected yet — stay quiet, launchd will retry on its next interval.
        log(f"DB unreachable (VPN not connected?), will retry later: {e!r}")
    except Exception:
        err = traceback.format_exc()
        log("ERROR:\n" + err)
        try:
            subprocess.run(['osascript', '-e',
                             'display notification "Check run_log.txt in .daily_funnel_cache" with title "Daily Funnel Report failed"'])
        except Exception:
            pass
        raise
