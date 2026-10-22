"""
scheduler_service (purview_scan_trigger_validation)
===================================================
One check per scan per month, at end time:

    end time = scheduled start + 1 hour + duration_minutes

At that moment the check asks Purview for the scan's latest run, prints the
status to the console, saves the outcome to scheduler_results.json, and queues
an alert email (via scripts/send_email.py) when the result is not a clean pass.
Successful runs are recorded silently.

Run the scheduler from the repository root:
    python -m tests.purview_scan_trigger_validation.scheduler_service

Build the HTML report from saved results and Purview scans:
    python -m tests.purview_scan_trigger_validation.scheduler_service --report

Settings (.env in the repository root)
    PURVIEW_ACCOUNT_NAME, AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET   required
    PURVIEW_COLLECTION (or ENVIRONMENT)   parent collection, default Prod
    PURVIEW_COLLECTION_COMMERCIAL         collection, default Commercial
    EXPECTED_SCHEDULE_CSV                 default <this folder>/expected_schedule.csv
    SCAN_TOLERANCE_MINUTES                default 60 (match window around the scheduled start)
    SCHEDULER_RESULTS_FILE                default reports/scheduler_results.json
    SCAN_REPORT_DAYS                      default 30
    MAX_WORKERS                           default 4 (scans checked in parallel)
    SMTP_HOST, SMTP_FROM, ALERT_TO        email (see scripts/send_email.py for SMTP_PORT/USER/PASSWORD)
    DEVOPS_SLACK_WEBHOOK_URL              optional Slack fallback after failed emails
"""

import json
import os
import sys
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from . import scan_report_builder as srb
from . import scan_validator as sv
from .purview_scan_client import PurviewScanClient

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
REPORTS_DIR = REPO_ROOT / "reports"
REQUIRED_ENV = ["PURVIEW_ACCOUNT_NAME", "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"]
BUFFER_MINUTES = 60          # the 1-hour buffer added after the expected duration
SCAN_CACHE_SECONDS = 300     # reuse one Purview scan list for 5 minutes

_scheduler: Optional[BlockingScheduler] = None
_state_lock = threading.Lock()
_scan_lock = threading.Lock()
_scan_cache: Dict[str, Any] = {"at": None, "scans": None}


@dataclass(frozen=True)
class Settings:
    default_tolerance_minutes: int
    expected_csv: Path
    results_file: Path
    parent_collection: str
    collection: str
    max_workers: int
    report_days: int


def load_settings() -> Settings:
    return Settings(
        default_tolerance_minutes=int(os.environ.get("SCAN_TOLERANCE_MINUTES", "60")),
        expected_csv=Path(os.environ.get("EXPECTED_SCHEDULE_CSV", HERE / "expected_schedule.csv")),
        results_file=Path(os.environ.get("SCHEDULER_RESULTS_FILE", REPORTS_DIR / "scheduler_results.json")),
        parent_collection=os.environ.get("PURVIEW_COLLECTION") or os.environ.get("ENVIRONMENT") or "Prod",
        collection=os.environ.get("PURVIEW_COLLECTION_COMMERCIAL", "Commercial"),
        max_workers=int(os.environ.get("MAX_WORKERS", "4")),
        report_days=int(os.environ.get("SCAN_REPORT_DAYS", "30")),
    )


# ── console output ───────────────────────────────────────────────────────

def log(message: str) -> None:
    """Print a line with the real UTC time it was printed."""
    print(f"{datetime.now(timezone.utc):%d/%m/%Y %H:%M:%S} UTC  {message}", flush=True)


# ── settings from .env ───────────────────────────────────────────────────

def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from .env into os.environ. Existing variables are kept."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def check_required_env() -> None:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing settings in .env: " + ", ".join(missing))


# ── results file: the restart record ─────────────────────────────────────

def state_key(exp: Dict[str, Any], slot: datetime) -> str:
    return f"{exp['scan_name']}|{slot.isoformat()}"


def load_state(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def record(s: Settings, exp: Dict[str, Any], slot: datetime, payload: Dict[str, Any]) -> None:
    """Save the outcome before anything else. Written atomically, under a lock."""
    with _state_lock:
        state = load_state(s.results_file)
        entry = state.setdefault(
            state_key(exp, slot),
            {"scan_name": exp["scan_name"], "expected_time_utc": slot.isoformat()},
        )
        entry["trigger"] = payload
        s.results_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = s.results_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(s.results_file)


# ── Purview access ───────────────────────────────────────────────────────

def fetch_scans(s: Settings) -> List[Dict[str, Any]]:
    """Purview scan list, reused for 5 minutes so checks don't each download everything."""
    with _scan_lock:
        now = datetime.now(timezone.utc)
        at = _scan_cache["at"]
        if at and (now - at).total_seconds() < SCAN_CACHE_SECONDS:
            return _scan_cache["scans"]

        client = PurviewScanClient(
            account_name=os.environ["PURVIEW_ACCOUNT_NAME"],
            tenant_id=os.environ["AZURE_TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
        )
        try:
            client.check_connection()
            scans = srb.collect_scans(client, collection=s.collection, parent=s.parent_collection)
        finally:
            client.close()

        _scan_cache.update(at=now, scans=scans)
        return scans


# ── schedule: end time and monthly cron ──────────────────────────────────

def check_time(exp: Dict[str, Any]) -> Tuple[int, int]:
    """End time = scheduled start + 1 hour buffer + duration, as (hour, minute) UTC."""
    hh, mm, _ = sv.parse_clock(exp["time"])
    total = hh * 60 + mm + BUFFER_MINUTES + int(exp["duration_minutes"])
    if total >= 24 * 60:
        raise ValueError("check time passes midnight; move the start time earlier")
    return divmod(total, 60)


def cron_for(exp: Dict[str, Any]) -> CronTrigger:
    """Fires once a month at the row's check time."""
    hour, minute = check_time(exp)
    return CronTrigger(day=sv.day_of_month(exp["day"]), hour=hour, minute=minute, timezone=timezone.utc)


def build_plan(rows: List[Dict[str, Any]]) -> int:
    """Books one monthly check per recurring row. Returns the number booked."""
    booked = 0
    for exp in rows:
        name = exp["scan_name"]
        if exp["expected_run_time"]:
            log(f"[BOOK] {name} skipped: one-off date rows are not used in the monthly design")
            continue
        try:
            trigger = cron_for(exp)
        except ValueError as exc:
            log(f"[BOOK] {name} skipped: {exc}")
            continue
        _scheduler.add_job(
            validation_check, trigger, args=[exp],
            id=f"check::{exp['line']}", replace_existing=True,
        )
        hour, minute = check_time(exp)
        log(f"[BOOK] {name} -> {exp['day']} of each month at {hour:02d}:{minute:02d} UTC")
        booked += 1
    return booked


def notify(name: str, outcome: str, run_status: str, reason: str, slot: datetime) -> None:
    """Queue an alert email in the background; never blocks or breaks the check."""
    try:
        scripts_dir = str(REPO_ROOT / "scripts")
        if scripts_dir not in sys.path:
            sys.path.append(scripts_dir)
        from send_email import send_email_async

        send_email_async(
            f"Purview scan did not run: {name}" if outcome == "Did Not Run"
            else f"Purview scan alert ({outcome}): {name}",
            f"<p><b>Scan:</b> {name}<br>"
            f"<b>Expected start:</b> {srb.fmt_utc(slot)}<br>"
            f"<b>Outcome:</b> {outcome}<br>"
            f"<b>Run status:</b> {run_status or '-'}<br>"
            f"<b>Reason:</b> {reason}</p>",
        )
        log(f"[EMAIL] {name} alert queued")
    except Exception as exc:
        log(f"[EMAIL] {name} alert not queued: {type(exc).__name__}: {exc}")


# ── the check ────────────────────────────────────────────────────────────

def validation_check(exp: Dict[str, Any]) -> None:
    """Runs once a month at end time. Prints the status and saves the outcome."""
    s = load_settings()
    name = exp["scan_name"]
    now = datetime.now(timezone.utc)
    slot, _ = sv.due_slot(exp, now)          # this month's scheduled start

    log(f"[CHECK] {name} started, expected start {srb.fmt_utc(slot)}")

    try:
        scans = fetch_scans(s)
    except Exception as exc:
        reason = f"Purview unavailable: {type(exc).__name__}: {exc}"
        record(s, exp, slot, {
            "outcome": "Check Failed", "run_status": "", "reason": reason,
            "checked_at": now.isoformat(),
        })
        log(f"[CHECK] {name} -> Check Failed | status: - | {reason}")
        log(f"[RECORD] {name} saved to {s.results_file.name}")
        notify(name, "Check Failed", "", reason, slot)
        return

    log(f"[CHECK] {name} Purview scans fetched: {len(scans)}")
    res = sv.validate([exp], scans, slot)[0]
    found = res["category"] in ("passed", "warning", "failed")
    outcome = "Run Found" if found else "Did Not Run"
    run_status = res["run_status"] if found else ""

    record(s, exp, slot, {
        "outcome": outcome,
        "run_status": run_status,
        "category": res["category"],
        "actual_run_time": res["actual_run_time"],
        "reason": res["reason"],
        "checked_at": now.isoformat(),
    })
    log(f"[CHECK] {name} -> {outcome} | status: {run_status or '-'} | {res['reason']}")
    log(f"[RECORD] {name} saved to {s.results_file.name}")
    if res["category"] != "passed":
        notify(name, outcome, run_status, res["reason"], slot)


# ── run continuously ─────────────────────────────────────────────────────

def main() -> None:
    global _scheduler
    load_dotenv(REPO_ROOT / ".env")
    check_required_env()
    s = load_settings()

    log("[STARTUP] Loading settings and schedule")
    rows = sv.read_expected(str(s.expected_csv), default_tolerance=s.default_tolerance_minutes)

    _scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=s.max_workers)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
        timezone=timezone.utc,
    )
    booked = build_plan(rows)
    if booked == 0:
        log("[STARTUP] Nothing to check. Exiting.")
        return

    log(f"[STARTUP] {booked} scan(s) booked. Waiting for check times. Ctrl+C to stop.")
    try:
        _scheduler.start()          # blocks; runs continuously
    except (KeyboardInterrupt, SystemExit):
        log("[STOP] Stopped.")
    finally:
        try:
            from send_email import shutdown_email_sender
            shutdown_email_sender()     # flush any queued alert emails
        except ImportError:
            pass


# ── validation report ────────────────────────────────────────────────────

TRIGGER_MAP = {
    "Did Not Run": ("FAIL", "did_not_run"),
    "Check Failed": ("FAIL", "trigger_failed"),
}

RUN_MAP = {
    "passed": ("PASS", "passed"),
    "warning": ("WARN", "warning"),
    "failed": ("FAIL", "failed"),
}


def _fmt_iso(value: str) -> str:
    return srb.fmt_utc(datetime.fromisoformat(value)) if value else ""


def to_validation_row(
    entry: Dict[str, Any],
    csv_lines: Dict[str, int],
    scans_by_name: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    trig = entry.get("trigger") or {}
    outcome = trig.get("outcome", "")
    name = entry.get("scan_name", "")

    if outcome == "Run Found":
        result, category = RUN_MAP.get(trig.get("category", ""), ("WARN", "warning"))
    else:
        result, category = TRIGGER_MAP.get(outcome, ("WARN", "warning"))

    scan = scans_by_name.get(name.lower())
    expected = entry.get("expected_time_utc", "")
    return {
        "scan_name": name,
        "result": result,
        "category": category,
        "reason": trig.get("reason", ""),
        "expected_run_time": _fmt_iso(expected),
        "actual_run_time": trig.get("actual_run_time", ""),
        "delay_min": "",
        "run_status": trig.get("run_status", ""),
        "triggered_by": "",
        "expected_schedule": "Monthly",
        "last_scan_time": scan["last_scan_time"] if scan else "",
        "csv_line": csv_lines.get(name.lower(), ""),
    }


def load_scans_for_report(s: Settings) -> List[Dict[str, Any]]:
    try:
        return fetch_scans(s)
    except Exception as exc:
        log(f"[REPORT] Purview unavailable, Overview and Scans tabs will be empty: "
            f"{type(exc).__name__}: {exc}")
        return []


def load_test_results(path: Path) -> List[Dict[str, Any]]:
    """Read pytest's JUnit XML into the rows the report's test card expects."""
    if not path.exists():
        return []
    results = []
    for case in ET.parse(path).iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        results.append({
            "name": f"{case.get('classname', '')}::{case.get('name', '')}",
            "outcome": outcome,
            "duration": round(float(case.get("time", 0) or 0), 2),
        })
    return results


def build_report() -> None:
    load_dotenv(REPO_ROOT / ".env")
    s = load_settings()

    if not s.results_file.exists():
        raise SystemExit(f"No results found at {s.results_file}. Run the scheduler first.")
    state = load_state(s.results_file)

    expected = sv.read_expected(str(s.expected_csv), default_tolerance=s.default_tolerance_minutes)
    csv_lines: Dict[str, int] = {}
    for exp in expected:
        csv_lines.setdefault(exp["scan_name"].lower(), exp["line"])

    scans = load_scans_for_report(s)
    scans_by_name: Dict[str, Dict[str, Any]] = {}
    for sc in scans:
        scans_by_name.setdefault(sc["scan_name"].lower(), sc)

    validation = sorted(
        (to_validation_row(entry, csv_lines, scans_by_name) for entry in state.values()),
        key=lambda r: (r["scan_name"], r["expected_run_time"]),
    )
    rows = srb.build_scan_report(scans, s.report_days, datetime.now(timezone.utc))

    meta = {
        "environment": os.environ.get("ENVIRONMENT", ""),
        "scope": f"{s.parent_collection} > {s.collection}",
        "account": os.environ.get("PURVIEW_ACCOUNT_NAME", ""),
        "test_results": load_test_results(REPORTS_DIR / "pytest_results.xml"),
        "start_time": None,
        "end_time": None,
    }
    index_path = srb.write_html_report(rows=rows, days=s.report_days, meta=meta, validation=validation)
    print(f"Report written: {index_path}  ({len(validation)} expected run(s), {len(scans)} scan(s))")


if __name__ == "__main__":
    if "--report" in sys.argv:
        build_report()
    else:
        main()