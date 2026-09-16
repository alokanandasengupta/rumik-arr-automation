# IRA ARR.xlsx — Fill Logic

Source: `metabase.prod.rumik.ai`, database id `2` (prodDB), role `metabase_ro` (read-only).
Timezone: all bucketing is in `Asia/Kolkata` (IST), matching how timestamps are stored (`timestamptz`, `+05:30`) and Metabase's `results_timezone`.

The data currently sitting in the workbook (through 2026-09-04) is confirmed **placeholder**, not real historical output — safe to overwrite entirely rather than reconcile against.

---

## 1. Payment Gateway

The 3 gateways (`Cashfree`, `Paytm`, `Razorpay`) are **not** `subscriptions.platform` (which only holds `web_razorpay` / `apple_ios` / `google_play_android`). They come from the UPI-Autopay mandate layer:

| Sheet label | `mandates.provider_account` |
|---|---|
| Cashfree | `cashfree_recurring` |
| Paytm | `paytm_recurring` |
| Razorpay | `razorpay_caw_recurring` |

A user's gateway = the `provider_account` of their **most recent mandate** (`max(created_at)` per `user_id`), regardless of that mandate's current status.

```sql
select distinct on (user_id) user_id, provider_account, status, created_at, updated_at
from mandates
order by user_id, created_at desc
```

## 2. Active Subscriber

A user counts as an active subscriber for gateway G if their latest mandate (above) has `provider_account = G` and `status` is in the **active set**:

```
ACTIVE_SET = {'active', 'created', 'paused', 'expired'}
EXCLUDED    = {'authorization_pending', 'revoked', 'failed'}
```

- `authorization_pending` is excluded — it means UPI mandate setup was started but never approved in the user's banking app (this is Cashfree's dominant status by volume; including it would make Cashfree look ~10x larger than Razorpay, which doesn't match reality).
- `created` and `expired` are tiny (905 and 5 rows total, respectively) — included in the active set per literal instruction, but flagged since their real-world meaning is fuzzy. Immaterial to totals either way.

`Active Subscribers (Trailing 30d / Mandate-based)` = `count(distinct user_id)` per gateway from this filtered latest-mandate set, evaluated as of the report date/time.

## 3. Avg MRR per Subscriber (the AOV)

Per your instruction: **average payment amount, trailing 30 days, of people currently holding an active mandate, excluding ₹1 payments** (₹1 = UPI mandate authorization/verification debits, not real revenue).

This single average is applied uniformly to every active subscriber of that gateway — it is *not* each subscriber's individual price. That matches the existing workbook structure (`MRR (Rs) == MRR Calculated`, and `MRR = Avg MRR per Subscriber × Active Subscribers` reconciles exactly on every row I checked).

Each gateway's payments table links to a user differently:

| Gateway | Payments table | Link to user |
|---|---|---|
| Cashfree | `cashfree_payments` | `document->>subscription_id` → `cashfree_subscriptions.document->>subscription_id` → mandate via `cashfree_subscriptions.document->>'raw'->'notes'->>'user_id'` (or via `mandates` on `cf_subscription_id`/user match — needs a quick confirm query before I wire this up) |
| Paytm | `paytm_payments` | `document->>user_id` directly |
| Razorpay | `razorpay_caw_payments` | `document->'notes'->>'user_id'` directly |

```sql
-- example for one gateway (Paytm), date D, trailing 30d window
with active_users as (
  select distinct on (user_id) user_id
  from mandates
  where provider_account = 'paytm_recurring'
    and status in ('active','created','paused','expired')
  order by user_id, created_at desc
)
select avg((document->>'amount_paise')::numeric / 100.0) as avg_mrr_per_subscriber
from paytm_payments p
join active_users u on u.user_id = (p.document->>'user_id')
where (p.document->>'payment_status') = 'captured'   -- confirm exact success status string per gateway
  and (p.document->>'amount_paise')::numeric != 100   -- exclude ₹1
  and (p.document->>'payment_time')::timestamptz between :D - interval '30 days' and :D
```

**Flagged for confirmation before I implement this:**
- Exact "successful payment" status string per gateway (`captured` for Razorpay/Cashfree-style gateways is a guess — need to check `payment_status`/`STATUS` distinct values per table).
- Cashfree's join path (subscription_id → user) needs one more verification query.

## 4. MRR / ARR / USD

```
MRR (Rs)        = Avg MRR per Subscriber × Active Subscribers          (per gateway, per date)
MRR Calculated  = same formula, computed independently as a cross-check — should equal MRR (Rs)
ARR (Rs)        = MRR (Rs) × 12
MRR (USD)       = MRR (Rs) / 94.54
ARR (USD)       = ARR (Rs) / 94.54
```

FX rate `94.54` is a fixed constant (back-calculated from the placeholder data, confirmed by you) — there is no FX table in the DB.

## 5. Day-over-day deltas

```
Net MRR Change        = MRR(t) − MRR(t−1)                 (NOT New − Churned — verified independently on placeholder data)
Net Subscriber Change = Active Subscribers(t) − Active Subscribers(t−1)
```

## 6. New MRR Added / MRR Churned

There's no mandate status-change history table (unlike `subscription_history`, which tracks `previous_status`/`new_status` explicitly) — only `mandates.updated_at`, confirmed usable as the change timestamp.

Because we can't see *what a mandate transitioned from*, the best reproducible proxy is:

```
New MRR Added (day D, gateway G)
  = count(mandates where provider_account = G, status in ACTIVE_SET, created_at::date = D)
    × Avg MRR per Subscriber(G, D)
  -- i.e. brand-new mandates created and already active/counted that day

MRR Churned (day D, gateway G)
  = count(mandates where provider_account = G, status in EXCLUDED, updated_at::date = D, created_at::date < D)
    × Avg MRR per Subscriber(G, D−1)
  -- i.e. a pre-existing mandate that flipped out of the active set that day, valued at the prior day's rate
```

This is an approximation (a mandate that flips active→paused→active within one day, for instance, won't be seen precisely). Flagging this as the one piece of logic most likely to need adjustment once you eyeball the output.

---

## Per-sheet application

### Sheet 1 (daily, one row per date × gateway)
All columns as defined in sections 1–6 above, computed as of end-of-day IST for each date.

### Intraday10min (10-min buckets, per gateway, current/recent days)
| Column | Logic |
|---|---|
| Payments in Bucket | count of captured payments for that gateway in the 10-min window |
| New Distinct Payers | `count(distinct user_id)` of payers in that bucket — **ASSUMPTION**: "new" here means distinct-within-bucket, not lifetime-first-ever; flag if you meant the latter |
| Revenue in Bucket (Rs) | sum of captured payment amounts in that gateway/bucket |
| Cumulative Revenue Today (Rs) | running sum of Revenue in Bucket since IST midnight |
| Active Subscribers (Running) | same mandate-based active count, evaluated live at bucket timestamp |
| Avg MRR per Subscriber / MRR / ARR / USD cols | same formulas as daily sheet, evaluated at that timestamp |

### Minute3Gateway (1-min buckets, all 3 gateways combined — no gateway column)
Same as Intraday10min but summed across all gateways, plus:
| Column | Logic |
|---|---|
| Churned This Minute | mandates (any gateway) with `created_at::date < today`, `status` flips into `EXCLUDED`, `updated_at` in that minute |
| Resumed This Minute | mandates (any gateway) with `created_at::date < today`, `status` flips into `ACTIVE_SET`, `updated_at` in that minute — **ASSUMPTION**: this sheet has no separate "New" counter, so brand-new same-minute activations would also land here unless you want them excluded; flag if so |

---

## Resolved — final validated join paths

The unified, normalized `payment_attempts` table (not the raw per-gateway webhook mirrors) turned out to be the right source for actual payment facts — `status`, `amount_paise`, `provider_account`, `completed_at` are all clean there. It links to a user differently per gateway:

| Gateway | `payment_attempts` → user |
|---|---|
| Cashfree | `payment_attempts.charge_id = invoices.charge_id` → `invoices.user_id` |
| Paytm | `payment_attempts.id = paytm_billing_plans.last_attempt_id` → `paytm_billing_plans.user_id` / `.mandate_id` |
| Razorpay | `payment_attempts.id = razorpay_caw_billing_plans.last_attempt_id` → `razorpay_caw_billing_plans.user_id` / `.mandate_id` |

(The raw per-gateway tables' embedded `user_id`/`mandate_id` fields — e.g. `cashfree_subscriptions.document->raw->notes->user_id`, `razorpay_caw_payments.document->notes->user_id` — are a **legacy Mongo ObjectId scheme**, not the canonical Postgres UUID. Don't join on those. Cashfree does have one clean canonical-space link though: `mandates.metadata->>'cfSubscriptionId' = cashfree_payments.document->>'cf_subscription_id'`.)

"Payment succeeded" status per gateway, confirmed:
- Cashfree: `document->>'payment_status' = 'SUCCESS'`
- Paytm: `document->>'payment_status' = 'SUCCESS'`
- Razorpay CAW: `document->>'status' = 'captured'`
- Unified (`payment_attempts.status`): `'succeeded'`

Reconciled active-subscriber counts (current, all definitions per your answers): **Cashfree 1,068 / Paytm 1,941 / Razorpay 857** (as of 2026-09-04). This is a very different shape than the placeholder data (which had Razorpay dominant) — confirmed expected, since the placeholder was synthetic.

**Caveat**: Razorpay's trailing-30d AOV is currently computed from only ~4 successful payments — expect that number to be noisy/volatile day to day until more real volume accumulates on that rail.

**Remaining semantic assumption** (not yet re-confirmed, low-stakes): "New Distinct Payers" = distinct payers within that bucket, and "Resumed This Minute" = any pre-existing mandate (not brand-new) flipping back into the active set, which may double-count with a brand-new same-minute activation since Minute3Gateway has no separate "New" counter. See `sync_arr.py` for the implementation.

## Implementation

See `sync_arr.py` in this folder for the full working implementation (tested against live Metabase data).

## Resolved — the "second writer" data-corruption mystery (2026-09-05)

For a long stretch, Minute3Gateway periodically showed a second, conflicting "regime" of numbers
(active subscribers jumping to a different value than what this script had just computed, skewing
MRR/ARR along with it) that didn't match any formula derivable from Metabase. It was not an
external editor and not a bug in this script's own math.

Root cause: an earlier, abandoned first attempt at this automation — three separate scripts at
`~/.ira_arr/scripts/` (`minute_sync_cycle.py`, `intraday_sync_cycle.py`, `daily_sync_cycle.py`)
writing via the Apps Script Web App (`gas_post.sh`) — had been loaded into `launchd` as
`com.iraarr.minutesync` (60s), `com.iraarr.intradaysync` (10min), and `com.iraarr.dailysync`
(hourly). When the project moved to this single `sync_arr.py` + direct-Sheets-API design, those
plist files were deleted, but `launchd` keeps a *loaded* job running until it's explicitly
unloaded — so all three kept firing against the exact same "Minute3Gateway" tab the entire time
(561/73/13 runs respectively by the time this was found), each cycle racing this script to write
the same minute's row with an incompatible active-subscriber computation.

Fixed by unloading all three (`launchctl bootout gui/<uid>/com.iraarr.<name>` for each). As
defense in depth against any future competing writer, `sync_arr.py` also gained:
- `dedupe_minute_rows()` — after each cycle, deletes any row sharing our timestamp key whose
  values don't match what was actually computed this run.
- `apply_minute3gateway_active_formula()` re-anchors column P's rewritten window to `=F{row}`
  every cycle rather than only ever seeding row 2, so a bad churn/resume value that slips into
  the recursive P(r)=P(r-1)-G(r-1)+H(r-1) chain can only persist for one window instead of forever.
