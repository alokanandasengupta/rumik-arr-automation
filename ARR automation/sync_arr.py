#!/usr/bin/env python3
"""
IRA ARR — syncs live production numbers from Metabase into the "IRA ARR" Google Sheet,
writing directly via the Google Sheets API (service account auth).

Intended to run every 1 minute via cron, on a machine connected to the Rumik VPN.
See ARR_MRR_logic.md in this folder for the full definition of every metric computed here.

Setup (once):
    Fill in .env in this folder: GOOGLE_SERVICE_ACCOUNT_EMAIL, GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY,
    GOOGLE_SHEET_ID (the spreadsheet's ID, from its URL between /d/ and /edit), and MB_KEY.
    Share the spreadsheet with the service account's email as Editor first.

Cron entry (every 1 minute) — .env is loaded automatically, only MB_KEY needs to be passed
(or add it to .env too and drop it from the crontab line):
    * * * * * cd "/Users/alokananda/Desktop/Rumik_on/ARR automation" && \
        MB_KEY='...' /usr/local/bin/python3 sync_arr.py >> sync_arr.log 2>&1

Manual test without touching the sheet:
    python3 sync_arr.py --dry-run
"""

import fcntl
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    sys.exit("Python 3.9+ required (zoneinfo).")

import gspread
from google.oauth2.service_account import Credentials

# macOS python.org builds ship without a populated system cert store, which makes
# urllib's default SSL context reject everything. Use certifi's bundle when present.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()


def load_dotenv(path):
    """Tiny .env loader (no external dependency) — does not override already-set env vars,
    so real cron/export values still win over the file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MB_URL = os.environ.get("MB_URL", "https://metabase.prod.rumik.ai")
MB_KEY = os.environ.get("MB_KEY")
MB_DATABASE_ID = 2

SA_EMAIL = os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
SA_PRIVATE_KEY = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY", "")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

IST = ZoneInfo("Asia/Kolkata")
FX_RATE = 94.54  # fixed INR->USD constant, confirmed — see ARR_MRR_logic.md section 4

# How many trailing minutes/buckets we (re)write on every run, to self-heal any missed tick
# OR any row a second, unidentified writer has overwritten with fabricated data (a real,
# unresolved issue as of this writing — see ARR_MRR_logic.md). Widened from the original 12/1
# specifically so a stray bad write gets clawed back within one cycle instead of lingering for
# several minutes while that other process's row sits unmatched by anything in our own lookback.
LOOKBACK_MINUTES = 60
INTRADAY_LOOKBACK_BUCKETS = 6  # 6 x 10-min buckets = 60 minutes, matching LOOKBACK_MINUTES

INTRADAY_RETENTION_DAYS = 15
MINUTE_RETENTION_DAYS = 3

STATE_FILE = Path(__file__).parent / "arr_sync_state.json"
LOCK_FILE = Path(__file__).parent / ".sync_arr.lock"

GATEWAYS = [
    ("Cashfree", "cashfree_recurring"),
    ("Paytm", "paytm_recurring"),
    ("Razorpay", "razorpay_caw_recurring"),
]
PROVIDER_TO_LABEL = {p: label for label, p in GATEWAYS}

ACTIVE_STATUSES_SQL = "('active','created','paused','expired')"
CHURNED_STATUSES_SQL = "('revoked','failed','authorization_pending')"

SHEET1_HEADERS = [
    "Date", "Payment Gateway", "MRR (Rs)", "New MRR Added", "MRR Churned",
    "Net MRR Change (+/-)", "Active Subscribers (Trailing 30d / Mandate-based)",
    "Net Subscriber Change (+/-)", "Avg MRR per Subscriber (Rs)", "MRR Calculated",
    "ARR", "MRR (USD)", "ARR (USD)",
]
INTRADAY_HEADERS = [
    "Time (10-min bucket start)", "Payment Gateway", "Payments in Bucket",
    "New Distinct Payers", "Revenue in Bucket (Rs)", "Cumulative Revenue Today (Rs)",
    "Active Subscribers (Running)", "Avg MRR per Subscriber (Rs, Trailing 30d)",
    "MRR (Rs)", "ARR (Rs)", "MRR (USD)", "ARR (USD)",
]
MINUTE_HEADERS = [
    "Time (1-min)", "Payments in Minute", "New Distinct Payers", "Revenue in Minute (Rs)",
    "Cumulative Revenue Today (Rs)", "Active Subscribers (Running)", "Churned This Minute",
    "Resumed This Minute", "Avg MRR per Subscriber (Rs)", "MRR (Rs)", "ARR (Rs)",
    "MRR (USD)", "ARR (USD)", "Δ MRR (USD)", "Δ ARR (USD)", "Active Subscribers (Recursive)",
]

DRY_RUN = "--dry-run" in sys.argv


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def acquire_lock():
    """Refuses to let two runs execute concurrently. launchd's StartInterval can fire a new
    tick while a slow previous run (a manual invocation, a slow VPN/Metabase response) is still
    writing to the sheet -- two overlapping runs interleaving their append/delete/sort/formula
    calls is exactly what left a handful of rows without a P-column value in one earlier
    incident. Held for the lifetime of the process; released automatically on exit."""
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[{datetime.now(IST).isoformat()}] another run is still in progress, skipping this tick")
        sys.exit(0)
    return lock_fp  # keep this reference alive (module-level) so the lock isn't GC'd/released early


def parse_ts(value):
    """Metabase serializes timestamps as ISO strings; normalize to a naive datetime
    (values already carry 'AT TIME ZONE Asia/Kolkata' wall-clock semantics from the SQL)."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    s = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=None)


def mb_query(sql):
    if not MB_KEY:
        die("MB_KEY environment variable is not set.")
    payload = json.dumps({"database": MB_DATABASE_ID, "type": "native", "native": {"query": sql}}).encode()
    req = urllib.request.Request(
        f"{MB_URL}/api/dataset", data=payload, method="POST",
        headers={"X-API-Key": MB_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90, context=SSL_CONTEXT) as resp:
            body = json.loads(resp.read())
    except urllib.error.URLError as e:
        die(f"Metabase request failed (is the VPN connected?): {e}")
    if "data" not in body:
        die(f"Metabase query error: {body.get('error', body)}\nSQL:\n{sql}")
    cols = [c["name"] for c in body["data"]["cols"]]
    return [dict(zip(cols, row)) for row in body["data"]["rows"]]


# ---------------------------------------------------------------------------
# Google Sheets (service account) helpers
# ---------------------------------------------------------------------------

_gc = None
_ss = None


def sheets_client():
    global _gc, _ss
    if _ss is not None:
        return _ss
    if not SA_EMAIL or not SA_PRIVATE_KEY:
        die("GOOGLE_SERVICE_ACCOUNT_EMAIL / GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY are not set (check .env).")
    if not SHEET_ID or SHEET_ID.startswith("REPLACE_ME"):
        die("GOOGLE_SHEET_ID is not set — paste the spreadsheet ID (the long string in its URL "
            "between /d/ and /edit) into .env.")
    private_key = SA_PRIVATE_KEY.replace("\\n", "\n")
    info = {
        "type": "service_account",
        "client_email": SA_EMAIL,
        "private_key": private_key,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    creds = Credentials.from_service_account_info(info, scopes=SHEETS_SCOPES)
    _gc = gspread.authorize(creds)
    # gspread sets NO timeout by default -- a slow/stuck response from Google (observed once
    # already: two ESTABLISHED connections sitting idle, the process pinned at 0% CPU for 16+
    # minutes) hangs forever rather than raising, and since acquire_lock() holds the run lock the
    # whole time, every subsequent launchd tick just skips instead of retrying. (connect, read)
    # timeout in seconds -- generous since Sheets calls can legitimately take a few seconds, but
    # bounded so a genuinely stuck request fails fast instead of blocking every future cycle.
    _gc.set_timeout((10, 30))
    try:
        _ss = _gc.open_by_key(SHEET_ID)
    except Exception as e:
        die(f"Could not open spreadsheet (is it shared with {SA_EMAIL} as Editor?): {e}")
    return _ss


def get_or_create_worksheet(title, headers):
    ss = sheets_client()
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
        return ws
    if not ws.row_values(1):
        ws.append_row(headers, value_input_option="RAW")
    return ws


def upsert_rows(sheet_title, key_column, headers, keys_to_replace, rows):
    """Append the new rows, THEN delete the stale ones they replace, then re-sort — append-first
    so a concurrent reader (the dashboard polling mid-write) sees a brief moment of *duplicate*
    rows at worst, never a moment where the current row is simply missing. The old delete-first
    order left a real gap (rows deleted, not yet re-appended) that a same-second dashboard poll
    could catch, making the live figure appear to regress to an older, lower value before
    self-correcting on the next 60s poll."""
    if DRY_RUN:
        print(f"[dry-run] would upsert sheet={sheet_title} rows={len(rows)} keys={keys_to_replace}")
        return

    ws = get_or_create_worksheet(sheet_title, headers)
    existing_headers = ws.row_values(1)
    key_idx = existing_headers.index(key_column) if key_column in existing_headers else 0

    col_values = ws.col_values(key_idx + 1)  # includes header at index 0
    keys_set = set(keys_to_replace)
    # computed from the pre-append snapshot, so these row numbers stay valid after appending —
    # append only adds rows at the end, never shifts anything before it
    rows_to_delete = [i + 1 for i, v in enumerate(col_values) if i > 0 and v in keys_set]

    if rows:
        ws.append_rows(rows, value_input_option="RAW")

    if rows_to_delete:
        ss = sheets_client()
        requests = [
            {"deleteDimension": {
                "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": r - 1, "endIndex": r}
            }}
            for r in sorted(rows_to_delete, reverse=True)
        ]
        ss.batch_update({"requests": requests})

    last_row = len(ws.col_values(1))
    if last_row > 2:
        ws.sort((1, "asc"), (2, "asc"), range=f"A2:{gspread.utils.rowcol_to_a1(last_row, len(headers))}")


def trim_old_rows(sheet_title, key_column, retention_days):
    """Delete rows whose key_column timestamp/date is older than retention_days. Cheap no-op
    when nothing qualifies; call sparingly (e.g. once an hour) to limit Sheets API traffic.
    Returns the number of rows deleted, so a caller that depends on row 2's identity (Minute3Gate-
    way's P-column chain) knows when it needs reseeding -- see the call site in main()."""
    if DRY_RUN:
        return 0
    ws = get_or_create_worksheet(sheet_title, [key_column])
    headers = ws.row_values(1)
    if key_column not in headers:
        return 0
    key_idx = headers.index(key_column)
    col_values = ws.col_values(key_idx + 1)
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=retention_days)

    rows_to_delete = []
    for i, v in enumerate(col_values):
        if i == 0 or not v:
            continue
        try:
            ts = datetime.fromisoformat(v) if len(v) > 10 else datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            continue
        if ts < cutoff:
            rows_to_delete.append(i + 1)

    if not rows_to_delete:
        return 0
    ss = sheets_client()
    requests = [
        {"deleteDimension": {
            "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": r - 1, "endIndex": r}
        }}
        for r in sorted(rows_to_delete, reverse=True)
    ]
    ss.batch_update({"requests": requests})
    print(f"trimmed {len(rows_to_delete)} rows older than {retention_days}d from {sheet_title}")
    return len(rows_to_delete)


def apply_minute3gateway_active_formula(ws):
    """Maintains 'Active Subscribers (Recursive)' (column P) on Minute3Gateway as a live,
    self-referencing spreadsheet formula: P2 = F2 (seed, the oldest row's python-computed
    value), and P(r) = P(r-1) - G(r-1) + H(r-1) for every row after -- i.e. previous minute's
    running total, minus that minute's churn, plus that minute's resumes. Column F itself is
    left alone (still whatever sync_arr computed directly from Metabase each run).

    MRR (Rs)/ARR (Rs)/MRR (USD)/ARR (USD) (columns J-M) are rebuilt alongside it as formulas
    derived from P and the Avg MRR per Subscriber (Rs) column (I): MRR = P * avg, ARR = MRR*12,
    then both divided by FX_RATE for the USD columns.

    Re-applied fresh on every run rather than written once and left alone, because upsert_rows()
    deletes/re-appends/sorts rows each cycle -- a sort relocates a row's contents but does NOT
    rewrite the row numbers inside a formula's relative references, so a formula written before
    a sort would silently point at the wrong row after one. Rebuilding the sheet's actual current
    positions every time sidesteps that entirely.

    Only the tail (last WINDOW rows) is ever rewritten, not the whole column: rows older than
    that already carry correct formulas from a previous run and are untouched by this run's
    upsert/trim, so respraying formula text into all ~4000+ historical rows every 60s bought
    nothing but multi-second Sheets API calls that were pushing the whole sync cycle past 60s.
    This is purely a performance bound, not a correctness boundary: row 2 is the ONE seed
    (`P2 = F2`), and every row from 3 onward -- in this window or any earlier one written by a
    past run -- chains to the row directly above it with no other reseed point anywhere, so the
    formula is one continuous, unbroken drag-down from the top rather than a series of independent
    segments. (An earlier version re-anchored each window's first row straight to F as a defense
    against a second writer corrupting the chain; that writer -- three orphaned launchd jobs from
    an abandoned earlier automation attempt, see ARR_MRR_logic.md -- has since been found and
    stopped, and dedupe_minute_rows() now keeps any future conflicting row out of the chain before
    it can poison anything, so that reseed no longer serves a purpose and only broke continuity.)

    P2 = F2, permanently and unconditionally, every single run. An earlier version of this
    function stopped enforcing that (reasoning that a manual correction to the seed shouldn't get
    silently undone) -- that reasoning was backwards: P2 is not a value anyone should ever be
    "correcting" in the first place, per the original spec ("p2=f2", literally). Treating P/F
    disagreement as something to fix by editing the seed just breaks the one guarantee that
    actually matters (P2=F2, always) without addressing anything real -- drift between P and F is
    exactly what verify_and_backfill_formula_columns()'s drift-check warning exists to surface, as
    information for a human, not license for automated code (or an agent acting on a hunch) to
    silently rewrite row 2's seed to chase realignment with the tail. Re-enforcing this every run
    is what actually keeps the seed's meaning intact.

    Returns the last_row it computed, so the caller (main()) can hand that same number to
    verify_and_backfill_formula_columns() right after instead of it re-reading column A again --
    this function's own writes never change the row count, so the value stays valid."""
    if DRY_RUN:
        return None
    last_row = len(ws.col_values(1))
    ws.update(range_name="P2", values=[["=F2"]], value_input_option="USER_ENTERED")
    if last_row < 3:
        return last_row
    WINDOW = LOOKBACK_MINUTES + 15  # safety buffer over the upsert replace-window
    p_start = max(3, last_row - WINDOW + 1)
    p_formulas = [[f"=P{r-1}-G{r-1}+H{r-1}"] for r in range(p_start, last_row + 1)]
    ws.update(range_name=f"P{p_start}:P{last_row}", values=p_formulas, value_input_option="USER_ENTERED")
    jm_start = max(2, last_row - WINDOW + 1)
    mrr_arr_formulas = [
        [f"=P{r}*I{r}", f"=J{r}*12", f"=J{r}/{FX_RATE}", f"=K{r}/{FX_RATE}"]
        for r in range(jm_start, last_row + 1)
    ]
    ws.update(range_name=f"J{jm_start}:M{last_row}", values=mrr_arr_formulas, value_input_option="USER_ENTERED")
    return last_row


def verify_and_backfill_formula_columns(ws, last_row):
    """The manual equivalent of this would be selecting P2:P<last> and J2:M<last> and hitting
    Ctrl+D (fill down) after every new row -- this is that, automated and self-checking.

    apply_minute3gateway_active_formula() only rewrites a trailing WINDOW of rows each cycle for
    speed, which covers every row filled by this script's own normal (non-backfill) operation.
    But any row outside that window that's still missing its P/J-M formula -- e.g. one written by
    a `--since` backfill reaching further back than WINDOW, or any other gap -- would otherwise go
    unnoticed forever. This scans the ENTIRE P and J columns every run to confirm every filled row
    actually has its formula, and patches any gap it finds with a single targeted write instead of
    a full rebuild. Always logs the outcome, so 'it's been dragged down' is a verified fact each
    cycle, not an assumption.

    Takes the already-open worksheet and a known-current last_row (both computed once by the
    caller) rather than re-fetching either -- this and apply_minute3gateway_active_formula() used
    to each independently call get_or_create_worksheet() and re-read the whole first column, and
    this function alone used to issue two separate full-column reads (P then J); stacked on top of
    everything else upsert_rows()/dedupe_minute_rows() already do every cycle, that redundant read
    volume tripped Google's per-user Sheets API read-quota (429 Quota exceeded), silently dropping
    whole cycles -- and since the dashboard reads through the very same service account, it shares
    that same quota and can 429 too. One batch_get for both columns here, and one shared last_row
    across the whole post-upsert pipeline, cuts this function from 3 read calls to 1."""
    if DRY_RUN or last_row < 3:
        return

    def is_formula(cell_row):
        return len(cell_row) > 0 and str(cell_row[0]).startswith("=")

    # P2:P (not P3:P): apply_minute3gateway_active_formula() unconditionally sets P2="=F2" before
    # this runs, so P2 is always already correct by the time we get here -- included in the same
    # scan as everything else rather than special-cased.
    p_range, j_range = f"P2:P{last_row}", f"J2:J{last_row}"
    batches = ws.batch_get([p_range, j_range], value_render_option="FORMULA")
    p_vals, j_vals = batches[0], batches[1]
    p_gaps = [i + 2 for i, row in enumerate(p_vals) if not is_formula(row)]
    j_gaps = [i + 2 for i, row in enumerate(j_vals) if not is_formula(row)]

    # Drift check: P's recursive chain only ever reacts to churn/resume (G/H) -- it has no way to
    # notice if F itself jumps (e.g. a local-ledger state reset snapping back to a fresh Metabase
    # count, as happened once already: F dropped ~2300 in a single minute while G/H that minute
    # were near zero, and P just kept compounding from the old baseline forever after). This is a
    # warning only, never an auto-fix -- the chain stays exactly what "drag the formula down" means,
    # with a human deciding if and when a gap this size warrants a manual reseed like the one used
    # to correct that incident (see ARR_MRR_logic.md).
    DRIFT_ALERT_THRESHOLD = 150
    tail = ws.get(f"F{last_row}:P{last_row}", value_render_option="UNFORMATTED_VALUE")
    if tail and len(tail[0]) >= 11:
        try:
            f_val, p_val = float(tail[0][0]), float(tail[0][10])
            if abs(p_val - f_val) > DRIFT_ALERT_THRESHOLD:
                print(f"[{datetime.now(IST).isoformat()}] WARNING: column P (Active Subscribers "
                      f"Recursive) has drifted from F (Active Subscribers Running) by "
                      f"{p_val - f_val:+.0f} at row {last_row} (F={f_val:.0f} P={p_val:.0f}) -- "
                      f"P's chain doesn't see F's own resets, this needs a manual reseed if it "
                      f"keeps growing")
        except (TypeError, ValueError, IndexError):
            pass

    if not p_gaps and not j_gaps:
        print(f"[{datetime.now(IST).isoformat()}] verified: P and J-M formulas present for all "
              f"{last_row - 1} data row(s)")
        return

    updates = []
    for r in p_gaps:
        formula = "=F2" if r == 2 else f"=P{r-1}-G{r-1}+H{r-1}"
        updates.append({"range": f"P{r}", "values": [[formula]]})
    for r in j_gaps:
        updates.append({"range": f"J{r}", "values": [[f"=P{r}*I{r}"]]})
        updates.append({"range": f"K{r}", "values": [[f"=J{r}*12"]]})
        updates.append({"range": f"L{r}", "values": [[f"=J{r}/{FX_RATE}"]]})
        updates.append({"range": f"M{r}", "values": [[f"=K{r}/{FX_RATE}"]]})
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    print(f"[{datetime.now(IST).isoformat()}] backfilled missing formulas: "
          f"P gap row(s)={p_gaps} J-M gap row(s)={j_gaps}")


def dedupe_minute_rows(ws, minute_keys, minute_rows):
    """Removes any Minute3Gateway row sharing a timestamp key with what THIS run just wrote, but
    whose values don't match what was actually computed -- i.e. a row injected by the persistent,
    unidentified second writer described in ARR_MRR_logic.md (a different, conflicting 'regime' of
    active-subscriber/ARR numbers has repeatedly shown up interleaved with our own correct rows,
    sometimes on nearly every minute). upsert_rows()'s delete step only clears rows it saw in a
    snapshot taken *before* it appended -- if that other writer inserts its own row for the same
    minute in the gap between our snapshot and our delete, its row survives untouched.

    This runs right after upsert_rows() settles (append+delete+sort done) and, for every key we
    just wrote, keeps whichever physical row's numbers are closest to what we computed, deleting
    any other row sharing that same key. Matters most as a companion to the P-column formula
    (apply_minute3gateway_active_formula): that formula's recursive chain reads whatever churn/
    resume values sit in the sheet, so leaving a conflicting duplicate in place would poison it."""
    if DRY_RUN or not minute_keys:
        return
    expected = dict(zip(minute_keys, minute_rows))
    key_set = set(minute_keys)
    col_a = ws.col_values(1)
    candidate_rows = [i + 1 for i, v in enumerate(col_a) if i > 0 and v in key_set]
    if not candidate_rows:
        return
    first_r, last_r = min(candidate_rows), max(candidate_rows)
    block = ws.get(f"A{first_r}:I{last_r}", value_render_option="UNFORMATTED_VALUE")

    by_key = {}
    for offset, row in enumerate(block):
        if not row:
            continue
        key = str(row[0])
        if key not in key_set:
            continue
        by_key.setdefault(key, []).append((first_r + offset, row))

    def mismatch(row, exp):
        total = 0.0
        for i in range(1, min(len(row), len(exp))):
            try:
                total += abs(float(row[i]) - float(exp[i]))
            except (TypeError, ValueError):
                total += 1.0
        return total

    rows_to_delete = []
    dup_keys = []
    for key, occurrences in by_key.items():
        if len(occurrences) < 2:
            continue
        exp = expected.get(key)
        if exp is None:
            continue
        occurrences.sort(key=lambda item: mismatch(item[1], exp))
        rows_to_delete.extend(r for r, _ in occurrences[1:])  # keep the closest match only
        dup_keys.append(key)

    if not rows_to_delete:
        return
    ss = sheets_client()
    requests = [
        {"deleteDimension": {
            "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": r - 1, "endIndex": r}
        }}
        for r in sorted(set(rows_to_delete), reverse=True)
    ]
    ss.batch_update({"requests": requests})
    print(f"[{datetime.now(IST).isoformat()}] deduped {len(rows_to_delete)} conflicting "
          f"Minute3Gateway row(s) for {len(dup_keys)} key(s): {sorted(dup_keys)}")


# ---------------------------------------------------------------------------
# State (only used to compute Sheet1's day-over-day Net Change columns —
# the Apps Script exposes no "read all rows" action, so we keep yesterday's
# finalized totals locally instead of reading them back from the sheet)
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"prev_day_date": None, "prev_day": {}, "today_date": None, "today_snapshot": {},
            "minute_baseline": {"mrr_usd": None, "arr_usd": None}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def roll_state_if_new_day(state, today_str):
    if state.get("today_date") != today_str:
        # whatever we last computed as "today" becomes the new "yesterday" baseline
        if state.get("today_date") is not None:
            state["prev_day_date"] = state["today_date"]
            state["prev_day"] = state.get("today_snapshot", {})
        state["today_date"] = today_str
        state["today_snapshot"] = {}
    return state


# ---------------------------------------------------------------------------
# Metric queries
# ---------------------------------------------------------------------------

def fetch_gateway_state():
    """Current active-subscriber count + all-time AOV (Rs 1 < amount <= Rs 999), per gateway.

    Cashfree/Paytm: UPI-Autopay mandate-based (their only recurring-billing rail here).
    Razorpay: NOT mandate-based — mandates only covers a small newer "Charge At Will" side
    product (razorpay_caw_recurring, ~7k). The real Razorpay population runs through the
    primary razorpay_subscriptions entity (current_status='active', ~27k) — see ARR_MRR_logic.md.
    """
    mandate_sql = f"""
    with latest_mandate as (
      select distinct on (user_id) user_id, id as mandate_id, provider_account, status
      from mandates
      order by user_id, created_at desc
    ),
    active_mandate as (
      select * from latest_mandate where status in {ACTIVE_STATUSES_SQL}
    ),
    active_counts as (
      select provider_account, count(*) as active_subscribers
      from active_mandate
      where provider_account in ('cashfree_recurring', 'paytm_recurring')
      group by provider_account
    ),
    cashfree_aov as (
      select avg(pa.amount_paise) / 100.0 as aov
      from payment_attempts pa
      join invoices i on i.charge_id = pa.charge_id
      join active_mandate am on am.user_id = i.user_id and am.provider_account = 'cashfree_recurring'
      where pa.provider_account = 'cashfree_recurring' and pa.status = 'succeeded'
        and pa.amount_paise != 100 and pa.amount_paise <= 99900
    ),
    paytm_aov as (
      select avg(pa.amount_paise) / 100.0 as aov
      from payment_attempts pa
      join paytm_billing_plans bp on bp.last_attempt_id = pa.id
      join active_mandate am on am.mandate_id = bp.mandate_id and am.provider_account = 'paytm_recurring'
      where pa.provider_account = 'paytm_recurring' and pa.status = 'succeeded'
        and pa.amount_paise != 100 and pa.amount_paise <= 99900
    )
    select
      ac.provider_account,
      ac.active_subscribers,
      coalesce(
        case ac.provider_account
          when 'cashfree_recurring' then (select aov from cashfree_aov)
          when 'paytm_recurring' then (select aov from paytm_aov)
        end, 0) as avg_mrr_per_subscriber
    from active_counts ac
    """
    # AOV restricted to the active population's OWN payments (via Razorpay's native customer_id,
    # present on both razorpay_subscriptions and razorpay_payments) rather than all captured
    # razorpay_payments company-wide. Confirmed ~555 vs the old ~406 -- the old company-wide
    # figure diluted in a lot of payment history from users no longer subscribed.
    razorpay_sql = """
    with active_customers as (
      select distinct entity_data->>'customer_id' as customer_id
      from razorpay_subscriptions
      where current_status = 'active' and entity_data->>'customer_id' is not null
    ),
    active_customer_payments as (
      select (p.entity_data->>'amount')::numeric / 100.0 as amount
      from razorpay_payments p
      join active_customers a on p.entity_data->>'customer_id' = a.customer_id
      where (p.entity_data->>'status') = 'captured'
        and (p.entity_data->>'amount')::numeric > 100
        and (p.entity_data->>'amount')::numeric <= 99900
    )
    select
      (select count(*) from razorpay_subscriptions where current_status = 'active') as active_subscribers,
      (select avg(amount) from active_customer_payments) as avg_mrr_per_subscriber
    """
    state = {p: {"active_subscribers": 0, "avg_mrr_per_subscriber": 0.0} for _, p in GATEWAYS}
    for r in mb_query(mandate_sql):
        state[r["provider_account"]] = {
            "active_subscribers": int(r["active_subscribers"] or 0),
            "avg_mrr_per_subscriber": float(r["avg_mrr_per_subscriber"] or 0),
        }
    rzp = mb_query(razorpay_sql)[0]
    state["razorpay_caw_recurring"] = {
        "active_subscribers": int(rzp["active_subscribers"] or 0),
        "avg_mrr_per_subscriber": float(rzp["avg_mrr_per_subscriber"] or 0),
    }
    return state


def fetch_today_minute_series():
    """Per-minute, per-gateway payment count + revenue for today (IST), for cumsum + bucketing.

    Cashfree/Paytm come from payment_attempts (their real rail). Razorpay comes from
    razorpay_payments (the primary Subscriptions-API rail), NOT payment_attempts' tiny
    razorpay_caw_recurring slice — see fetch_gateway_state() docstring."""
    sql = """
    select date_trunc('minute', completed_at at time zone 'Asia/Kolkata') as minute_ist,
           provider_account, count(*) as payments, sum(amount_paise) / 100.0 as revenue
    from payment_attempts
    where status = 'succeeded' and provider_account in ('cashfree_recurring', 'paytm_recurring')
      and (completed_at at time zone 'Asia/Kolkata')::date = (now() at time zone 'Asia/Kolkata')::date
    group by 1, 2
    union all
    select date_trunc('minute', to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata') as minute_ist,
           'razorpay_caw_recurring' as provider_account,
           count(*) as payments, sum((entity_data->>'amount')::numeric) / 100.0 as revenue
    from razorpay_payments
    where (entity_data->>'status') = 'captured'
      and (to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata')::date
          = (now() at time zone 'Asia/Kolkata')::date
    group by 1
    order by 1
    """
    rows = mb_query(sql)
    for r in rows:
        r["minute_ist"] = parse_ts(r["minute_ist"])
    return rows


def fetch_recent_distinct_payers(lookback_minutes):
    """Distinct payers per minute per gateway, last N minutes (bounded window, cheap joins).
    Razorpay uses razorpay_payments' own customer_id (no reliable canonical user_id join there)."""
    sql = f"""
    with recent_users as (
      select date_trunc('minute', pa.completed_at at time zone 'Asia/Kolkata') as minute_ist,
             pa.provider_account, i.user_id::text as payer_key
      from payment_attempts pa
      join invoices i on i.charge_id = pa.charge_id
      where pa.status='succeeded' and pa.provider_account='cashfree_recurring'
        and pa.completed_at >= now() - interval '{lookback_minutes} minutes'
      union all
      select date_trunc('minute', pa.completed_at at time zone 'Asia/Kolkata'), pa.provider_account, bp.user_id::text
      from payment_attempts pa
      join paytm_billing_plans bp on bp.last_attempt_id = pa.id
      where pa.status='succeeded' and pa.provider_account='paytm_recurring'
        and pa.completed_at >= now() - interval '{lookback_minutes} minutes'
      union all
      select date_trunc('minute', to_timestamp((entity_data->>'created_at')::bigint) at time zone 'Asia/Kolkata'),
             'razorpay_caw_recurring', entity_data->>'customer_id'
      from razorpay_payments
      where (entity_data->>'status') = 'captured'
        and to_timestamp((entity_data->>'created_at')::bigint) >= now() - interval '{lookback_minutes} minutes'
    )
    select minute_ist, provider_account, count(distinct payer_key) as distinct_payers
    from recent_users
    group by 1, 2
    """
    rows = mb_query(sql)
    for r in rows:
        r["minute_ist"] = parse_ts(r["minute_ist"])
    return rows


def fetch_new_mandates_today():
    sql = f"""
    select provider_account, count(*) as n
    from mandates
    where (created_at at time zone 'Asia/Kolkata')::date = (now() at time zone 'Asia/Kolkata')::date
      and status in {ACTIVE_STATUSES_SQL}
    group by 1
    """
    return {r["provider_account"]: int(r["n"]) for r in mb_query(sql)}


def fetch_churned_mandates_today():
    sql = f"""
    select provider_account, count(*) as n
    from mandates
    where (updated_at at time zone 'Asia/Kolkata')::date = (now() at time zone 'Asia/Kolkata')::date
      and (created_at at time zone 'Asia/Kolkata')::date < (now() at time zone 'Asia/Kolkata')::date
      and status in {CHURNED_STATUSES_SQL}
    group by 1
    """
    return {r["provider_account"]: int(r["n"]) for r in mb_query(sql)}


def fetch_minute_churn_resume(lookback_minutes):
    """Mandate status flips into/out of the active set, per minute, all gateways combined."""
    churn_sql = f"""
    select date_trunc('minute', updated_at at time zone 'Asia/Kolkata') as minute_ist, count(*) as n
    from mandates
    where updated_at >= now() - interval '{lookback_minutes} minutes'
      and created_at < updated_at - interval '1 minute'
      and status in {CHURNED_STATUSES_SQL}
    group by 1
    """
    resume_sql = f"""
    select date_trunc('minute', updated_at at time zone 'Asia/Kolkata') as minute_ist, count(*) as n
    from mandates
    where updated_at >= now() - interval '{lookback_minutes} minutes'
      and created_at < updated_at - interval '1 minute'
      and status in {ACTIVE_STATUSES_SQL}
    group by 1
    """
    churn = {parse_ts(r["minute_ist"]): int(r["n"]) for r in mb_query(churn_sql)}
    resume = {parse_ts(r["minute_ist"]): int(r["n"]) for r in mb_query(resume_sql)}
    return churn, resume


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def mrr_row_values(active_subs, avg_mrr):
    mrr = round(avg_mrr * active_subs, 2)
    arr = round(mrr * 12, 2)
    return mrr, arr, round(mrr / FX_RATE, 2), round(arr / FX_RATE, 2)


def build_sheet1_rows(gw_state, state):
    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    new_today = fetch_new_mandates_today()
    churned_today = fetch_churned_mandates_today()

    state = roll_state_if_new_day(state, today_str)
    prev_day = state.get("prev_day", {})

    rows = []
    for label, provider in GATEWAYS:
        gs = gw_state[provider]
        active_subs = gs["active_subscribers"]
        avg_mrr = gs["avg_mrr_per_subscriber"]
        mrr, arr, mrr_usd, arr_usd = mrr_row_values(active_subs, avg_mrr)

        prev = prev_day.get(provider, {})
        prev_mrr = prev.get("mrr")
        prev_active = prev.get("active_subscribers")
        net_mrr_change = round(mrr - prev_mrr, 2) if prev_mrr is not None else 0
        net_sub_change = active_subs - prev_active if prev_active is not None else 0

        new_mrr_added = round(new_today.get(provider, 0) * avg_mrr, 2)
        # churned valued at yesterday's rate if we have it, else today's as a fallback
        churn_rate = prev.get("avg_mrr_per_subscriber", avg_mrr)
        mrr_churned = round(churned_today.get(provider, 0) * churn_rate, 2)

        rows.append([
            today_str, label, mrr, new_mrr_added, mrr_churned, net_mrr_change,
            active_subs, net_sub_change, round(avg_mrr, 2), mrr, arr, mrr_usd, arr_usd,
        ])

        state["today_snapshot"][provider] = {
            "mrr": mrr, "active_subscribers": active_subs, "avg_mrr_per_subscriber": avg_mrr,
        }

    save_state(state)
    return today_str, rows


def build_intraday10min_rows(gw_state, minute_series, distinct_payers, since=None):
    """Builds the last INTRADAY_LOOKBACK_BUCKETS buckets (normal live tick, self-healing the
    same way Minute3Gateway does) or every 10-min bucket from `since` to now (backfill mode,
    e.g. --since 17:00 to redo today's buckets under corrected logic)."""
    now = datetime.now(IST)
    current_bucket_start = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
    first_bucket_start = (
        since.replace(minute=(since.minute // 10) * 10, second=0, microsecond=0)
        if since else current_bucket_start - timedelta(minutes=10 * (INTRADAY_LOOKBACK_BUCKETS - 1))
    )

    per_minute = {p: {} for _, p in GATEWAYS}
    for r in minute_series:
        p = r["provider_account"]
        if p not in per_minute:
            continue
        per_minute[p][r["minute_ist"]] = (int(r["payments"] or 0), float(r["revenue"] or 0))

    payers_by_minute = {}
    for r in distinct_payers:
        m = r["minute_ist"]
        payers_by_minute.setdefault(m, {})[r["provider_account"]] = int(r["distinct_payers"])

    # running cumulative-revenue-today per gateway, advanced minute by minute up to each bucket end.
    # Pre-accumulate everything before the first bucket (cumulative always starts from IST midnight,
    # even in backfill mode where we only rewrite buckets from `since` onward).
    cum_by_gw = {p: 0.0 for _, p in GATEWAYS}
    all_minutes = sorted({m for p in per_minute.values() for m in p})
    minute_idx = 0
    while minute_idx < len(all_minutes) and all_minutes[minute_idx] < first_bucket_start.replace(tzinfo=None):
        m = all_minutes[minute_idx]
        for _, p in GATEWAYS:
            cum_by_gw[p] += per_minute[p].get(m, (0, 0.0))[1]
        minute_idx += 1

    keys, rows = [], []
    bucket_cursor = first_bucket_start
    while bucket_cursor <= current_bucket_start:
        bucket_end = bucket_cursor + timedelta(minutes=10)
        bucket_key = bucket_cursor.strftime("%Y-%m-%d %H:%M")
        bucket_payments = {p: 0 for _, p in GATEWAYS}
        bucket_revenue = {p: 0.0 for _, p in GATEWAYS}

        while minute_idx < len(all_minutes) and all_minutes[minute_idx] < bucket_end.replace(tzinfo=None):
            m = all_minutes[minute_idx]
            for _, p in GATEWAYS:
                payments, revenue = per_minute[p].get(m, (0, 0.0))
                cum_by_gw[p] += revenue
                if m >= bucket_cursor.replace(tzinfo=None):
                    bucket_payments[p] += payments
                    bucket_revenue[p] += revenue
            minute_idx += 1

        bucket_payers = {p: 0 for _, p in GATEWAYS}
        for m, by_gw in payers_by_minute.items():
            if bucket_cursor.replace(tzinfo=None) <= m < bucket_end.replace(tzinfo=None):
                for p, n in by_gw.items():
                    bucket_payers[p] = bucket_payers.get(p, 0) + n

        for label, provider in GATEWAYS:
            gs = gw_state[provider]
            active_subs = gs["active_subscribers"]
            avg_mrr = gs["avg_mrr_per_subscriber"]
            mrr, arr, mrr_usd, arr_usd = mrr_row_values(active_subs, avg_mrr)
            rows.append([
                bucket_key, label,
                bucket_payments[provider], bucket_payers[provider],
                round(bucket_revenue[provider], 2), round(cum_by_gw[provider], 2),
                active_subs, round(avg_mrr, 2), mrr, arr, mrr_usd, arr_usd,
            ])
        keys.append(bucket_key)
        bucket_cursor = bucket_end

    return keys, rows


def build_minute3gateway_rows(gw_state, minute_series, distinct_payers, churn_by_min, resume_by_min,
                               since=None, baseline_mrr_usd=None, baseline_arr_usd=None, active_anchor=None):
    now = datetime.now(IST).replace(second=0, microsecond=0)
    cutoff = since.replace(second=0, microsecond=0) if since else now - timedelta(minutes=LOOKBACK_MINUTES)

    by_minute = {}
    running_cum = 0.0
    # walk the whole day's series in order to build a correct running cumulative total,
    # then only keep the last LOOKBACK_MINUTES minutes for the sheet
    minutes_seen = sorted({r["minute_ist"] for r in minute_series})
    per_minute_totals = {m: {"payments": 0, "revenue": 0.0} for m in minutes_seen}
    for r in minute_series:
        m = r["minute_ist"]
        per_minute_totals[m]["payments"] += int(r["payments"] or 0)
        per_minute_totals[m]["revenue"] += float(r["revenue"] or 0)

    for m in minutes_seen:
        running_cum += per_minute_totals[m]["revenue"]
        if m >= cutoff.replace(tzinfo=None):
            by_minute[m] = {
                "payments": per_minute_totals[m]["payments"],
                "revenue": per_minute_totals[m]["revenue"],
                "cumulative": running_cum,
            }

    payers_by_minute = {}
    for r in distinct_payers:
        m = r["minute_ist"]
        payers_by_minute[m] = payers_by_minute.get(m, 0) + int(r["distinct_payers"])

    # AOV blend stays a live figure from gw_state, unaffected by this change -- only the
    # active-subscriber COUNT feeding into MRR below is now derived recursively (see below).
    live_active_total = sum(gw_state[p]["active_subscribers"] for _, p in GATEWAYS)
    total_mrr_live = sum(gw_state[p]["active_subscribers"] * gw_state[p]["avg_mrr_per_subscriber"] for _, p in GATEWAYS)
    avg_mrr_combined = (total_mrr_live / live_active_total) if live_active_total else 0

    # Active Subscribers (Running): n = (n-1) - churned + resumed, anchored to our own
    # last-remembered value (persisted in arr_sync_state.json) instead of a fresh live COUNT
    # query. This makes the figure immune to the second, unidentified writer corrupting the
    # sheet: we never read our own prior value back FROM the sheet (which could be corrupted),
    # only from local state, so a bad row there can no longer poison our own computation the
    # way re-deriving "current active count" from scratch every run implicitly could not have
    # anyway, but this also removes any reliance on re-querying a count that could in principle
    # drift; it's a pure event-driven ledger from here on. Seeded once from a live count if no
    # anchor exists yet (first run after this change, or state lost).
    cutoff_naive = cutoff.replace(tzinfo=None)
    now_naive = now.replace(tzinfo=None)
    if active_anchor and active_anchor.get("time"):
        anchor_time = parse_ts(active_anchor["time"])
        anchor_active = active_anchor["active"]
    else:
        anchor_time = cutoff_naive - timedelta(minutes=1)
        anchor_active = live_active_total

    active_series = {}
    running_active = anchor_active
    t = anchor_time + timedelta(minutes=1)
    while t <= now_naive:
        running_active += resume_by_min.get(t, 0) - churn_by_min.get(t, 0)
        active_series[t] = running_active
        t += timedelta(minutes=1)
    # roll the anchor forward to the start of THIS run's window, so next run only replays the
    # small gap since then instead of the whole history every time
    new_anchor = {
        "time": cutoff_naive.strftime("%Y-%m-%d %H:%M"),
        "active": active_series.get(cutoff_naive, anchor_active),
    }

    # Minute-over-minute delta, in USD, however small (down to a cent).
    prev_mrr_usd = baseline_mrr_usd
    prev_arr_usd = baseline_arr_usd

    keys, rows = [], []
    minute_cursor = cutoff.replace(second=0, microsecond=0)
    while minute_cursor <= now:
        naive = minute_cursor.replace(tzinfo=None)
        key = minute_cursor.strftime("%Y-%m-%d %H:%M")
        data = by_minute.get(naive, {"payments": 0, "revenue": 0.0, "cumulative": running_cum})
        active_subs_total = active_series.get(naive, anchor_active)
        mrr, arr, mrr_usd, arr_usd = mrr_row_values(active_subs_total, avg_mrr_combined)
        delta_mrr_usd = round(mrr_usd - prev_mrr_usd, 2) if prev_mrr_usd is not None else 0
        delta_arr_usd = round(arr_usd - prev_arr_usd, 2) if prev_arr_usd is not None else 0
        prev_mrr_usd, prev_arr_usd = mrr_usd, arr_usd
        rows.append([
            key,
            data["payments"], payers_by_minute.get(naive, 0), round(data["revenue"], 2),
            round(data["cumulative"], 2), active_subs_total,
            churn_by_min.get(naive, 0), resume_by_min.get(naive, 0),
            round(avg_mrr_combined, 2), mrr, arr, mrr_usd, arr_usd,
            delta_mrr_usd, delta_arr_usd,
        ])
        keys.append(key)
        minute_cursor += timedelta(minutes=1)
    return keys, rows, mrr_usd, arr_usd, new_anchor


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_sheet1_title():
    """The daily tab might be called 'Sheet1' or 'Sheet 1' depending on how it was created —
    match whichever already exists rather than guessing and creating a duplicate."""
    if DRY_RUN:
        return "Sheet1"
    existing = [ws.title for ws in sheets_client().worksheets()]
    for title in existing:
        if title.strip().lower().replace(" ", "") == "sheet1":
            return title
    return "Sheet1"  # Google Sheets' default name for a brand-new tab


def parse_since_arg():
    """--since HH:MM triggers backfill mode: redo every bucket from that time (today, IST) to
    now, instead of just the current live tick. Used for one-off corrections like a logic fix."""
    if "--since" not in sys.argv:
        return None
    val = sys.argv[sys.argv.index("--since") + 1]
    hh, mm = val.split(":")
    return datetime.now(IST).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def main():
    _lock = acquire_lock()
    run_start = datetime.now(IST)
    print(f"[{run_start.isoformat()}] run start")
    since = parse_since_arg()
    lookback = LOOKBACK_MINUTES
    if since:
        lookback = int((datetime.now(IST) - since).total_seconds() // 60) + 1
        print(f"[backfill mode] redoing all buckets since {since.strftime('%H:%M')} IST "
              f"({lookback} minutes)")

    gw_state = fetch_gateway_state()
    minute_series = fetch_today_minute_series()
    distinct_payers = fetch_recent_distinct_payers(lookback)

    # Sheet1 (daily) -- state loaded here (not after the churn/resume fetch below) specifically
    # so the persisted active-subscriber anchor's age is known before deciding how far back to
    # query churn/resume.
    state = load_state()
    today_str, sheet1_rows = build_sheet1_rows(gw_state, state)
    sheet1_title = resolve_sheet1_title()
    upsert_rows(sheet1_title, "Date", SHEET1_HEADERS, [today_str], sheet1_rows)

    # Intraday10min (current 10-min bucket, or every bucket since `since` in backfill mode)
    bucket_keys, intraday_rows = build_intraday10min_rows(gw_state, minute_series, distinct_payers, since=since)
    upsert_rows("Intraday10min", "Time (10-min bucket start)", INTRADAY_HEADERS, bucket_keys, intraday_rows)

    # Minute3Gateway (last LOOKBACK_MINUTES minutes, or since `since` in backfill mode).
    # The Δ MRR/ARR (USD) columns need to know what the previous run last wrote, since gw_state
    # (and therefore mrr_usd/arr_usd) is fixed for this whole run -- persisted across runs the
    # same way Sheet1's day-over-day baseline is.
    minute_baseline = state.get("minute_baseline", {})
    active_anchor = state.get("minute_active_anchor")

    # The active-subscriber ledger (active_series in build_minute3gateway_rows) walks forward
    # from the anchor's timestamp to now, applying churn/resume deltas -- but fetch_minute_churn_
    # resume() below only ever fetched a fixed LOOKBACK_MINUTES window. If the anchor is OLDER
    # than that (e.g. after any outage longer than an hour -- a real, repeated occurrence), every
    # churn/resume event between the anchor and (now - LOOKBACK_MINUTES) was invisible to that
    # query and silently treated as "no change," permanently biasing the ledger by however much
    # activity happened in the gap. Widening the churn/resume fetch to always cover from the
    # anchor's actual age (with a small safety buffer) means no outage, however long, can cause
    # this again -- this is a data-completeness fix, not a change to any sheet formula.
    churn_resume_lookback = lookback
    if active_anchor and active_anchor.get("time"):
        anchor_age_minutes = int((datetime.now(IST) - parse_ts(active_anchor["time"]).replace(tzinfo=IST)).total_seconds() // 60) + 5
        churn_resume_lookback = max(lookback, anchor_age_minutes)
    churn_by_min, resume_by_min = fetch_minute_churn_resume(churn_resume_lookback)

    minute_keys, minute_rows, new_mrr_usd, new_arr_usd, new_active_anchor = build_minute3gateway_rows(
        gw_state, minute_series, distinct_payers, churn_by_min, resume_by_min, since=since,
        baseline_mrr_usd=minute_baseline.get("mrr_usd"), baseline_arr_usd=minute_baseline.get("arr_usd"),
        active_anchor=active_anchor,
    )
    upsert_rows("Minute3Gateway", "Time (1-min)", MINUTE_HEADERS, minute_keys, minute_rows)
    if not DRY_RUN:
        mg_ws = get_or_create_worksheet("Minute3Gateway", MINUTE_HEADERS)
        dedupe_minute_rows(mg_ws, minute_keys, minute_rows)
    state["minute_baseline"] = {"mrr_usd": new_mrr_usd, "arr_usd": new_arr_usd}
    state["minute_active_anchor"] = new_active_anchor
    save_state(state)

    # retention cleanup (Sheet1 daily history is kept forever, no trimming) —
    # only run near the top of the hour to limit Sheets API traffic, skip entirely in backfill mode.
    # Must happen BEFORE apply_minute3gateway_active_formula(): deleteDimension shifts rows up, and
    # the row right after the deleted range keeps its OLD absolute reference (now pointing at a row
    # that no longer exists), which shows as #REF! and cascades down the whole formula chain below
    # it. Rebuilding the formula column last, from the sheet's final post-trim row positions, avoids
    # ever leaving a stale reference behind.
    if not since and datetime.now(IST).minute == 0:
        trim_old_rows("Intraday10min", "Time (10-min bucket start)", INTRADAY_RETENTION_DAYS)
        trim_old_rows("Minute3Gateway", "Time (1-min)", MINUTE_RETENTION_DAYS)

    if not DRY_RUN:
        last_row = apply_minute3gateway_active_formula(mg_ws)
        if last_row is not None:
            verify_and_backfill_formula_columns(mg_ws, last_row)

    print(f"[{datetime.now(IST).isoformat()}] synced Sheet1={today_str} "
          f"Intraday10min={len(bucket_keys)} bucket(s) Minute3Gateway=last {len(minute_keys)} min")


if __name__ == "__main__":
    main()
