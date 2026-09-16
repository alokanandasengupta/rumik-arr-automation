# Rumik ARR & Funnel Automation

A self-updating revenue/funnel reporting system built against a
multi-gateway subscription business (Cashfree, Paytm, Razorpay): a scheduled
job that reconciles trial signups through paid conversion, classifies every
payment event (first conversion vs. reactivation vs. renewal vs. churn), and
writes a daily gateway- and campaign-level report — unattended, tolerant of
the DB only being reachable over VPN.

## The core problem this solves

Payment-gateway webhooks and trial records don't line up cleanly into
"who converted, when, and for how much." A single payer can retry a failed
trial multiple times, generating several mandate attempts for one eventual
payment — naively joining trials to payments double- and triple-counts
conversions. `daily_funnel_job.py` implements a chronological,
consume-once classification algorithm (documented in full in
[`ARR automation/DAILY_FUNNEL_AUTOMATION_HANDOFF.md`](ARR%20automation/DAILY_FUNNEL_AUTOMATION_HANDOFF.md))
that resolves this correctly, plus attributes every conversion back to the
Meta ad campaign/ad-set that drove the original signup.

## What's in here

- **`ARR automation/daily_funnel_job.py`** — the production job. Runs on a
  loop (launchd/cron-friendly), retries quietly until the DB is reachable,
  maintains an incremental local cache so it never needs to re-pull full
  history, and writes a two-sheet Excel report: a gateway-level funnel
  (signup → trial → conversion → renewal/churn) and a campaign-attribution
  breakdown — both built from the *same* classification pass, so the
  numbers are guaranteed to reconcile against each other by construction,
  not by after-the-fact checking.
- **`ARR automation/DAILY_FUNNEL_AUTOMATION_HANDOFF.md`** — a complete,
  standalone spec: every metric's exact definition, the classification
  algorithm, every data-source gotcha found the hard way (silent API
  truncation, timestamp-parsing edge cases, float-precision loss on large
  IDs), and how the scheduling/retry design works. Written so a fresh
  engineer (or LLM) with DB access could pick this up cold.
- **`ARR automation/DAILY_FUNNEL_REPORT_LOGIC.md`** — the underlying
  business logic reference the job implements.
- **`ARR automation/com.rumik.dailyfunnel.plist`** — the actual launchd
  config: drop into `~/Library/LaunchAgents/` and
  `launchctl bootstrap gui/$(id -u) <path>` to run the job every 30
  minutes, all day — it no-ops if already done for the day or if the DB
  isn't reachable yet, so it doesn't need a fixed wake time.
- **`ARR automation/*.py`** (export/reconcile scripts) — supporting
  one-off scripts used to validate the job's numbers against a
  hand-maintained reference sheet during development.
- **`arr_recalculated/`** — a from-scratch ARR recalculation methodology
  and its SQL query template, built to independently verify the
  automated numbers against a ground-truth query.

## Stack

Python, Postgres (via a Metabase API layer — no direct DB driver), openpyxl
for report generation, launchd for scheduling.
