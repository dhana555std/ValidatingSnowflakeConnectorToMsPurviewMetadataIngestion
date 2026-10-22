"""
scan_report_builder
===================
Builds the Purview scan validation report by combining scan schedules,
run history and validation results.

Report Sections
    Overview                scan and execution summary
    Validation              Trigger and Metadata Validation results
    Scans & Run History     detailed scan execution information

Inputs
    Purview scans
    Expected schedule CSV
    Validation results
    Test results

Outputs
    HTML report
    CSV report
    JSONL report

All report timestamps are displayed in UTC.
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

SCAN_REPORT_COLUMNS = [
    "scan_name", "last_run_status", "schedule", "scan_rule_set",
    "last_scan_time", "collection",
]


def parse_api_ts(value: Optional[str]) -> Optional[datetime]:
    """Convert a Purview API timestamp to an aware UTC datetime."""
    if not value:
        return None
    try:
        normalized = re.sub(
            r"\.(\d{6})\d+",
            r".\1",
            str(value).replace("Z", "+00:00"),
        )
        timestamp = datetime.fromisoformat(normalized)
        if not timestamp.tzinfo:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
    except ValueError:
        return None


def fmt_utc(timestamp: Optional[datetime]) -> str:
    return timestamp.astimezone(timezone.utc).strftime(TIME_FMT) if timestamp else ""


def portal_status(run: Optional[Dict[str, Any]]) -> str:
    """Return the run status using Microsoft Purview portal wording."""
    if not run:
        return "Never run"

    status = run.get("status") or ""
    if status != "Succeeded":
        return {
            "Failed": FAILED,
            "Canceled": "Canceled",
            "InProgress": "In progress",
            "Accepted": "Queued",
            "TransientFailure": "Transient failure",
            "CompletedWithExceptions": EXCEPTIONS,
        }.get(status, status or "Unknown")

    discovery = (run.get("discoveryExecutionDetails") or {}).get("status") or ""
    ingestion = (run.get("ingestionExecutionDetails") or {}).get("status") or ""
    exceptions = (run.get("diagnostics") or {}).get("exceptionCountMap") or {}
    if (
        discovery in ("CompletedWithExceptions", "CompleteWithWarning")
        or ingestion in ("PartialSucceeded", "Failed")
        or any(exceptions.values())
    ):
        return EXCEPTIONS
    return COMPLETED


def run_trigger(run: Dict[str, Any]) -> str:
    return "Manual" if (run.get("runType") or "").lower() == "manual" else "Schedule"


def schedule_type(trigger: Optional[Dict[str, Any]]) -> str:
    if not trigger:
        return "Manual"
    state = (trigger.get("properties") or {}).get("state", "Enabled")
    return "Schedule" if state == "Enabled" else "Manual (schedule disabled)"


def schedule_detail(trigger: Optional[Dict[str, Any]]) -> str:
    if not trigger:
        return ""
    recurrence = (trigger.get("properties") or {}).get("recurrence") or {}
    schedule = recurrence.get("schedule") or {}
    parts = [f"Every {recurrence.get('interval', 1)} {recurrence.get('frequency', '?')}"]
    if schedule.get("weekDays"):
        parts.append("on " + ", ".join(schedule["weekDays"]))
    if schedule.get("monthDays"):
        parts.append("on day " + ", ".join(map(str, schedule["monthDays"])))
    if schedule.get("hours"):
        times = [
            f"{hour:02d}:{minute:02d}"
            for hour in schedule["hours"]
            for minute in (schedule.get("minutes") or [0])
        ]
        parts.append("at " + ", ".join(times))
    if recurrence.get("timeZone"):
        parts.append(f"({recurrence['timeZone']})")
    return " ".join(parts)


def collection_path(reference: str, collections: Dict[str, Dict[str, str]]) -> str:
    names, seen = [], set()
    while reference and reference in collections and reference not in seen:
        seen.add(reference)
        names.append(collections[reference]["name"])
        reference = collections[reference]["parent"]
    if len(names) > 1:
        names = names[:-1]
    return " > ".join(reversed(names))


def collect_scans(
    client: Any,
    collection: str,
    parent: str,
    kind: str = "Snowflake",
) -> List[Dict[str, Any]]:
    """Collect scans, schedules and complete run history from Purview."""
    collections = client.collections()

    def in_scope(reference: str) -> bool:
        if not collection:
            return True
        item = collections.get(reference)
        if not item or item["name"] != collection:
            return False
        return not parent or collections.get(item["parent"], {}).get("name") == parent

    scans: List[Dict[str, Any]] = []
    for source in client.datasources():
        if kind and str(source.get("kind", "")).lower() != kind.lower():
            continue
        source_name = source["name"]
        source_collection = (
            ((source.get("properties") or {}).get("collection") or {}).get(
                "referenceName", ""
            )
        )
        for scan in client.scans(source_name):
            properties = scan.get("properties") or {}
            scan_collection = (
                (properties.get("collection") or {}).get("referenceName")
                or source_collection
            )
            if not in_scope(scan_collection):
                continue

            trigger = client.trigger(source_name, scan["name"])
            runs: List[Dict[str, Any]] = []
            for run in client.runs(source_name, scan["name"]):
                start = parse_api_ts(run.get("startTime"))
                end = parse_api_ts(run.get("endTime"))
                assets = (
                    ((run.get("discoveryExecutionDetails") or {}).get("statistics") or {})
                    .get("assets")
                    or {}
                )
                runs.append({
                    "start": start,
                    "end": end,
                    "status": portal_status(run),
                    "triggered_by": run_trigger(run),
                    "run_type_raw": run.get("runType"),
                    "scan_level": run.get("scanLevelType"),
                    "assets_discovered": assets.get("discovered", ""),
                    "ingestion_status": (
                        (run.get("ingestionExecutionDetails") or {}).get("status") or ""
                    ),
                    "error": (
                        run.get("errorMessage")
                        or (run.get("error") or {}).get("message")
                        or ""
                    ),
                    "run_id": run.get("id"),
                })

            runs.sort(
                key=lambda item: item["start"]
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            scans.append({
                "data_source": source_name,
                "scan_name": scan["name"],
                "scan_rule_set": properties.get("scanRulesetName") or "",
                "collection": collection_path(scan_collection, collections),
                "schedule": schedule_type(trigger),
                "schedule_detail": schedule_detail(trigger),
                "last_run_status": portal_status(None) if not runs else runs[0]["status"],
                "last_scan_time": fmt_utc(runs[0]["start"]) if runs else "",
                "runs": runs,
            })
    return scans


def build_scan_report(
    scans: List[Dict[str, Any]],
    days: int,
    now: datetime,
) -> List[Dict[str, Any]]:
    """Build one row per run and one placeholder row for scans without recent runs."""
    cutoff = now - timedelta(days=days)
    rows: List[Dict[str, Any]] = []
    for scan in sorted(scans, key=lambda item: (item["scan_name"], item["data_source"])):
        window = [
            run for run in scan["runs"]
            if run["start"] and run["start"] >= cutoff
        ]
        status_counts = Counter(run["status"] for run in window)
        trigger_counts = Counter(run["triggered_by"] for run in window)
        base = {key: scan[key] for key in SCAN_REPORT_COLUMNS}
        base.update({
            "data_source": scan["data_source"],
            "schedule_detail": scan["schedule_detail"],
            f"runs_{days}d": len(window),
            "completed": status_counts[COMPLETED],
            "completed_with_exceptions": status_counts[EXCEPTIONS],
            "failed": status_counts[FAILED],
            "other": (
                len(window)
                - status_counts[COMPLETED]
                - status_counts[EXCEPTIONS]
                - status_counts[FAILED]
            ),
            "scheduled_runs": trigger_counts["Schedule"],
            "manual_runs": trigger_counts["Manual"],
        })

        if not window:
            rows.append({
                **base,
                "run_start": "",
                "run_end": "",
                "duration_min": "",
                "run_status": f"No runs in last {days} days",
                "triggered_by": "",
                "run_type_raw": "",
                "scan_level": "",
                "assets_discovered": "",
                "ingestion_status": "",
                "error": "",
                "run_id": "",
            })

        for run in window:
            rows.append({
                **base,
                "run_start": fmt_utc(run["start"]),
                "run_end": fmt_utc(run["end"]),
                "duration_min": (
                    round((run["end"] - run["start"]).total_seconds() / 60, 1)
                    if run["start"] and run["end"] else ""
                ),
                "run_status": run["status"],
                "triggered_by": run["triggered_by"],
                "run_type_raw": run["run_type_raw"],
                "scan_level": run["scan_level"],
                "assets_discovered": run["assets_discovered"],
                "ingestion_status": run["ingestion_status"],
                "error": run["error"],
                "run_id": run["run_id"],
            })
    return rows


def ordered(
    rows: List[Dict[str, Any]],
    leading: List[str],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    if not rows:
        return leading, []
    discovered: List[str] = []
    for row in rows:
        for column in row:
            if column not in leading and column not in discovered:
                discovered.append(column)
    columns = leading + discovered
    return columns, [{column: row.get(column, "") for column in columns} for row in rows]


_BADGE = {
    COMPLETED: "ok",
    EXCEPTIONS: "warn",
    FAILED: "bad",

    # Scheduler Trigger Validation
    "Run Scheduled": "ok",
    "Did Not Run": "bad",

    # Scheduler Metadata Validation
    "Success": "ok",
    "CompletedWithExceptions": "warn",
    "Running": "warn",
    "Failed": "bad",

    "passed": "ok",
    "failed": "bad",
    "Schedule": "ok",
    "Manual": "mute",
    "PASS": "ok",
    "WARN": "warn",
    "FAIL": "bad",
}

VALIDATION_COLUMNS = [
    "scan_name",
    "result",
    "reason",
    "expected_run_time",
    "trigger_checked_at",
    "actual_run_time",
    "delay_min",
    "run_status",
    "triggered_by",
    "metadata_due_at",
    "metadata_checked_at",
    "expected_schedule",
    "last_scan_time",
    "csv_line",
]

VALIDATION_GROUPS = [

    # Trigger Validation

    (
        "Trigger Validation Failed",
        "trigger_failed",
        "bad",
    ),

    (
        "Scan Did Not Run",
        "did_not_run",
        "bad",
    ),

    # Metadata Validation

    (
        "Metadata Validation Failed",
        "metadata_failed",
        "bad",
    ),

    (
        "Metadata Validation Success",
        "metadata_success",
        "ok",
    ),

    (
        "Metadata Validation With Exceptions",
        "metadata_exception",
        "warn",
    ),

    (
        "Metadata Validation Running",
        "metadata_running",
        "warn",
    ),

    # Existing Validation Groups

    (
        "Missed scheduled runs",
        "missed",
        "bad",
    ),

    (
        "Failed runs",
        "failed",
        "bad",
    ),

    (
        "Scan not found in Purview",
        "not_found",
        "bad",
    ),

    (
        "No enabled schedule",
        "no_schedule",
        "bad",
    ),

    (
        "Invalid rows in expected schedule",
        "invalid_csv",
        "bad",
    ),

    (
        "Completed with exceptions / in progress",
        "warning",
        "warn",
    ),

    (
        "Not due yet",
        "not_due",
        "warn",
    ),

    (
        "Ran on time and completed",
        "passed",
        "ok",
    ),
]


def _cell(value: Any, badged: bool) -> Any:
    if not badged:
        return value
    text = "" if value is None else str(value)
    if not text:
        return ""
    return badge(
        text,
        _BADGE.get(text, "bad" if text.startswith("Manual (") else "mute"),
    )


def _table(
    columns: List[str],
    data: List[Dict[str, Any]],
    badged: set,
    tid: str,
    wrap: tuple = ("reason", "error"),
    filterable: bool = True,
) -> Dict[str, Any]:
    return table_spec(
        [column.replace("_", " ").title() for column in columns],
        [[_cell(row.get(column), column in badged) for column in columns] for row in data],
        tid=tid,
        wrap=[index for index, column in enumerate(columns) if column in wrap],
        filterable=filterable,
    )


def _card(value: Any, label: str, cls: str = "") -> Dict[str, Any]:
    return {"value": value, "label": label, "cls": cls}


def _validation_context(
    validation: Optional[List[Dict[str, Any]]],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    if validation is None:
        return {
            "error": meta.get("validation_error") or "no expected schedule",
            "cards": [], "summary": {}, "groups": [],
        }

    totals = Counter(row["result"] for row in validation)
    if not totals["FAIL"] and not totals["WARN"]:
        summary = {"cls": "ok", "text": "All Trigger Validations and Metadata Validations completed successfully"}
    elif totals["FAIL"]:
        summary = {"cls": "err", "text": f"{totals['FAIL']} expected run(s) failed validation"}
    else:
        summary = {"cls": "warn", "text": f"{totals['WARN']} expected run(s) need a look"}

    groups = []
    for title, category, cls in VALIDATION_GROUPS:
        items = [row for row in validation if row["category"] == category]
        if items:
            groups.append({
                "title": title,
                "count": len(items),
                "cls": cls,
                "open": cls == "bad",
                "table": _table(
                    VALIDATION_COLUMNS,
                    items,
                    {"result", "run_status", "triggered_by"},
                    f"t-v-{category}",
                ),
            })

    return {
        "error": "",
        "cards": [
            _card(len(validation), "Expected runs"),
            _card(totals["PASS"], "Pass", "ok"),
            _card(totals["WARN"], "Warn", "warn" if totals["WARN"] else "mute"),
            _card(totals["FAIL"], "Fail", "bad" if totals["FAIL"] else "mute"),
        ],
        "summary": summary,
        "groups": groups,
    }


def build_context(
    rows: List[Dict[str, Any]],
    days: int,
    meta: Dict[str, Any],
    validation: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build report context with one combined Scans & Run History table."""
    scans, seen = [], set()
    for row in rows:
        key = (row.get("data_source"), row.get("scan_name"))
        if key not in seen:
            seen.add(key)
            scans.append(row)

    runs = [row for row in rows if row.get("run_start")]
    tests = meta.get("test_results") or []
    passed_tests = sum(test["outcome"] == "passed" for test in tests)
    status = Counter(row["run_status"] for row in runs)
    triggers = Counter(row["triggered_by"] for row in runs)
    scheduled = sum(scan["schedule"] == "Schedule" for scan in scans)
    manual = len(scans) - scheduled
    success_rate = (
        f"{round(100 * (status[COMPLETED] + status[EXCEPTIONS]) / len(runs))}%"
        if runs else "-"
    )
    started = (
        datetime.fromtimestamp(meta["start_time"]).strftime("%d/%m/%Y %H:%M:%S")
        if meta.get("start_time") else "-"
    )
    duration = (
        f"{meta['end_time'] - meta['start_time']:.1f}s"
        if meta.get("start_time") and meta.get("end_time") else "-"
    )

    combined_columns = [
        "scan_name",
        "last_run_status",
        "schedule",
        "scan_rule_set",
        "last_scan_time",
        "collection",
        "data_source",
        "schedule_detail",
        "run_start",
        "run_end",
        "duration_min",
        "run_status",
        "triggered_by",
        "assets_discovered",
        "error",
    ]

    return {
        "title": "Purview Scan Trigger Validation",
        "subtitle": "Snowflake scans - schedules and run history",
        "scope": meta.get("scope") or "",
        "days": days,
        "tabs": [
            {"id": "tab-overview", "label": "Overview"},
            {
                "id": "tab-validation",
                "label": f"Validation ({len(validation) if validation is not None else 0})",
                "flag": any(row["result"] == "FAIL" for row in validation or []),
            },
            {
                "id": "tab-scans-runs",
                "label": f"Scans & Run History ({len(rows)})",
                "flag": manual > 0 or status[FAILED] > 0,
            },
        ],
        "banner": [
            ("Environment", meta.get("environment") or ""),
            ("Scope", meta.get("scope") or ""),
            ("Purview account", meta.get("account") or ""),
            ("Run history window", f"Last {days} days"),
            ("Run started", started),
            ("Duration", duration),
        ],
        "scan_cards": [
            _card(len(scans), "Scans"),
            _card(scheduled, "Scheduled", "ok"),
            _card(manual, "Manual / no schedule", "bad" if manual else "mute"),
        ],
        "run_cards": [
            _card(len(runs), "Runs"),
            _card(status[COMPLETED], "Completed", "ok"),
            _card(status[EXCEPTIONS], "With exceptions", "warn" if status[EXCEPTIONS] else "mute"),
            _card(status[FAILED], "Failed", "bad" if status[FAILED] else "mute"),
            _card(success_rate, "Success rate"),
            _card(triggers["Schedule"], "Triggered by schedule"),
            _card(triggers["Manual"], "Triggered manually", "mute"),
        ],
        "tests": {"total": len(tests), "passed": passed_tests},
        "test_table": _table(
            ["test", "result", "duration_s"],
            [
                {
                    "test": test["name"],
                    "result": test["outcome"],
                    "duration_s": test["duration"],
                }
                for test in tests
            ],
            {"result"},
            "t-tests",
            filterable=False,
        ),
        "validation": _validation_context(validation, meta),
        "scans_runs_table": _table(
            combined_columns,
            rows,
            {"last_run_status", "schedule", "run_status", "triggered_by"},
            "t-scans",
        ),
    }


def write_html_report(
    rows: List[Dict[str, Any]],
    days: int,
    meta: Dict[str, Any],
    validation: Optional[List[Dict[str, Any]]] = None,
) -> str:
    data = {
        "meta": meta,
        "days": days,
        "scan_report": rows,
        "validation": validation,
    }
    return render_report(
        PURVIEW_SCAN_TRIGGER_VALIDATION,
        build_context(rows, days, meta, validation),
        data=data,
    )