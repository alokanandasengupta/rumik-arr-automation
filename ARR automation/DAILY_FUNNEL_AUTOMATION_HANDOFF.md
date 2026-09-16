# Rumik Ira — Daily Funnel & Campaign Attribution Report: Full Handoff

This is a self-contained handoff doc. Give this file to any LLM (or engineer) that has:
- Network/VPN access to the production Postgres DB via a Metabase instance, and
- A Metabase API key with query access to that DB (database id `2` in this setup — confirm via `/api/database`),

...and they should be able to reproduce, run, or extend this system from scratch, without any other context.

It documents three things:
1. The **business logic** — exactly what each report metric means and how it's computed.
2. The **automation** — a script + macOS launchd job that runs this daily, unattended, tolerant of VPN being down.
3. The **full source** — the actual working script, embedded below, ready to drop onto a fresh machine.

---

## 1. What gets produced

One Excel file, two sheets, covering a rolling **T-5..T** window (T = the day the job successfully runs):

**Sheet 1 — "Daily Funnel"**: `Date | Payment Gateway | Distinct Amount (Rs) | Activity | Meta Spend (Rs) | Revenue`
One row per (Date, Gateway, Amount, Activity) combination — the day-by-day funnel from ad spend through signup, trial, and revenue outcomes, split by payment gateway (Cashfree / Paytm / Razorpay).

**Sheet 2 — "Campaign Attribution"**: `Date | Campaign Name | Campaign ID | Ad Set Name | Ad Set ID | Spend (Rs) | Signups | 1st Messages | Trials Initiated | Trials Successful | First Debit | Rs999 | Rs99 | Rs299 | Rs699 | Rs89 | Rs29 | Rs499 | Rs98 | Other Amount`
Same window, broken down by Meta ad campaign/ad-set instead of gateway. **First Debit is computed from the exact same classification pass as Sheet 1's "Debit Success"** — they are guaranteed to sum to the same total per date, by construction (not by reconciliation after the fact).

Output path: `~/Desktop/Daily Funnel Report.xlsx` (overwritten each successful run — it's always "today's view" of the last 6 days).

---

## 2. Prerequisites

- A `sync_arr.py` (or equivalent) module in the same directory as the job script, exposing:
  - `MB_URL` — Metabase base URL (e.g. `https://metabase.prod.rumik.ai`)
  - `MB_KEY` — Metabase API key (`X-API-Key` header)
  - `MB_DATABASE_ID` — the Postgres database's id within Metabase (this setup uses `2`)
  - `SSL_CONTEXT` — an `ssl.SSLContext` (built via `certifi.where()` if available, else default)
  - These are loaded from a `.env` file (`MB_URL=...`, `MB_KEY=...`) sitting next to the script, via a tiny inline dotenv loader (`os.environ.setdefault` per line — see `sync_arr.py` for the exact ~15-line implementation, trivial to reproduce if starting fresh).
- Python 3.10+, with `openpyxl` installed (`pip install openpyxl`).
- macOS with `launchd` (for the scheduling half) and `osascript` (for failure notifications) — if deploying on Linux, swap `launchd` for `cron`/`systemd timer` and `osascript` for whatever notification mechanism is available; the Python script itself is platform-agnostic.
- The DB is only reachable over VPN. The automation is explicitly designed around this constraint (see §4).

**Metabase query pattern used throughout** (all queries in this system go through this):
```python
def mb_query(sql, timeout=400):
    import urllib.request, json
    payload = json.dumps({"database": MB_DATABASE_ID, "type": "native", "native": {"query": sql}}).encode()
    req = urllib.request.Request(f"{MB_URL}/api/dataset", data=payload, method="POST",
        headers={"X-API-Key": MB_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        body = json.loads(resp.read())
    if "data" not in body:
        raise RuntimeError(f"Metabase query error: {body.get('error', body)}\nSQL:\n{sql}")
    cols = [c["name"] for c in body["data"]["cols"]]
    return [dict(zip(cols, row)) for row in body["data"]["rows"]]
```

**Critical gotcha**: Metabase's `/api/dataset` endpoint **silently truncates any query at 2000 rows** — no error, no warning, just a short result. Any query that could return more than 2000 rows MUST be paginated:
```python
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
```
This bug has bitten this project multiple times (signup counts silently capped at exactly 2000, a churn-event export silently capped at exactly 2000 rows). Always paginate; always sanity-check for a suspiciously round row count.

**Second gotcha**: Metabase serializes `timestamptz` columns as full ISO8601 (`2026-08-25T00:00:00+05:30`), but if you `::text`-cast a `timestamp without time zone` value (e.g. after `at time zone 'Asia/Kolkata'`) Postgres renders it space-separated with variable-length fractional seconds (`2026-08-17 14:03:04.77906` — 5 digits, not 6). A naive `datetime.fromisoformat()` will throw on that. Use a tolerant parser (see `parse_dt` in the script below) that pads fractional seconds to 6 digits regardless of whether a timezone suffix is present.

---

## 3. Business logic

### 3.1 The 10 gateway-level activity types (Sheet 1)

| # | Activity | Date basis | Gateway/Amount shown | Revenue |
|---|---|---|---|---|
| 1 | Meta Spends | Ad spend day | blank / blank | 0 (value is in the Count column) |
| 2 | Sign Up | Signup day | blank / 0 | 0 |
| 3 | 1st Message | First-message day | blank / 0 | 0 |
| 4 | Trial Intent | Trial attempt day | per gateway / blank | 0 |
| 5 | Trial Successful | Trial payment day | per gateway / 1 | count × 1 |
| 6 | **Debit Success** | **Mandate creation date** | per gateway / actual amount | count × amount |
| 7 | **Halted Reactivation** | **Payment date** | per gateway / actual amount | count × amount |
| 8 | Renewal | Payment date | per gateway / actual amount | count × amount |
| 9 | Mandates Cancelled | Mandate creation date | per gateway / plan amount | blank |
| 10 | Mandates Active | Mandate creation date | per gateway / plan amount | blank |

**The mixed date basis is intentional**: Trial Intent/Successful use the trial event's own date. Debit Success and the two Mandate-status rows use the **mandate's creation date** (a cohort view — "what happened to mandates created on day X"). Halted Reactivation and Renewal use the actual **payment date** (a revenue view — "what money came in on day X").

### 3.2 Meta Spends / Sign Up / 1st Message

- **Meta Spends**: `meta_ads_daily` is a document store — `document->campaigns[]->adsets[]->ads[]`, each level carrying its own `spend_paise`. Sum `ads[].spend_paise` (or `adsets[].spend_paise` directly — verified identical, ad-set level is pre-aggregated) per day.
- **Sign Up**: count of `users` by `(created_at at time zone 'Asia/Kolkata')::date`.
- **1st Message**: for each user, `min(messages.timestamp) where role='user'`, grouped by that first-message date. Because a user's first message can lag their signup by up to ~2 weeks, the candidate pool must be widened backward (this system uses signup date ≥ target_start − 14 days) — `messages` has no usable index for a pure date-range scan on hundreds of millions of rows, so this signup-window trick is required for performance.
- **Caveat**: `meta_ads_daily` keeps backfilling for several days after the fact — treat the last 7-10 days as provisional in every report.

### 3.3 Trial Intent / Trial Successful

Every ₹1 payment attempt, per gateway, per day (of the trial's own timestamp).

**Critical fix (do not use `payment_attempts` for CF/PT trial detection)**: the ₹1 trial/authorization event for Cashfree and Paytm does **not** reliably appear in `payment_attempts` — confirmed as low as 0 rows for Cashfree over a full month where the true count was in the hundreds/day. Use the raw gateway webhook mirrors instead:

- **Cashfree**: `cashfree_payments` (document store). Filter `(document->>'amount_paise')::numeric = 100`. Status: `document->>'payment_status'` ∈ `SUCCESS`/`FAILED`/`CANCELLED`. Date: `document->>'payment_time'`.
- **Paytm**: `paytm_payments` (same shape). Same amount filter. Status: `SUCCESS`/`FAILED`. `document->>'user_id'` is already a proper `users.id` UUID — no identity resolution needed.
- **Razorpay**: `razorpay_payments`, `(entity_data->>'amount')::numeric = 100`, status `captured` = successful. This table already is the raw store — unaffected by the CF/PT bug above.

Trial Intent = count of all rows (any status) that day/gateway. Trial Successful = count filtered to success/captured; revenue = that count × ₹1.

**Payer identity resolution** (needed for the classification algorithm in §3.4, not for the Intent/Successful counts themselves):
- **Cashfree**: no clean `user_id` on most rows. Resolve via `document->>'customer_email'` → `users.email`, falling back to `document->>'customer_phone'` (last-10-digits normalized, excluding placeholder `'9999999999'`) → `users.phone_number`. Both paths combined get ~100% match.
- **Paytm**: `document->>'user_id'` directly.
- **Razorpay**: regex `^user-([0-9a-fA-F-]{36})@noemail\.rumik\.ai$` on `entity_data->>'email'`, falling back to a real-email join against `users.email`.

### 3.4 Debit Success / Halted Reactivation / Renewal — the classification algorithm

**Real (>₹1) payment sources & payer identity** (unrelated to the CF/PT trial-detection bug — `payment_attempts` is reliable for real payments):
- **Cashfree**: `payment_attempts` (succeeded, `amount_paise != 100`, `provider_account = 'cashfree_recurring'`) → `invoices.charge_id` → `invoices.user_id`.
- **Paytm**: `payment_attempts` (succeeded, `provider_account = 'paytm_recurring'`) → `metadata->>'paytm.subscription_id'` → `paytm_subscriptions._id` → `document->>'user_id'`. (Do **not** use `paytm_billing_plans.last_attempt_id` — 8.6% match rate, broken.)
- **Razorpay**: `razorpay_payments` (captured, `amount != 100`) → same email regex / real-email fallback as trials.

**Mandate / plan-amount lookup**:
- **Cashfree/Paytm**: `mandates` table, joined on `user_id` + `provider_account`. `max_amount_paise` = plan amount, `status` = current state, `created_at` = mandate creation timestamp.
- **Razorpay**: `razorpay_subscriptions`, matched via **both** the synthetic-email regex on `entity_data->>'customer_email'` and a real-email fallback to `users.email` (do **not** use `mandates` for Razorpay — it only covers a small "Charge At Will" side-product). Plan amount = `(entity_data->'notes'->>'expected_price_paise')` (~98% coverage — far more reliable than inferring from `plan_id`, since one `plan_id` maps to many actual prices due to regional/discount pricing).
- When a payer has multiple mandate/subscription records for the same gateway (e.g. a retried setup), match each event to the **nearest-by-time** mandate record.

**Algorithm** (rank real payments per payer+gateway):
1. Build the full set of successful real (≠₹1) payments, with `payer_key` resolved per gateway.
2. Process every successful ₹1 trial **chronologically** (oldest first), per (payer, gateway):
   - Find the trial's nearest mandate/subscription record → its `created_at` and current `status`.
   - Look for the payer's earliest real payment with `paid_at >= mandate_created_at` that **hasn't already been claimed by an earlier trial from the same payer**.
   - If found: `gap_days = paid_at − mandate_created_at`.
     - `gap_days <= 15` → **Debit Success**, dated by **mandate_created_at**.
     - `gap_days > 15` → **Halted Reactivation**, dated by the **payment's own date**.
   - If not found: payer hasn't converted yet.
     - Mandate status ∈ {cancelled, revoked, expired, failed, halted} → **Mandates Cancelled**, dated by mandate creation, amount = plan amount.
     - Mandate status ∈ {active, paused, authenticated, authorization_pending, created, pending, completed} → **Mandates Active**, same dating.
     - No mandate found at all → excluded (small residual, ~3-5% of trials, mostly unresolved Razorpay identities).
3. **Retry-dedup (critical)**: mark each real payment "consumed" once matched to a trial. If a payer retries the ₹1 trial multiple times (each creating a new mandate attempt), only the **earliest** trial/mandate that actually produced a real payment gets credited — later retries, finding no unconsumed payment, correctly fall into Mandates Cancelled/Active instead. Without this, one real payment gets claimed by every retry independently, inflating conversion counts.
4. **Renewal**: separately, rank all real payments per (payer, gateway) by `paid_at` ascending. Every payment beyond the first (`rn > 1`) is a Renewal, dated by its own payment date. Ranking is **per-gateway**, not combined across a payer's gateways.

**Entire Revenue = Trial Successful + Debit Success + Halted Reactivation + Renewal.**

### 3.5 Mandates Cancelled / Mandates Active

Non-conversion outcomes from step 2 above. Both dated by mandate creation date, amount = plan amount (not ₹1). **These are current-status snapshots, not permanent** — a mandate created N days ago showing "Active" today may show "Cancelled" tomorrow. Any rerun produces different numbers for the same historical date because status is evaluated as-of-now, not as-of-that-date. Expected, not a bug.

### 3.6 Campaign Attribution sheet (Sheet 2)

Per-(date, campaign, ad-set) breakdown of Spend, Signups, 1st Messages, Trials Initiated/Successful, and **First Debit** (= Debit Success only from §3.4 — deliberately excludes Halted Reactivation; this was an explicit business decision after reconciling against a hand-maintained master sheet, where the "First Debit" column tracked Debit Success alone).

**Attribution source**: `user_profiles.meta_install_attribution` (jsonb), keyed by `user_id`. Field-name mapping is counter-intuitive — Meta's own naming is swapped from the human-facing hierarchy:
- `campaignGroupId` / `campaignGroupName` = the actual Meta **Campaign**.
- `campaignId` / `campaignName` = the actual Meta **Ad Set**.

Rows with no attribution (organic, or attribution missing) are bucketed as `(unattributed)` for both campaign and ad-set fields, rather than dropped.

**Campaign/ad-set spend** comes from the same `meta_ads_daily` structure as §3.2, but grouped by `campaign->>'id'`/`'name'`, `adset->>'id'`/`'name'` instead of summed to a daily total.

**The sync guarantee**: First Debit is populated from the *same* classification loop that produces Sheet 1's Debit Success (§3.4 step 2, `gap_days <= 15` branch) — each such event is tagged with its payer's attribution and bucketed here, in addition to being counted in Sheet 1. They are not two separate computations reconciled after the fact; they share one source of truth by construction. Verify with:
```python
# sheet1 Debit Success total per date == sheet2 First Debit total per date, always, exactly
```

**Campaign ID / Ad Set ID formatting**: Meta campaign/ad-set IDs are 15-18 digit integers. Excel/openpyxl stores numbers as IEEE 754 doubles, which lose precision beyond ~15-16 significant digits (empirically verified: `120249263503470043` → `120249263503470048` after a float round-trip). **Write these as text cells with `number_format = '@'`** (and right-align for a "looks like a number" appearance) — never as true numeric cells.

### 3.7 Known caveats (report these every time)

1. **Settling lag**: any date within the last ~7-10 days is provisional. `meta_ads_daily`, payment records, and mandate statuses keep changing for several days after the fact.
2. Metabase's 2000-row truncation (§2) — always paginate.
3. ~3-5% of trials/payments never resolve to a user — mostly Razorpay `entity_data->>'email'` matching neither the synthetic pattern nor a real `users.email` row. Silently excluded from every count, not shown as "Unresolved."
4. Cashfree launched ~2026-07-23, Paytm ~2026-08-10 — zero data before those dates is real business history, not a query gap.
5. Real anomalies will show up that are NOT bugs (e.g. a specific date where a large fraction of one gateway's mandates go `halted` almost instantly after trial, confirmed via manual mandate-timestamp inspection) — call these out explicitly rather than assuming pipeline error.

---

## 4. Automation design

### 4.1 The constraint that shapes everything

The DB is only reachable over VPN, and VPN isn't necessarily connected the moment the laptop wakes each morning. So this cannot be a simple "run once at 8am" cron job — it needs to be **time-insensitive**: keep trying periodically, no-op quietly when it can't reach the DB, and do the real work the first time after midnight that (a) it hasn't already succeeded today and (b) the DB happens to be reachable.

### 4.2 How that's implemented

- **launchd `StartInterval` = 1800** (every 30 minutes), all day, every day — not a single fixed time. `RunAtLoad = true` so it also tries immediately when the job is (re)loaded.
- **State file** (`last_success_date.txt`) tracks the ISO date of the last successful run. First thing `main()` does: if that equals today, return immediately — quiet no-op.
- **Network-error tolerance**: DB calls that fail with a timeout/connection error are caught specifically (`NETWORK_ERRORS` tuple) and logged as "will retry later" — the process exits **cleanly (code 0)**, not as a crash. This is a deliberate but important gotcha for whoever operates this: **a clean exit code does not mean the report was produced** — always check the log content / state file, not just the exit code, to confirm actual success.
- **Genuine failures** (a real bug, not a network hiccup) still raise, get logged with a full traceback, and trigger a macOS notification (`osascript display notification`) so a silent break doesn't go unnoticed for days.
- **Durable incremental cache** (`.daily_funnel_cache/*_base.json`): rather than re-pulling all of history every run, the job keeps a durable local cache of trials/real-payments/mandates and only re-queries a rolling **25-day refresh window** each run (covers the 15-day mandate→payment gap threshold plus settling-lag buffer), merging fresh rows into the cache by ID (`merge_by_id` — fresh overrides old on matching ID, never deletes). The very first run needs to be seeded with a genuine full-history pull (see §4.4) — after that, every run is fast.
- **Attribution cache** (`.daily_funnel_cache/attribution_cache.json`): grows over time too, avoiding repeat `user_profiles` lookups for returning payers.

### 4.3 File layout

```
ARR automation/
  daily_funnel_job.py                    — the job (full source in §5)
  sync_arr.py                            — Metabase credentials/query helper (pre-existing in this repo)
  .env                                   — MB_URL, MB_KEY (not committed)
  .daily_funnel_cache/
    trials_cf_base.json                  — durable cache, Cashfree ₹1 trial attempts
    trials_pt_base.json                  — durable cache, Paytm ₹1 trial attempts
    trials_rzp_base.json                 — durable cache, Razorpay ₹1 trial attempts
    real_cf_base.json                    — durable cache, Cashfree real (>₹1) payments
    real_pt_base.json                    — durable cache, Paytm real payments
    real_rzp_base.json                   — durable cache, Razorpay real payments
    mandate_cfpt_base.json               — durable cache, Cashfree+Paytm mandates
    mandate_rzp_a_base.json              — durable cache, Razorpay subscriptions (merged; a "_b" companion file exists for historical reasons but is kept empty going forward)
    mandate_rzp_b_base.json
    attribution_cache.json               — durable cache, user_id → meta_install_attribution
    last_success_date.txt                — state: ISO date of last successful run
    run_log.txt                          — append-only log, every run attempt
    launchd_stdout.log / launchd_stderr.log — launchd's own redirect targets (should stay empty; real logging goes to run_log.txt)

~/Library/LaunchAgents/
  com.rumik.dailyfunnel.plist            — the scheduler (full content in §6)

~/Desktop/
  Daily Funnel Report.xlsx               — the output, overwritten each successful run
```

### 4.4 Bootstrapping on a fresh machine (no existing cache)

The job's `merge_by_id` logic treats a missing `*_base.json` file as an empty list (`load_json(path, [])`), so it will technically run with no seed data — but the very first run's "25-day refresh window" pull is **not** a substitute for full history: the classification algorithm (§3.4) needs the payer's *entire* payment history to correctly identify "is this their first real payment or a renewal" and to correctly apply the retry-dedup rule. Running cold will produce wrong Renewal/Debit-Success numbers for any payer whose relevant history falls outside the 25-day window.

**To bootstrap correctly**: before the first scheduled run, do one manual full-history pull (from business launch date — Jan 2026 in this dataset, or whenever gateways actually went live per §3.7 point 4 — through today) for each of the 8 queries in §5 (trials × 3 gateways, real payments × 3 gateways, mandates × 2 gateway-groups), save each as the corresponding `*_base.json` in `.daily_funnel_cache/`, *then* start the scheduled job. This is a one-time cost; every run after that is incremental. (In this deployment, the seed was seeded from JSON files already produced during earlier ad-hoc analysis sessions that had already done a full-history pull and been validated against a hand-maintained reconciliation sheet at 29/32 exact match — cheaper than re-pulling from scratch. If starting genuinely fresh, budget real time for this step: the Razorpay trial-attempts table alone is ~140k+ rows over ~8 months of history.)

---

## 5. Full source: `daily_funnel_job.py`

```python
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
```

---

## 6. Scheduling: `com.rumik.dailyfunnel.plist`

Path: `~/Library/LaunchAgents/com.rumik.dailyfunnel.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rumik.dailyfunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>/Users/alokananda/Desktop/Rumik_on/ARR automation/daily_funnel_job.py</string>
    </array>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/alokananda/Desktop/Rumik_on/ARR automation/.daily_funnel_cache/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/alokananda/Desktop/Rumik_on/ARR automation/.daily_funnel_cache/launchd_stderr.log</string>
</dict>
</plist>
```

**Activate**: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rumik.dailyfunnel.plist`
**Deactivate**: `launchctl bootout gui/$(id -u)/com.rumik.dailyfunnel`
**Check status**: `launchctl list | grep rumik`
**Run once manually** (bypassing the schedule, useful for testing): `cd "ARR automation" && python3 daily_funnel_job.py`

If porting to Linux: replace with a `systemd` timer (`OnUnitActiveSec=30min`) or `cron` entry running every 30 minutes (`*/30 * * * *`), and replace the `osascript` notification call with whatever's available (email, Slack webhook, etc.) — the core Python script needs no changes.

---

## 7. Operating this system — checklist for a fresh agent

1. Confirm `sync_arr.py` + `.env` are present and `MB_KEY` is valid (`mb_query("select 1")` should return `[{'?column?': 1}]` or similar).
2. Confirm `.daily_funnel_cache/*_base.json` exist and are non-trivial in size (if starting fresh, do the one-time full-history bootstrap per §4.4 first).
3. Run `python3 daily_funnel_job.py` manually once, watch `run_log.txt` — expect it to walk through: meta/signup/msg → campaign spend → trial attempts → real payments → mandates → merge → classification → campaign report → XLSX write → open. A clean run typically takes 3-15 minutes depending on DB/VPN latency (this has been observed to vary significantly run-to-run — don't treat a slow run as broken unless it actually times out or errors).
4. Verify the sync guarantee (§3.6) programmatically before trusting output:
   ```python
   import openpyxl
   from collections import defaultdict
   wb = openpyxl.load_workbook(os.path.expanduser("~/Desktop/Daily Funnel Report.xlsx"))
   gw_debit = defaultdict(int)
   for row in wb['Daily Funnel'].iter_rows(min_row=2, values_only=True):
       date, gateway, amt, activity, count, revenue = row
       if activity == 'Debit Success':
           gw_debit[date] += count
   camp_debit = defaultdict(int)
   ws2 = wb['Campaign Attribution']
   fd_idx = [c.value for c in ws2[1]].index('First Debit')
   for row in ws2.iter_rows(min_row=2, values_only=True):
       camp_debit[row[0]] += row[fd_idx]
   assert gw_debit == camp_debit  # or diff and report any mismatch — should never happen by construction
   ```
5. Install/activate the launchd job (§6) once satisfied.
6. **Remember**: exit code 0 ≠ success. Always check `run_log.txt`'s last line for `=== run success` and/or `last_success_date.txt` matching today's date before reporting the job as done. A clean exit can also mean "backed off from a VPN timeout, will retry" (§4.2).
7. If unloading launchd temporarily for manual testing (to avoid two concurrent processes writing the cache at once), remember to reactivate it afterward: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rumik.dailyfunnel.plist`.

---

## 8. Extending this system

This same `subscription_history` / `subscriptions` table pair (discovered while investigating an ARR-drop/churn question, not currently wired into the daily job) is the canonical, gateway-agnostic status-transition log for Razorpay-web + mobile IAP subscriptions — far more reliable for true churn analysis than reconstructing status from `mandates`/`razorpay_subscriptions` current-state snapshots, because it records the actual *event* and *previous_status → new_status* transition with a timestamp, not just current state. Key facts if building a churn report on top of this doc's foundation:
- `subscriptions.platform = 'web_razorpay'` covers the overwhelming majority of volume; Cashfree/Paytm mandates have no equivalent transition-history table (only `mandates.status`, a current-state snapshot) and are not included in `subscription_history`.
- `subscriptions.billing_cycle` is almost universally `'monthly'` — `price × 12` gives ARR-per-subscriber.
- A genuine "churn event" = a `subscription_history` row where `new_status in ('cancelled','expired')` and `previous_status not in ('cancelled','expired', null)` — i.e. the first time a subscription leaves an active-influencing state.
- `subscription_history.metadata->>'reason'` gives a coarse reason tag (`razorpay_cancelled`, `user_initiated`, `replaced_by_new_subscription`) but nothing more granular (no exit-survey data was found in this schema) — most cancellations are just tagged `razorpay_cancelled`, a system tag not a business reason.
- Watch for **same-day mandate-creation-to-cancellation** as a distinct phenomenon from renewal-failure churn — they have very different implications (trial quality/UX issue vs. billing/retention issue) and should be reported separately, not blended into one "churn" number.
