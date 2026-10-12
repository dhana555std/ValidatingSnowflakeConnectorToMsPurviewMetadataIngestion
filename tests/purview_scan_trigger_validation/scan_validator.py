"""
scan_validator
==============
Validates actual Purview scan runs against the expected schedule CSV.
 
For every row of the expected schedule:
    1. the row itself is valid                         else FAIL  (invalid_csv)
    2. work out the expected run time that is already due
       (exact date/time, or the latest Daily/Weekly/Monthly slot)   not due yet → WARN (not_due)
    3. the scan exists in Purview                      else FAIL  (not_found)
    4. the scan has an enabled schedule                else FAIL  (no_schedule)
    5. a run started within ± tolerance of that time   else FAIL  (missed)
    6. that run's status                               Completed        → PASS (passed)
                                                       With exceptions  → WARN (warning)
                                                       Failed / other   → FAIL (failed)
 
Expected schedule CSV — one row per expected run; each row uses ONE of two styles:
 
    scan_name,expected_run_time,frequency,day,time,timezone,tolerance_minutes
    SCAN_A,,Monthly,1st,12:00 AM,UTC,60          ← repeats: 1st of every month, 12:00 AM UTC
    SCAN_B,01/10/2026 00:00:00 UTC,,,,,60         ← one exact date/time
 
    scan_name          scan name as in Purview (header may also be: scan, database, db)
    expected_run_time  exact run time, DD/MM/YYYY HH:MM:SS UTC
    frequency          Daily | Weekly | Monthly   ("Every Month" etc. also accepted)
    day                Weekly: Wednesday (or Monday;Thursday) · Monthly: 1 / 1st / 2nd … · Daily: blank
    time               00:00 (24h) or 12:00 AM
    timezone           UTC (default), IST, or an offset like +05:30
    tolerance_minutes  how early/late a run may start and still count (default 60)
 
All comparisons are done in UTC.
"""
 
import calendar
import csv
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
 
from .scan_report_builder import COMPLETED, EXCEPTIONS, fmt_utc
 
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
 
VALIDATION_COLUMNS = ["scan_name", "result", "reason", "expected_run_time", "actual_run_time",
                      "delay_min", "run_status", "triggered_by", "last_scan_time", "last_run_status"]
 
SCAN_NAME_HEADERS = ("scan_name", "scan", "database", "db")
EXPECTED_TIME_HEADERS = ("expected_run_time", "scheduled_time", "expected_time")
WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
NAMED_TZ = {"utc": timezone.utc, "gmt": timezone.utc, "z": timezone.utc,
            "ist": timezone(timedelta(hours=5, minutes=30))}
 
 
# ── parsing helpers ───────────────────────────────────────────────────────────
 
def parse_tz(token: str) -> Optional[timezone]:
    token = (token or "").strip()
    if not token:
        return None
    if token.lower() in NAMED_TZ:
        return NAMED_TZ[token.lower()]
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2}):?(\d{2})?", token, re.I)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
    raise ValueError(f"unknown time zone '{token}' (use UTC, IST or +05:30)")
 
 
def split_tz(text: str) -> Tuple[str, Optional[timezone]]:
    """'12:00 AM  UTC' → ('12:00 AM', utc).  Text without a zone → (text, None)."""
    m = re.match(r"^(.*?)\s*(UTC|GMT|IST|Z|(?:UTC|GMT)?[+-]\d{1,2}:?\d{0,2})\s*$", text.strip(), re.I)
    return (m.group(1), parse_tz(m.group(2))) if m else (text.strip(), None)
 
 
def parse_clock(text: str) -> Tuple[int, int, Optional[timezone]]:
    """'00:00', '5:30', '12:00 AM', '06:00 PM UTC' → (hour, minute, tz or None)."""
    body, tz = split_tz(text)
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?::\d{2})?\s*(AM|PM)?", body.strip(), re.I)
    if not m:
        raise ValueError(f"invalid time '{text}' (use 00:00 or 12:00 AM)")
    hh, mm, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ampm:
        if not 1 <= hh <= 12:
            raise ValueError(f"invalid time '{text}'")
        hh = (0 if hh == 12 else hh) + (12 if ampm == "PM" else 0)
    if hh > 23 or mm > 59:
        raise ValueError(f"invalid time '{text}'")
    return hh, mm, tz
 
 
def parse_expected_datetime(text: str, default_tz: timezone = timezone.utc) -> datetime:
    """'01/10/2026 00:00:00 UTC' (DD/MM/YYYY) or ISO → aware datetime."""
    body, tz = split_tz(text)
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %I:%M:%S %p",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(body.strip(), fmt).replace(tzinfo=tz or default_tz)
        except ValueError:
            continue
    raise ValueError(f"invalid expected_run_time '{text}' (use DD/MM/YYYY HH:MM:SS UTC)")
 
 
def normalise_frequency(text: str) -> str:
    t = text.strip().lower()
    for key in ("month", "week", "daily", "day"):
        if key in t:
            return {"month": "monthly", "week": "weekly", "daily": "daily", "day": "daily"}[key]
    raise ValueError(f"unknown frequency '{text}' (use Daily, Weekly or Monthly)")
 
 
def day_of_month(text: str) -> int:
    """'1', '1st', '2nd', '23rd' → int."""
    m = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", text.strip().lower())
    if not m or not 1 <= int(m.group(1)) <= 31:
        raise ValueError(f"invalid day of month '{text}' (use 1-31, e.g. 1 or 1st)")
    return int(m.group(1))
 
 
# ── expected schedule ─────────────────────────────────────────────────────────
 
def read_expected(path: str, default_tolerance: int) -> List[Dict[str, Any]]:
    """Read the expected schedule CSV. Bad values are reported per row by validate()."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {h.strip().lower(): h for h in (reader.fieldnames or []) if h}
        name_col = next((headers[h] for h in SCAN_NAME_HEADERS if h in headers), None)
        time_col = next((headers[h] for h in EXPECTED_TIME_HEADERS if h in headers), None)
        if not name_col or not (time_col or ("frequency" in headers and "time" in headers)):
            raise ValueError(
                "expected schedule CSV needs a scan_name column and either expected_run_time "
                f"or frequency + time columns. Found: {', '.join(reader.fieldnames or [])}")
 
        def cell(row: Dict[str, str], col: Optional[str]) -> str:
            return (row.get(col, "") or "").strip() if col else ""
 
        rows = []
        for line, r in enumerate(reader, start=2):
            name = cell(r, name_col)
            if not name:
                continue
            tol = cell(r, headers.get("tolerance_minutes"))
            rows.append({
                "line": line,
                "scan_name": name,
                "expected_run_time": cell(r, time_col),
                "frequency": cell(r, headers.get("frequency")),
                "day": cell(r, headers.get("day")),
                "time": cell(r, headers.get("time")),
                "timezone": cell(r, headers.get("timezone")),
                "tolerance_minutes": int(tol) if tol.isdigit() else default_tolerance,
            })
        return rows
 
 
def due_slot(exp: Dict[str, Any], now: datetime) -> Tuple[Optional[datetime], str]:
    """Latest expected run that is already due (started at least `tolerance` ago).
    Returns (slot in UTC, or None if not due yet; readable description). Raises ValueError."""
    tol = timedelta(minutes=exp["tolerance_minutes"])
    row_tz = parse_tz(exp["timezone"]) or timezone.utc
 
    if exp["expected_run_time"]:
        slot = parse_expected_datetime(exp["expected_run_time"], row_tz).astimezone(timezone.utc)
        return (slot if slot <= now - tol else None), f"Once at {fmt_utc(slot)}"
 
    freq = normalise_frequency(exp["frequency"])
    hh, mm, clock_tz = parse_clock(exp["time"])
    tz = clock_tz or row_tz
    weekdays, month_day = set(), 0
    if freq == "weekly":
        names = [w.strip().lower() for w in exp["day"].replace(",", ";").split(";") if w.strip()]
        if not names or any(n not in WEEKDAYS for n in names):
            raise ValueError(f"invalid weekday '{exp['day']}' (use e.g. Wednesday or Monday;Thursday)")
        weekdays = {WEEKDAYS[n] for n in names}
    elif freq == "monthly":
        month_day = day_of_month(exp["day"])
 
    label = {"daily": "Daily", "weekly": f"Weekly on {exp['day']}",
             "monthly": f"Monthly on day {month_day}"}[freq]
    ref = (now - tol).astimezone(tz)
    for back in range(0, 62):
        d = (ref - timedelta(days=back)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if d > ref:
            continue
        if (freq == "daily"
                or (freq == "weekly" and d.weekday() in weekdays)
                or (freq == "monthly" and d.day == min(month_day, calendar.monthrange(d.year, d.month)[1]))):
            return d.astimezone(timezone.utc), f"{label} at {hh:02d}:{mm:02d} {d.tzname() or ''}".strip()
    return None, label
 
 
# ── validation ────────────────────────────────────────────────────────────────
 
def validate(expected: List[Dict[str, Any]], scans: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """One result per expected row; `category` is what the tests filter on."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for s in scans:
        by_name.setdefault(s["scan_name"].lower(), s)
 
    results = []
    for exp in expected:
        out: Dict[str, Any] = {
            "scan_name": exp["scan_name"], "csv_line": exp["line"],
            "expected_schedule": "", "expected_run_time": "", "actual_run_time": "", "delay_min": "",
            "run_status": "", "triggered_by": "", "last_scan_time": "", "last_run_status": "",
            "purview_schedule": "", "purview_schedule_detail": "", "tolerance_min": exp["tolerance_minutes"],
        }
 
        def done(result: str, category: str, reason: str) -> None:
            out.update(result=result, category=category, reason=reason)
            results.append(out)
 
        try:
            slot, desc = due_slot(exp, now)
        except ValueError as exc:
            done(FAIL, "invalid_csv", f"Invalid expected schedule on CSV line {exp['line']}: {exc}")
            continue
        out["expected_schedule"] = desc
 
        scan = by_name.get(exp["scan_name"].lower())
        if scan:
            out.update(last_scan_time=scan["last_scan_time"], last_run_status=scan["last_run_status"],
                       purview_schedule=scan["schedule"], purview_schedule_detail=scan["schedule_detail"])
        if slot is None:
            done(WARN, "not_due", "Expected run time not reached yet")
            continue
        out["expected_run_time"] = fmt_utc(slot)
 
        if not scan:
            done(FAIL, "not_found", "Scan not found in Purview")
            continue
        if scan["schedule"] != "Schedule":
            done(FAIL, "no_schedule", f"No enabled schedule in Purview ({scan['schedule']})")
            continue
 
        tol = timedelta(minutes=exp["tolerance_minutes"])
        near = [r for r in scan["runs"] if r["start"] and abs(r["start"] - slot) <= tol]
        if not near:
            done(FAIL, "missed", f"No run within {exp['tolerance_minutes']} min of {out['expected_run_time']} "
                                 f"(last scan: {scan['last_scan_time'] or 'never'})")
            continue
 
        near.sort(key=lambda r: (r["triggered_by"] != "Schedule", abs(r["start"] - slot)))
        run = near[0]
        delay = round((run["start"] - slot).total_seconds() / 60)
        out.update(actual_run_time=fmt_utc(run["start"]), delay_min=delay,
                   run_status=run["status"], triggered_by=run["triggered_by"])
        timing = "on time" if abs(delay) <= 5 else f"{abs(delay)} min {'late' if delay > 0 else 'early'}"
        manual = " (manual run)" if run["triggered_by"] == "Manual" else ""
        if run["status"] == COMPLETED:
            done(PASS, "passed", f"Ran {timing}{manual} and completed")
        elif run["status"] in (EXCEPTIONS, "In progress", "Queued"):
            done(WARN, "warning", f"Ran {timing}{manual}; status {run['status']}")
        else:
            err = f": {run['error']}" if run["error"] else ""
            done(FAIL, "failed", f"Ran {timing}{manual} but {run['status']}{err}")
 
    order = {FAIL: 0, WARN: 1, PASS: 2}
    results.sort(key=lambda r: (order[r["result"]], r["scan_name"], r["expected_run_time"]))
    return results