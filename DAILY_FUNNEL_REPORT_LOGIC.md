# Daily Funnel Report — Data Sources & Logic (v2, confirmed)

This document describes exactly what data to pull and how to classify it to reproduce the daily funnel report in this format:

```
Date | Payment Gateway | Distinct Amount (Rs) | Activity | Count | Revenue
```

One row per (Date, Gateway, Amount, Activity) combination.

This version **supersedes** the original v1 logic below in every place they conflict — v1 had two undiscovered bugs (payment-attempts undercounting for CF/PT trials, and a retry-duplication issue in first-debit classification) that are fixed here. Kept for history at the bottom of this file.

---

## The 10 activity types

| # | Activity | Date basis | Gateway/Amount shown | Revenue |
|---|---|---|---|---|
| 1 | Meta Spends | Ad spend day | blank / blank | 0 (value is in the Count column) |
| 2 | Sign Up | Signup day | blank / 0 | 0 |
| 3 | 1st Message | First-message day | blank / 0 | 0 |
| 4 | Trial Intent | Trial attempt day | per gateway / blank (or 1) | 0 |
| 5 | Trial Successful | Trial payment day | per gateway / 1 | count × 1 |
| 6 | **Debit Success** | **Mandate creation date** | per gateway / actual amount | count × amount |
| 7 | **Halted Reactivation** | **Payment date** | per gateway / actual amount | count × amount |
| 8 | Renewal | Payment date | per gateway / actual amount | count × amount |
| 9 | Mandates Cancelled | Mandate creation date | per gateway / plan amount | blank (no revenue — no payment happened) |
| 10 | Mandates Active | Mandate creation date | per gateway / plan amount | blank |

**Note the mixed date basis** — this is intentional and confirmed with the business owner: Trial Intent/Successful use the trial event's own date; Debit Success and the two Mandate-status rows use the *mandate's* creation date (a cohort view — "what happened to mandates created on day X"); Halted Reactivation and Renewal use the actual *payment* date (a revenue view — "what money came in on day X").

---

## 1-3. Meta Spends / Sign Up / 1st Message

Unchanged from v1 — see SQL at the bottom of this file (Known bugs section retains the original queries, still correct).

**Caveat**: `meta_ads_daily` keeps backfilling for several days after the fact — always treat the last 7-10 days as provisional. `messages` needs the signup-date-window trick (no usable index for a pure date-range scan on 851M+ rows).

---

## 4-5. Trial Intent / Trial Successful

Every ₹1 payment attempt, per gateway, per day (of the trial's own `initiated_at`/`payment_time`/`created_at`).

**Critical fix from v1**: the ₹1 trial/authorization event for Cashfree and Paytm does **NOT** reliably appear in `payment_attempts` — that table only captures a small, inconsistent fraction of it (confirmed: as low as 0 rows for Cashfree over a full month where the true count was in the hundreds per day). The correct sources are the raw gateway webhook mirrors:

- **Cashfree**: `cashfree_payments` (a `document` jsonb store). Filter `(document->>'amount_paise')::numeric = 100`. `document->>'payment_status'` is `'SUCCESS'` / `'FAILED'` / `'CANCELLED'`. Date field: `document->>'payment_time'`.
- **Paytm**: `paytm_payments` (same document-store shape). Same filter on `amount_paise`. `document->>'payment_status'` is `'SUCCESS'` / `'FAILED'`. Date field: `document->>'payment_time'`. **`document->>'user_id'` is already a proper `users.id` UUID here — no join needed.**
- **Razorpay**: `razorpay_payments`, `(entity_data->>'amount')::numeric = 100`, status `'captured'` = successful. Unaffected by the bug above (this table already *is* the raw store, unlike CF/PT).

Trial Intent = count of all rows (any status) that day/gateway. Trial Successful = count filtered to success/captured, Revenue = that count (₹1 each).

**Payer identity for Cashfree trial rows** (needed for the cohort classification, not for the Intent/Successful counts themselves): `cashfree_payments` doesn't carry a clean `user_id` (it's null on most rows). Resolve via `document->>'customer_email'` → `users.email`, falling back to `document->>'customer_phone'` (normalized to last-10-digits, excluding the placeholder value `'9999999999'`) → `users.phone_number`. Combining both paths gets 100% match in practice.

---

## 6-8. Debit Success / Halted Reactivation / Renewal

### Payer identity for real (>₹1) payments — unchanged from v1

- **Cashfree**: `payment_attempts` (succeeded) → `invoices.charge_id` → `invoices.user_id`.
- **Paytm**: `payment_attempts` (succeeded) → `metadata->>'paytm.subscription_id'` → `paytm_subscriptions._id` → `document->>'user_id'`. (Do NOT use `paytm_billing_plans.last_attempt_id` — 8.6% match rate, broken.)
- **Razorpay**: `razorpay_payments` (captured) → regex `^user-([0-9a-fA-F-]{36})@noemail\.rumik\.ai$` on `entity_data->>'email'`, falling back to `users.email` join for real emails.

`payment_attempts` is reliable for *real* payments (this was validated repeatedly against reference tables) — the bug above only affects ₹1 trial rows.

### Mandate/plan-amount lookup

- **Cashfree/Paytm**: `mandates` table, joined on `user_id` + `provider_account`. Use `max_amount_paise` for the plan amount, `status` for current state, `created_at` for the creation timestamp.
- **Razorpay**: `razorpay_subscriptions`, matched via **both** the synthetic-email regex on `entity_data->>'customer_email'` AND a real-email fallback to `users.email` (do NOT use `mandates` for Razorpay — it only covers the small "Charge At Will" side-product). Plan amount = `(entity_data->'notes'->>'expected_price_paise')` — ~98% coverage, far more reliable than trying to infer it from `plan_id` (a single `plan_id` maps to many different actual amounts due to regional pricing/discounts).

When a payer has multiple mandate/subscription records for the same gateway (e.g. retried after a failed setup), match each event to the **nearest-by-time** mandate record, not just any/the first one.

### Classification algorithm (rank real payments per payer+gateway)

1. Build the full set of successful real (amount ≠ ₹1) payments, with `payer_key` resolved per gateway.
2. **Process every successful ₹1 trial chronologically** (oldest first), per (payer, gateway):
   - Find that trial's nearest mandate/subscription record → its `created_at` and current `status`.
   - Look for the payer's earliest real payment with `paid_at >= mandate_created_at` that **has not already been claimed by an earlier trial from the same payer** (see dedup below).
   - If found: `gap_days = paid_at - mandate_created_at`.
     - `gap_days <= 15` → **Debit Success**, row dated by **mandate_created_at**.
     - `gap_days > 15` → **Halted Reactivation**, row dated by the **payment's own date**.
   - If not found: the payer hasn't converted yet.
     - Current mandate status ∈ {cancelled, revoked, expired, failed, halted} → **Mandates Cancelled**, dated by mandate creation date, amount = plan amount.
     - Current mandate status ∈ {active, paused, authenticated, authorization_pending, created, pending, completed} → **Mandates Active**, dated by mandate creation date, amount = plan amount.
     - No mandate found at all → excluded (small residual, ~3-5% of trials, mostly unresolved Razorpay identities).
3. **Retry-dedup rule** (critical fix from v1): mark each real payment as "consumed" once it's matched to a trial. If a payer retries the ₹1 trial multiple times (each creating a new mandate attempt), only the **earliest** trial/mandate that actually produced a real payment gets credited as Debit Success/Halted Reactivation — later retries by the same payer, having found no unconsumed payment, correctly fall into Mandates Cancelled/Active instead. Without this, a single real payment gets claimed by every retry independently, inflating conversion counts (confirmed case: one user's 5 retries in 3 days turned 1 real conversion into 5 counted rows).
4. **Renewal**: separately, rank all real payments per (payer, gateway) by `paid_at` ascending. Every payment beyond the first (`rn > 1`) is a Renewal, dated by its own payment date. **Ranking is per-gateway, not combined across all of a payer's gateways** (confirmed decision — differs from a very early draft of this logic which said "across all gateways combined").

**Entire Revenue = Trial Successful + Debit Success + Halted Reactivation + Renewal.**

---

## 9-10. Mandates Cancelled / Mandates Active

See step 2 of the classification algorithm above — these are the two non-conversion outcomes for a trial-successful payer who has made zero real payments to date. Both dated by mandate creation date, amount = the mandate's plan amount (not ₹1).

These are **current-status snapshots**, not permanent — a mandate created N days ago showing "Active" today may show "Cancelled" tomorrow if it fails to convert. Any rerun of this report will produce different Mandates Cancelled/Active numbers for the *same* historical date, because the status is evaluated as of "now," not as of that date. This is expected, not a bug.

---

## Known caveats (confirmed, keep reporting these every morning)

1. **Settling lag**: any date within the last ~7-10 days is provisional. `meta_ads_daily`, payment records, and mandate statuses all keep changing for several days after the fact. Flag the last 7-10 days as "still settling" in every report.
2. **Metabase silently truncates any un-paginated query at 2000 rows** — always paginate anything that could exceed 2000 rows:
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
3. **~3-5% of trials/payments never resolve to a user** — mostly Razorpay `entity_data->>'email'` values matching neither the synthetic pattern nor a real `users.email` row. These are silently excluded from every count (not shown as an "Unresolved" row). Confirmed acceptable.
4. **Cashfree launched ~2026-07-23, Paytm ~2026-08-10** — there is genuinely zero data for these gateways before their launch dates. This is real business history, not a query gap.
5. Occasional real anomalies will show up that are NOT bugs — e.g. a specific date where a large fraction of one gateway's mandates go `halted` almost instantly after trial (confirmed real via manual mandate-timestamp inspection, not a matching artifact) — call these out explicitly rather than assuming they're pipeline errors.

---

## Reconciliation check

```sql
-- true total revenue for a date, no identity resolution needed
select sum(amount) from (
  select amount_paise/100.0 as amount from payment_attempts
  where status='succeeded' and provider_account in ('cashfree_recurring','paytm_recurring') and amount_paise != 100
  union all
  select (entity_data->>'amount')::numeric/100.0 from razorpay_payments
  where (entity_data->>'status')='captured' and (entity_data->>'amount')::numeric != 100
) x where <payment-date filter>
```
Compare to Debit Success + Halted Reactivation + Renewal revenue for the same date (payment-date basis only — Debit Success rows are dated by mandate creation, so reconcile using each row's underlying payment date, not its report date, when doing this check).

---

## Appendix: v1 logic (superseded, kept for history)

The original version of this document used `payment_attempts` for Cashfree/Paytm trial detection (now known to undercount by 10-100x) and had no retry-dedup logic (now known to inflate Debit Success/Halted Reactivation counts whenever a payer retried the trial). It also used a 7-day-window framing for "did they convert" rather than the 15-day mandate-to-payment gap threshold used above. If you find an old report built with v1 logic, treat its Trial Successful, Debit Success, and Halted Reactivation numbers for Cashfree and Paytm as unreliable and rebuild with the logic above.
