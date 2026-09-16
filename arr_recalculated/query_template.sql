-- ARR Recalculated -- query template
-- See ARR_RECALCULATED_LOGIC.md in this folder for the full explanation of every
-- clause below. Run against metabase.prod.rumik.ai, database id 2 (prodDB), via
-- POST /api/dataset (native query) with header `x-api-key: <MB_KEY>`.
--
-- To change the time range/granularity, edit the `mins` CTE's generate_series() call:
--   - Day-wise:    generate_series('<start-date>'::date, '<end-date>'::date, '1 day'::interval)
--   - Minute-wise: generate_series('<start-ts IST>'::timestamp at time zone 'Asia/Kolkata',
--                                  now(), -- or an end timestamp
--                                  '1 minute'::interval)

with mins as (
  select generate_series(
    '2026-09-09 14:21:00'::timestamp at time zone 'Asia/Kolkata',  -- start (edit per run)
    now(),                                                          -- end (edit per run)
    '1 minute'::interval
  ) as m
),
matched as (
  select
    mins.m as minute_ts,
    s.user_id,
    s.price,
    s.billing_cycle,
    row_number() over (
      partition by mins.m, s.user_id
      order by s.created_at desc
    ) as rn
  from mins
  join subscriptions s
    on s.start_date <= mins.m
   and (s.expiry_date is null or s.expiry_date >= mins.m)
   and s.currency = 'INR'
   and s.price > 0
),
current_active as (
  select * from matched where rn = 1
)
select
  (minute_ts at time zone 'Asia/Kolkata')::text as minute_ist,
  count(*) as active_subs,
  round(avg(price) filter (where price <= 999), 2) as aov,           -- AOV excludes >Rs999 from the AVERAGE only
  round(sum(case when billing_cycle = 'yearly' then price / 12.0 else price end)) as mrr
  -- ARR = mrr * 12 (computed downstream in Python, not in this query)
  -- ARR (USD) = ARR / 94.54 (fixed FX rate, see ARR_MRR_logic.md)
from current_active
group by minute_ts
order by minute_ts;

-- NOTE: for ranges wider than ~2000 result rows, Metabase's standard /api/dataset endpoint
-- truncates silently. Use the CSV export endpoint instead:
--   POST /api/dataset/csv  with form field `query` = the JSON-encoded {"type":"native",
--   "native":{"query": "<the SQL above>"}, "database": 2} object. This streams the full
--   result with no row cap.
