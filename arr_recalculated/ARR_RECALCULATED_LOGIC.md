# ARR Recalculated — Logic

This is an **independent reconstruction** built by Claude during a debugging session against
`metabase.prod.rumik.ai` (database id `2`, prodDB). It is **not** the same methodology as
`ARR automation/ARR_MRR_logic.md` / `sync_arr.py` (the actual production pipeline) — that one
uses the `mandates` + `razorpay_subscriptions` + real gateway payment tables (Cashfree/Paytm/
Razorpay split out), and its AOV is the *actual average payment amount* of currently-active
customers. This reconstruction instead uses the single `subscriptions` table, blended across
all platforms/gateways. Treat the two as different lenses on the same business, not as
numbers that should match exactly — see "Known divergence" below.

## Source table

`subscriptions` (public schema, prodDB). Columns used: `user_id`, `price`, `currency`,
`billing_cycle`, `start_date`, `expiry_date`, `created_at`.

## Step 1 — Deduplicate to one row per user

`subscriptions` has multiple historical rows per user (renewals, retries, failed-mandate
recovery cycles — see the earlier finding in this session: some users had up to 14 rows
simultaneously marked `status='active'`, which is why `status` is **not used** here at all).
Instead, for any point in time T, take each user's **latest-created row whose
`[start_date, expiry_date]` window covers T**:

```sql
row_number() over (partition by <time_bucket>, user_id order by created_at desc) = 1
```

This avoids double-counting a user with several overlapping/duplicate subscription rows,
and avoids relying on the `status` field (which we found gets touched inconsistently across
old and new rows for the same user).

## Step 2 — "Active as of T" filter

A row counts as active at time T if:

```sql
start_date <= T and (expiry_date is null or expiry_date >= T)
and currency = 'INR' and price > 0
```

- `currency = 'INR'`: restricts to the dominant currency (>99.9% of the base) to avoid
  mixing units in a single AOV/MRR figure. USD/AED/GBP/etc. rows are excluded from this sheet.
- `price > 0`: excludes the free `friend`-tier rows (price is `NULL` for those in recent
  months) — they contribute ₹0 and would only dilute the subscriber count's meaning.

## Step 3 — Metrics per time bucket

```
Active Subscribers = count(distinct user_id) of rows passing step 2
AOV                = avg(price) WHERE price <= 999   -- excludes outlier/high-price rows from the average only
MRR                = sum(price)                        for billing_cycle = 'monthly'
                     sum(price / 12.0)                  for billing_cycle = 'yearly'
                     -- i.e. MRR always sums the FULL population's price (not AOV x count) -
                     -- AOV is a diagnostic column, not an input to MRR here.
ARR                = MRR * 12
ARR (USD)          = ARR / 94.54   -- fixed FX rate, taken from ARR_MRR_logic.md (confirmed constant, no FX table in the DB)
```

**Important**: unlike the real pipeline (`MRR = AOV x Active Subscribers`), this reconstruction
computes MRR as the **sum of each active row's own price** (deduplicated). The `AOV` column
here excludes prices >₹999 only from *its own average* — it does not filter which rows count
toward Active Subscribers or MRR. If a stricter version (drop >₹999 rows entirely, from
subscriber count and MRR too) is wanted, that's a one-line change to Step 2's `price > 0`
filter.

## Time granularity

Same query shape works for day-wise or minute-wise — only the `generate_series` step and its
interval changes. Minute-wise buckets `[start_date, expiry_date]` at 1-minute resolution using
IST wall-clock time (`at time zone 'Asia/Kolkata'`), matching how the DB stores and Metabase
displays timestamps for this project.

## Known limitations / caveats

1. **Not point-in-time correct for historical days beyond what `created_at`/`expiry_date`
   capture.** A user's *current* subscription row's dates are used to infer historical
   activity; if their current active row was created recently, an earlier (now-superseded)
   subscription they had isn't picked up for dates before the current row's `start_date`.
   This under/over-counts in ways that haven't been fully quantified — see the Jan-Aug
   month-end reconciliation earlier in this session, where this method diverged substantially
   from the reported table for Apr-Jul (no churn dip visible here, one was visible there).
2. **Midnight step-down**: every day boundary (00:00 IST) shows a sharp drop in Active
   Subscribers (e.g. Sep 9: 28,513 -> 27,585 in the first minute; same pattern at the Sep6->
   Sep7 boundary). This repeats daily and is very likely a batch expiry/cleanup process
   re-evaluating `expiry_date` cutoffs at day start, not organic churn. **Not yet root-caused
   in the app code** — flagged for follow-up.
3. **Known divergence from the real pipeline**: this uses `subscriptions.price` (the plan's
   listed price) as both the AOV input and the MRR input. The production pipeline
   (`sync_arr.py`) instead uses **actual captured payment amounts** for AOV, and a completely
   different subscriber-count source for Razorpay (`razorpay_subscriptions.current_status`,
   not `subscriptions`). The two will not match exactly; this sheet is useful for trend/shape
   validation and fast iteration, not as a drop-in replacement for the real pipeline's numbers.

## FX rate

`94.54` INR->USD, fixed constant per `ARR automation/ARR_MRR_logic.md` (confirmed by the user
against the placeholder workbook data; there is no FX table in the DB).

## Files in this folder

- `ARR_RECALCULATED_LOGIC.md` — this file.
- `query_template.sql` — the parameterized SQL used to generate any time range (day-wise or
  minute-wise) of this sheet.
- `ARR minutewise Sep9.csv` — the output sheet itself (minute-by-minute, Sep 6 00:00 IST
  onward, appended incrementally).
