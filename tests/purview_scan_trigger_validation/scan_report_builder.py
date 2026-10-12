"""
scan_report_builder
===================
Builds the Purview scan trigger report:
 
1. collect_scans()        Purview API data → one dict per scan (portal wording, all runs)
2. build_scan_report()    one row per run in the last N days, scan totals on every row
 
All times are handled as timezone-aware UTC and shown as  DD/MM/YYYY HH:MM:SS UTC.
"""
 
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from utils.paths import PURVIEW_SCAN_TRIGGER_VALIDATION
from utils.report_renderer import badge, render_report, table_spec
 
TIME_FMT = "%d/%m/%Y %H:%M:%S UTC"
 
COMPLETED = "Completed"
EXCEPTIONS = "Completed with exceptions"
FAILED = "Failed"
 
SCAN_REPORT_COLUMNS = ["scan_name", "last_run_status", "schedule", "scan_rule_set", "last_scan_time",
                       "collection"]
 
 
# ── time helpers ──────────────────────────────────────────────────────────────
 
def parse_api_ts(v: Optional[str]) -> Optional[datetime]:
    """Purview timestamp text ('2026-10-02T05:30:22.6764952Z') → aware UTC datetime."""
    if not v:
        return None
    try:
        v = re.sub(r"\.(\d{6})\d+", r".\1", str(v).replace("Z", "+00:00"))
        ts = datetime.fromisoformat(v)
        return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except ValueError:
        return None
 
 
def fmt_utc(ts: Optional[datetime]) -> str:
    return ts.astimezone(timezone.utc).strftime(TIME_FMT) if ts else ""
 
 
# ── Purview API data → portal wording ─────────────────────────────────────────
 
def portal_status(run: Optional[Dict[str, Any]]) -> str:
    """API run → the portal's 'Last run status' wording."""
    if not run:
        return "Never run"
    status = run.get("status") or ""
    if status != "Succeeded":
        return {"Failed": FAILED, "Canceled": "Canceled", "InProgress": "In progress",
                "Accepted": "Queued", "TransientFailure": "Transient failure"}.get(status, status or "Unknown")
    discovery = (run.get("discoveryExecutionDetails") or {}).get("status") or ""
    ingestion = (run.get("ingestionExecutionDetails") or {}).get("status") or ""
    exc_map = (run.get("diagnostics") or {}).get("exceptionCountMap") or {}
    if (discovery in ("CompletedWithExceptions", "CompleteWithWarning")
            or ingestion in ("PartialSucceeded", "Failed") or any(exc_map.values())):
        return EXCEPTIONS
    return COMPLETED
 
 
def run_trigger(run: Dict[str, Any]) -> str:
    return "Manual" if (run.get("runType") or "").lower() == "manual" else "Schedule"
 
 
def schedule_type(trig: Optional[Dict[str, Any]]) -> str:
    if not trig:
        return "Manual"
    state = (trig.get("properties") or {}).get("state", "Enabled")
    return "Schedule" if state == "Enabled" else "Manual (schedule disabled)"
 
 
def schedule_detail(trig: Optional[Dict[str, Any]]) -> str:
    if not trig:
        return ""
    rec = (trig.get("properties") or {}).get("recurrence") or {}
    sch = rec.get("schedule") or {}
    parts = [f"Every {rec.get('interval', 1)} {rec.get('frequency', '?')}"]
    if sch.get("weekDays"):
        parts.append("on " + ", ".join(sch["weekDays"]))
    if sch.get("monthDays"):
        parts.append("on day " + ", ".join(map(str, sch["monthDays"])))
    if sch.get("hours"):
        times = [f"{h:02d}:{m:02d}" for h in sch["hours"] for m in (sch.get("minutes") or [0])]
        parts.append("at " + ", ".join(times))
    if rec.get("timeZone"):
        parts.append(f"({rec['timeZone']})")
    return " ".join(parts)
 
 
def collection_path(ref: str, cols: Dict[str, Dict[str, str]]) -> str:
    names, seen = [], set()
    while ref and ref in cols and ref not in seen:
        seen.add(ref)
        names.append(cols[ref]["name"])
        ref = cols[ref]["parent"]
    if len(names) > 1:
        names = names[:-1]          # drop the root (account-level) collection
    return " > ".join(reversed(names))
 
 
def collect_scans(client: Any, collection: str, parent: str, kind: str = "Snowflake") -> List[Dict[str, Any]]:
    """Every scan in parent > collection with its schedule and full run history (newest first)."""
    cols = client.collections()
 
    def in_scope(ref: str) -> bool:
        if not collection:
            return True
        c = cols.get(ref)
        if not c or c["name"] != collection:
            return False
        return not parent or cols.get(c["parent"], {}).get("name") == parent
 
    scans = []
    for ds in client.datasources():
        if kind and str(ds.get("kind", "")).lower() != kind.lower():
            continue
        ds_name = ds["name"]
        ds_coll = ((ds.get("properties") or {}).get("collection") or {}).get("referenceName", "")
        for scan in client.scans(ds_name):
            props = scan.get("properties") or {}
            scan_coll = (props.get("collection") or {}).get("referenceName") or ds_coll
            if not in_scope(scan_coll):
                continue
            trig = client.trigger(ds_name, scan["name"])
            runs = []
            for r in client.runs(ds_name, scan["name"]):
                start, end = parse_api_ts(r.get("startTime")), parse_api_ts(r.get("endTime"))
                assets = ((r.get("discoveryExecutionDetails") or {}).get("statistics") or {}).get("assets") or {}
                runs.append({
                    "start": start, "end": end,
                    "status": portal_status(r),
                    "triggered_by": run_trigger(r),
                    "run_type_raw": r.get("runType"),
                    "scan_level": r.get("scanLevelType"),
                    "assets_discovered": assets.get("discovered", ""),
                    "ingestion_status": (r.get("ingestionExecutionDetails") or {}).get("status") or "",
                    "error": r.get("errorMessage") or (r.get("error") or {}).get("message") or "",
                    "run_id": r.get("id"),
                })
            runs.sort(key=lambda x: x["start"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            scans.append({
                "data_source": ds_name,
                "scan_name": scan["name"],
                "scan_rule_set": props.get("scanRulesetName") or "",
                "collection": collection_path(scan_coll, cols),
                "schedule": schedule_type(trig),
                "schedule_detail": schedule_detail(trig),
                "last_run_status": portal_status(None) if not runs else runs[0]["status"],
                "last_scan_time": fmt_utc(runs[0]["start"]) if runs else "",
                "runs": runs,
            })
    return scans
 
 
def build_scan_report(scans: List[Dict[str, Any]], days: int, now: datetime) -> List[Dict[str, Any]]:
    """One row per run in the last `days` days; the scan's totals repeat on each row.
    Scans with no runs in the window still get one row."""
    cutoff = now - timedelta(days=days)
    rows = []
    for s in sorted(scans, key=lambda x: (x["scan_name"], x["data_source"])):
        window = [r for r in s["runs"] if r["start"] and r["start"] >= cutoff]
        st = Counter(r["status"] for r in window)
        tr = Counter(r["triggered_by"] for r in window)
        base = {k: s[k] for k in SCAN_REPORT_COLUMNS}
        base.update({
            "data_source": s["data_source"], "schedule_detail": s["schedule_detail"],
            f"runs_{days}d": len(window), "completed": st[COMPLETED],
            "completed_with_exceptions": st[EXCEPTIONS], "failed": st[FAILED],
            "other": len(window) - st[COMPLETED] - st[EXCEPTIONS] - st[FAILED],
            "scheduled_runs": tr["Schedule"], "manual_runs": tr["Manual"],
        })
        if not window:
            rows.append({**base, "run_start": "", "run_end": "", "duration_min": "",
                         "run_status": f"No runs in last {days} days", "triggered_by": "",
                         "run_type_raw": "", "scan_level": "", "assets_discovered": "",
                         "ingestion_status": "", "error": "", "run_id": ""})
        for r in window:
            rows.append({**base,
                         "run_start": fmt_utc(r["start"]), "run_end": fmt_utc(r["end"]),
                         "duration_min": round((r["end"] - r["start"]).total_seconds() / 60, 1)
                         if r["start"] and r["end"] else "",
                         "run_status": r["status"], "triggered_by": r["triggered_by"],
                         "run_type_raw": r["run_type_raw"], "scan_level": r["scan_level"],
                         "assets_discovered": r["assets_discovered"],
                         "ingestion_status": r["ingestion_status"], "error": r["error"],
                         "run_id": r["run_id"]})
    return rows
 
 
def ordered(rows: List[Dict[str, Any]], leading: List[str]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Put `leading` columns first, keep the rest in their built order."""
    if not rows:
        return leading, []
    columns = leading + [c for c in rows[0] if c not in leading]
    return columns, [{c: r.get(c, "") for c in columns} for r in rows]
 
 
# ── HTML report ───────────────────────────────────────────────────────────────
# Only shapes data here; markup and styling live in report_ui/purview_scan_trigger_validation/
# and the shared branding in report_ui/shared/ (see utils/report_renderer.py).

_BADGE = {COMPLETED: "ok", EXCEPTIONS: "warn", FAILED: "bad", "passed": "ok", "failed": "bad",
          "Schedule": "ok", "Manual": "mute", "PASS": "ok", "WARN": "warn", "FAIL": "bad"}

SCAN_COLUMNS = ["scan_name", "last_run_status", "schedule", "scan_rule_set", "last_scan_time", "collection",
                "data_source", "schedule_detail"]
RUN_COLUMNS = ["scan_name", "run_start", "run_end", "duration_min", "run_status", "triggered_by",
               "scan_level", "assets_discovered", "ingestion_status", "error", "data_source"]
VALIDATION_COLUMNS = ["scan_name", "result", "reason", "expected_run_time", "actual_run_time", "delay_min",
                      "run_status", "triggered_by", "expected_schedule", "last_scan_time", "csv_line"]

# (title, category, style) in display order; "bad" groups start expanded
VALIDATION_GROUPS = [
    ("Missed scheduled runs", "missed", "bad"), ("Failed runs", "failed", "bad"),
    ("Scan not found in Purview", "not_found", "bad"), ("No enabled schedule", "no_schedule", "bad"),
    ("Invalid rows in expected schedule", "invalid_csv", "bad"),
    ("Completed with exceptions / in progress", "warning", "warn"),
    ("Not due yet", "not_due", "warn"), ("Ran on time and completed", "passed", "ok"),
]


def _cell(value: Any, badged: bool) -> Any:
    if not badged:
        return value
    v = "" if value is None else str(value)
    if not v:
        return ""
    return badge(v, _BADGE.get(v, "bad" if v.startswith("Manual (") else "mute"))


def _table(columns: List[str], data: List[Dict[str, Any]], badged: set, tid: str,
           wrap: tuple = ("reason", "error"), filterable: bool = True) -> Dict[str, Any]:
    return table_spec(
        [c.replace("_", " ").title() for c in columns],
        [[_cell(r.get(c), c in badged) for c in columns] for r in data],
        tid=tid, wrap=[i for i, c in enumerate(columns) if c in wrap], filterable=filterable)


def _card(value: Any, label: str, cls: str = "") -> Dict[str, Any]:
    return {"value": value, "label": label, "cls": cls}


def _validation_context(validation: Optional[List[Dict[str, Any]]], meta: Dict[str, Any]) -> Dict[str, Any]:
    if validation is None:
        return {"error": meta.get("validation_error") or "no expected schedule",
                "cards": [], "summary": {}, "groups": []}
    vt = Counter(r["result"] for r in validation)
    if not vt["FAIL"] and not vt["WARN"]:
        summary = {"cls": "ok", "text": "✔ Every expected run happened on time and completed"}
    elif vt["FAIL"]:
        summary = {"cls": "err", "text": f"✖ {vt['FAIL']} expected run(s) failed validation"}
    else:
        summary = {"cls": "warn", "text": f"⚠ {vt['WARN']} expected run(s) need a look"}
    groups = []
    for title, cat, cls in VALIDATION_GROUPS:
        items = [r for r in validation if r["category"] == cat]
        if items:
            groups.append({"title": title, "count": len(items), "cls": cls, "open": cls == "bad",
                           "table": _table(VALIDATION_COLUMNS, items, {"result", "run_status", "triggered_by"},
                                           f"t-v-{cat}")})
    return {
        "error": "",
        "cards": [_card(len(validation), "Expected runs"), _card(vt["PASS"], "Pass", "ok"),
                  _card(vt["WARN"], "Warn", "warn" if vt["WARN"] else "mute"),
                  _card(vt["FAIL"], "Fail", "bad" if vt["FAIL"] else "mute")],
        "summary": summary,
        "groups": groups,
    }


def build_context(rows: List[Dict[str, Any]], days: int, meta: Dict[str, Any],
                  validation: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Template variables for report_ui/purview_scan_trigger_validation/templates/index.html."""
    scans, seen = [], set()
    for r in rows:
        key = (r.get("data_source"), r.get("scan_name"))
        if key not in seen:
            seen.add(key)
            scans.append(r)
    runs = [r for r in rows if r.get("run_start")]
    tests = meta.get("test_results") or []
    t_pass = sum(t["outcome"] == "passed" for t in tests)
    status = Counter(r["run_status"] for r in runs)
    trig = Counter(r["triggered_by"] for r in runs)
    sched = sum(s["schedule"] == "Schedule" for s in scans)
    manual = len(scans) - sched
    ok_rate = f"{round(100 * (status[COMPLETED] + status[EXCEPTIONS]) / len(runs))}%" if runs else "–"
    started = datetime.fromtimestamp(meta["start_time"]).strftime("%d/%m/%Y %H:%M:%S") if meta.get("start_time") else "–"
    duration = (f"{meta['end_time'] - meta['start_time']:.1f}s"
                if meta.get("start_time") and meta.get("end_time") else "–")
    v_ctx = _validation_context(validation, meta)

    scan_columns = SCAN_COLUMNS + [f"runs_{days}d", "completed", "completed_with_exceptions",
                                   "failed", "scheduled_runs", "manual_runs"]
    return {
        "title": "Purview Scan Trigger Validation",
        "subtitle": "Snowflake scans · schedules and run history",
        "scope": meta.get("scope") or "",
        "days": days,
        "tabs": [
            {"id": "tab-overview", "label": "Overview"},
            {"id": "tab-validation", "label": f"Validation ({len(validation) if validation is not None else 0})",
             "flag": any(r["result"] == "FAIL" for r in validation or [])},
            {"id": "tab-scans", "label": f"Scans ({len(scans)})", "flag": manual > 0},
            {"id": "tab-runs", "label": f"Run history ({len(runs)})", "flag": status[FAILED] > 0},
        ],
        "banner": [("Environment", meta.get("environment") or ""), ("Scope", meta.get("scope") or ""),
                   ("Purview account", meta.get("account") or ""), ("Run history window", f"Last {days} days"),
                   ("Run started", started), ("Duration", duration)],
        "scan_cards": [_card(len(scans), "Scans"), _card(sched, "Scheduled", "ok"),
                       _card(manual, "Manual / no schedule", "bad" if manual else "mute")],
        "run_cards": [_card(len(runs), "Runs"), _card(status[COMPLETED], "Completed", "ok"),
                      _card(status[EXCEPTIONS], "With exceptions", "warn" if status[EXCEPTIONS] else "mute"),
                      _card(status[FAILED], "Failed", "bad" if status[FAILED] else "mute"),
                      _card(ok_rate, "Success rate"),
                      _card(trig["Schedule"], "Triggered by schedule"),
                      _card(trig["Manual"], "Triggered manually", "mute")],
        "tests": {"total": len(tests), "passed": t_pass},
        "test_table": _table(["test", "result", "duration_s"],
                             [{"test": t["name"], "result": t["outcome"], "duration_s": t["duration"]} for t in tests],
                             {"result"}, "t-tests", filterable=False),
        "validation": v_ctx,
        "scans_table": _table(scan_columns, scans, {"last_run_status", "schedule"}, "t-scans"),
        "runs_table": _table(RUN_COLUMNS, runs, {"run_status", "triggered_by"}, "t-runs"),
    }


def write_html_report(rows: List[Dict[str, Any]], days: int, meta: Dict[str, Any],
                      validation: Optional[List[Dict[str, Any]]] = None) -> str:
    """Render reports/purview_scan_trigger_validation/index.html (Overview, Validation, Scans, Run history).

    rows        scan report rows from build_scan_report()
    meta        environment, scope, account, start_time, end_time, test_results, validation_error
    validation  rows from scan_validator.validate(), or None if it didn't run

    Returns the path of the written index.html.
    """
    data = {"meta": meta, "days": days, "scan_report": rows, "validation": validation}
    return render_report(PURVIEW_SCAN_TRIGGER_VALIDATION, build_context(rows, days, meta, validation), data=data)
