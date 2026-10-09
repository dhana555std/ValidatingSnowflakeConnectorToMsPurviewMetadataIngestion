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
# Styled to match the Snowflake connector validation report (navy header, tab bar,
# stat cards, navy-header tables, collapsible sections).
 
_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Arial, Helvetica, sans-serif; background: #F5F7FA;
       color: #1A2636; font-size: 14px; line-height: 1.5; }
header { background: linear-gradient(135deg, #1B3A5C 0%, #264D7A 100%);
  padding: 14px 28px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  justify-content: space-between; box-shadow: 0 3px 8px rgba(0,0,0,.3); }
header .brand { display: flex; align-items: center; gap: 14px; }
header .title { color: #FFFFFF; font-size: 18px; font-weight: 700; letter-spacing: .3px; }
header .subtitle { color: #A8C8E8; font-size: 12px; margin-top: 2px; }
header .meta { color: #A8C8E8; font-size: 12px; text-align: right; }
header .meta strong { color: #FFFFFF; }
footer { background: #1B3A5C; color: #7A9AB8; text-align: center;
  padding: 14px; font-size: 12px; margin-top: 40px; }
.container { max-width: 1300px; margin: 0 auto; padding: 24px 20px; }
.tab-bar { display: flex; gap: 3px; background: #1B3A5C; padding: 0 10px;
  border-radius: 6px 6px 0 0; overflow-x: auto; flex-wrap: nowrap; }
.tab-btn { background: transparent; border: none; color: #A8C8E8; cursor: pointer;
  padding: 11px 16px; font-size: 13px; font-weight: 600; white-space: nowrap;
  border-bottom: 3px solid transparent; transition: .15s; }
.tab-btn:hover { color: #FFFFFF; border-bottom-color: #2C6FAC; }
.tab-btn.active { color: #FFFFFF; border-bottom-color: #E07B29; background: rgba(255,255,255,.07); }
.tab-btn .flag { color: #FF8A7A; font-size: 10px; margin-left: 4px; }
.tab-content { background: #FFFFFF; border-radius: 0 0 8px 8px; padding: 24px;
  box-shadow: 0 2px 8px rgba(0,0,0,.08); }
.tab-pane { display: none; animation: fadeIn .2s ease; }
.tab-pane.active { display: block; }
@keyframes fadeIn { from { opacity:0; transform:translateY(3px); } to { opacity:1; transform:none; } }
.table-wrap { overflow: auto; max-height: 560px; border: 1px solid #DDE5F0; border-radius: 6px; }
.vt-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.vt-table th { background: #1B3A5C; color: #FFFFFF; padding: 8px 12px; text-align: left;
  white-space: nowrap; position: sticky; top: 0; z-index: 1; }
.vt-table td { padding: 6px 12px; border-bottom: 1px solid #DDE5F0; white-space: nowrap; }
.vt-table td.wrap { white-space: normal; min-width: 260px; }
.vt-table tr:nth-child(even) { background: #E8F0F8; }
.vt-table tr:hover { background: #D0E4F4; }
.stat-row { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 18px; }
.stat-card { flex: 1; min-width: 120px; border-radius: 8px; padding: 16px 20px;
  box-shadow: 0 1px 4px rgba(0,0,0,.1); border-top: 4px solid #1A2636; background: #F5F7FA; }
.stat-card.ok   { border-top-color: #1B7A3E; background: #EAF7ED; }
.stat-card.bad  { border-top-color: #B5291C; background: #FDECEA; }
.stat-card.warn { border-top-color: #C77700; background: #FFF4E0; }
.stat-card.mute { border-top-color: #6B7A8D; background: #F0F0F0; }
.stat-val { font-size: 30px; font-weight: 800; color: #1A2636; }
.stat-card.ok .stat-val { color: #1B7A3E; } .stat-card.bad .stat-val { color: #B5291C; }
.stat-card.warn .stat-val { color: #8A5300; } .stat-card.mute .stat-val { color: #6B7A8D; }
.stat-lbl { font-size: 12px; color: #6B7A8D; margin-top: 4px; }
.collapsible { margin: 14px 0; border: 1px solid #D0DAE8; border-radius: 6px; }
.collapsible summary { cursor: pointer; padding: 10px 14px; font-size: 13px; font-weight: 700;
  color: #1B3A5C; background: #E8F0F8; border-radius: 6px; list-style: none;
  display: flex; align-items: center; gap: 8px; user-select: none; }
.collapsible summary::-webkit-details-marker { display: none; }
.collapsible summary::before { content: "\\25B6"; font-size: 10px; transition: transform .2s; color: #2C6FAC; }
.collapsible[open] > summary::before { transform: rotate(90deg); }
.collapsible summary .count { margin-left: auto; font-weight: 800; }
.collapsible-body { padding: 14px; }
.entity-banner { background: #EEF4FC; border-radius: 6px; padding: 12px 16px; margin-bottom: 18px;
  font-size: 13px; border: 1px solid #C5D8EE; display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 6px 24px; }
.entity-banner b { display: block; color: #6B7A8D; font-weight: 600; font-size: 12px; }
.ok-box { background: #EAF7ED; border: 1px solid #B0DCBD; border-radius: 6px;
  padding: 12px 16px; color: #1B7A3E; font-weight: 600; margin: 6px 0; }
.err-box { background: #FDECEA; border: 1px solid #E8A89A; border-radius: 6px;
  padding: 10px 14px; color: #B5291C; font-weight: 600; margin: 6px 0; }
.warn-box { background: #FFF4E0; border: 1px solid #F0C98A; border-radius: 6px;
  padding: 10px 14px; color: #8A5300; font-weight: 600; margin: 6px 0 10px; }
.section-h3 { font-size: 14px; font-weight: 700; color: #1B3A5C; border-left: 4px solid #2C6FAC;
  padding: 4px 10px; margin: 18px 0 10px; background: #E8F0F8; border-radius: 0 4px 4px 0; }
.filter { margin: 0 0 10px; padding: 7px 10px; width: min(360px, 100%); border: 1px solid #C5D8EE;
  border-radius: 6px; font: inherit; font-size: 13px; }
.badge { display: inline-block; border-radius: 4px; padding: 1px 8px; font-size: 12px; font-weight: 700; }
.badge.ok { background: #EAF7ED; color: #1B7A3E; } .badge.bad { background: #FDECEA; color: #B5291C; }
.badge.warn { background: #FFF4E0; color: #8A5300; } .badge.mute { background: #EEF1F5; color: #6B7A8D; }
.empty { color: #6B7A8D; }
@media (max-width: 640px) { header { padding: 12px 16px; } header .meta { text-align: left; }
  .container { padding: 16px 12px; } .tab-content { padding: 16px; } }
"""
 
_LOGO = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 130 52' height='44' aria-label='Purview scan trigger validator'>
  <ellipse cx='26' cy='14' rx='18' ry='7' fill='none' stroke='#5BAAD8' stroke-width='2.5'/>
  <line x1='8' y1='14' x2='8' y2='38' stroke='#5BAAD8' stroke-width='2.5'/>
  <line x1='44' y1='14' x2='44' y2='38' stroke='#5BAAD8' stroke-width='2.5'/>
  <ellipse cx='26' cy='38' rx='18' ry='7' fill='none' stroke='#5BAAD8' stroke-width='2.5'/>
  <line x1='52' y1='26' x2='72' y2='26' stroke='#E07B29' stroke-width='3' stroke-linecap='round'/>
  <polyline points='66,20 72,26 66,32' fill='none' stroke='#E07B29' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/>
  <circle cx='105' cy='26' r='16' fill='none' stroke='#5BAAD8' stroke-width='2.5'/>
  <polyline points='105,16 105,26 113,30' fill='none' stroke='#5BAAD8' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>
</svg>"""
 
_BADGE = {COMPLETED: "ok", EXCEPTIONS: "warn", FAILED: "bad", "passed": "ok", "failed": "bad",
          "Schedule": "ok", "Manual": "mute", "PASS": "ok", "WARN": "warn", "FAIL": "bad"}
 
 
def write_html_report(path: str, rows: List[Dict[str, Any]], days: int, meta: Dict[str, Any],
                      validation: Optional[List[Dict[str, Any]]] = None) -> None:
    """Self-contained HTML report: Overview, Validation, Scans and Run history tabs.
 
    rows        scan report rows from build_scan_report()
    meta        environment, scope, account, start_time, end_time, test_results, validation_error
    validation  rows from scan_validator.validate(), or None if it didn't run
    """
    import html as _html
    from datetime import datetime as _dt
 
    def e(v: Any) -> str:
        return _html.escape("" if v is None else str(v))
 
    def badge(v: Any) -> str:
        v = "" if v is None else str(v)
        if not v:
            return ""
        cls = _BADGE.get(v, "bad" if v.startswith("Manual (") else "mute" if v.startswith(("No runs", "Never")) else "mute")
        return f"<span class='badge {cls}'>{e(v)}</span>"
 
    def table(columns: List[str], data: List[Dict[str, Any]], badges: set, tid: str,
              wrap: tuple = ("reason", "error"), with_filter: bool = True) -> str:
        if not data:
            return "<p class='empty'>Nothing to show.</p>"
        head = "".join(f"<th>{e(c.replace('_', ' ').title())}</th>" for c in columns)
        body = "".join(
            "<tr>" + "".join(
                f"<td class='{'wrap' if c in wrap else ''}'>{badge(r.get(c)) if c in badges else e(r.get(c))}</td>"
                for c in columns) + "</tr>" for r in data)
        flt = (f"<input class='filter' placeholder='Filter rows…' oninput=\"filt(this,'{tid}')\">"
               if with_filter else "")
        return (f"{flt}<div class='table-wrap'><table class='vt-table' id='{tid}'>"
                f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
 
    def card(n: Any, label: str, cls: str = "") -> str:
        return f"<div class='stat-card {cls}'><div class='stat-val'>{e(n)}</div><div class='stat-lbl'>{e(label)}</div></div>"
 
    def section(title: str, n: int, body: str, cls: str = "", open_: bool = False) -> str:
        color = {"bad": "#B5291C", "warn": "#8A5300", "ok": "#1B7A3E"}.get(cls, "#1B3A5C")
        return (f"<details class='collapsible'{' open' if open_ else ''}><summary>{e(title)}"
                f"<span class='count' style='color:{color}'>{n}</span></summary>"
                f"<div class='collapsible-body'>{body}</div></details>")
 
    # ── data ──
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
    ok_rate = f"{round(100 * (status[COMPLETED] + status[EXCEPTIONS]) / len(runs))}%" if runs else "–"
    started = _dt.fromtimestamp(meta["start_time"]).strftime("%d/%m/%Y %H:%M:%S") if meta.get("start_time") else "–"
    duration = (f"{meta['end_time'] - meta['start_time']:.1f}s"
                if meta.get("start_time") and meta.get("end_time") else "–")
 
    # ── Overview ──
    test_box = (f"<div class='ok-box'>✔ All {len(tests)} tests passed</div>" if tests and t_pass == len(tests)
                else f"<div class='err-box'>✖ {len(tests) - t_pass} of {len(tests)} tests failed</div>" if tests
                else "")
    overview = f"""
      <div class='entity-banner'>
        <div><b>Environment</b>{e(meta.get('environment'))}</div>
        <div><b>Scope</b>{e(meta.get('scope'))}</div>
        <div><b>Purview account</b>{e(meta.get('account'))}</div>
        <div><b>Run history window</b>Last {days} days</div>
        <div><b>Run started</b>{started}</div>
        <div><b>Duration</b>{duration}</div>
      </div>
      <div class='section-h3'>Scans</div>
      <div class='stat-row'>{card(len(scans), 'Scans')}{card(sched, 'Scheduled', 'ok')}{card(len(scans) - sched, 'Manual / no schedule', 'bad' if len(scans) - sched else 'mute')}</div>
      <div class='section-h3'>Runs in last {days} days</div>
      <div class='stat-row'>{card(len(runs), 'Runs')}{card(status[COMPLETED], 'Completed', 'ok')}{card(status[EXCEPTIONS], 'With exceptions', 'warn' if status[EXCEPTIONS] else 'mute')}{card(status[FAILED], 'Failed', 'bad' if status[FAILED] else 'mute')}{card(ok_rate, 'Success rate')}{card(trig['Schedule'], 'Triggered by schedule')}{card(trig['Manual'], 'Triggered manually', 'mute')}</div>
      <div class='section-h3'>Test results ({t_pass}/{len(tests)} passed)</div>
      {test_box}
      {table(['test', 'result', 'duration_s'], [{'test': t['name'], 'result': t['outcome'], 'duration_s': t['duration']} for t in tests], {'result'}, 't-tests', with_filter=False)}"""
 
    # ── Validation ──
    vcols = ["scan_name", "result", "reason", "expected_run_time", "actual_run_time", "delay_min",
             "run_status", "triggered_by", "expected_schedule", "last_scan_time", "csv_line"]
    if validation is None:
        validation_html = (f"<div class='warn-box'>⚠ Validation did not run: "
                           f"{e(meta.get('validation_error') or 'no expected schedule')}</div>")
        v_fail = 0
    else:
        vt = Counter(r["result"] for r in validation)
        v_fail = vt["FAIL"]
        groups = [("Missed scheduled runs", "missed", "bad"), ("Failed runs", "failed", "bad"),
                  ("Scan not found in Purview", "not_found", "bad"), ("No enabled schedule", "no_schedule", "bad"),
                  ("Invalid rows in expected schedule", "invalid_csv", "bad"),
                  ("Completed with exceptions / in progress", "warning", "warn"),
                  ("Not due yet", "not_due", "warn"), ("Ran on time and completed", "passed", "ok")]
        summary_box = ("<div class='ok-box'>✔ Every expected run happened on time and completed</div>"
                       if not vt["FAIL"] and not vt["WARN"] else
                       f"<div class='err-box'>✖ {vt['FAIL']} expected run(s) failed validation</div>" if vt["FAIL"] else
                       f"<div class='warn-box'>⚠ {vt['WARN']} expected run(s) need a look</div>")
        sections = "".join(
            section(title, len(items), table(vcols, items, {"result", "run_status", "triggered_by"}, f"t-v-{cat}"),
                    cls, open_=(cls == "bad"))
            for title, cat, cls in groups
            for items in [[r for r in validation if r["category"] == cat]] if items)
        validation_html = (
            f"<div class='stat-row'>{card(len(validation), 'Expected runs')}{card(vt['PASS'], 'Pass', 'ok')}"
            f"{card(vt['WARN'], 'Warn', 'warn' if vt['WARN'] else 'mute')}{card(vt['FAIL'], 'Fail', 'bad' if vt['FAIL'] else 'mute')}</div>"
            f"{summary_box}<div class='section-h3'>Details</div>{sections}")
 
    # ── Scans / Run history ──
    scan_cols = ["scan_name", "last_run_status", "schedule", "scan_rule_set", "last_scan_time", "collection",
                 "data_source", "schedule_detail", f"runs_{days}d", "completed", "completed_with_exceptions",
                 "failed", "scheduled_runs", "manual_runs"]
    run_cols = ["scan_name", "run_start", "run_end", "duration_min", "run_status", "triggered_by",
                "scan_level", "assets_discovered", "ingestion_status", "error", "data_source"]
    scans_html = table(scan_cols, scans, {"last_run_status", "schedule"}, "t-scans")
    runs_html = table(run_cols, runs, {"run_status", "triggered_by"}, "t-runs")
 
    def tab(tid: str, label: str, flag: bool = False, active: bool = False) -> str:
        mark = "<span class='flag' title='Has failures'>●</span>" if flag else ""
        return (f"<button class='tab-btn{' active' if active else ''}' data-tab='{tid}' "
                f"onclick=\"showTab('{tid}')\">{e(label)}{mark}</button>")
 
    tabs = (tab("tab-overview", "Overview", active=True)
            + tab("tab-validation", f"Validation ({len(validation) if validation is not None else 0})", v_fail > 0)
            + tab("tab-scans", f"Scans ({len(scans)})", len(scans) - sched > 0)
            + tab("tab-runs", f"Run history ({len(runs)})", status[FAILED] > 0))
    generated = _dt.now().strftime("%d/%m/%Y %H:%M:%S")
 
    page = f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Purview Scan Trigger Report</title>
<style>{_CSS}</style></head>
<body>
<header>
  <div class='brand'>{_LOGO}
    <div><div class='title'>Purview Scan Trigger Validation</div>
         <div class='subtitle'>Snowflake scans · schedules and run history</div></div>
  </div>
  <div class='meta'>Scope: <strong>{e(meta.get('scope'))}</strong><br/>Generated {generated} · times in UTC</div>
</header>
<div class='container'>
  <div class='tab-bar'>{tabs}</div>
  <div class='tab-content'>
    <div class='tab-pane active' id='tab-overview'>{overview}</div>
    <div class='tab-pane' id='tab-validation'>{validation_html}</div>
    <div class='tab-pane' id='tab-scans'>{scans_html}</div>
    <div class='tab-pane' id='tab-runs'>{runs_html}</div>
  </div>
</div>
<footer>DDX QA Automation Framework · Purview Scan Trigger Validation</footer>
<script>
function showTab(id) {{
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
  document.getElementById(id).classList.add('active');
}}
function filt(inp, id) {{
  const q = inp.value.toLowerCase();
  document.querySelectorAll('#' + id + ' tbody tr').forEach(tr =>
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none');
}}
</script>
</body></html>"""
 
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)