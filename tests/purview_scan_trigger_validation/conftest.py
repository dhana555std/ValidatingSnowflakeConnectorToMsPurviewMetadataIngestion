"""
conftest (purview_scan_trigger_validation)
==========================================
All setup and session hooks for the Purview scan trigger validation.
 
Execution order
---------------
1. clear_scan_results    — wipes this validation's data/ and reports/ folders only
2. extract_scan_data     — connects to Purview, pulls every Snowflake scan's schedule and
                           run history, then validates the runs against expected_schedule.csv
                           (package-scoped; aborts the session if Purview fails)
3. Tests run             — pure assertions on the JSONL written in step 2, no cloud calls
4. pytest_sessionfinish  — writes scan_report.html, scan_report / validation_report (CSV + JSONL)
                           and test_results.json
 
Settings (.env)
---------------
PURVIEW_ACCOUNT_NAME, AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET   required
PURVIEW_COLLECTION (or ENVIRONMENT)   parent collection, default Prod
PURVIEW_COLLECTION_COMMERCIAL         collection, default Commercial
SCAN_HISTORY_DAYS                     run history window, default 30
EXPECTED_SCHEDULE_CSV                 default tests/purview_scan_trigger_validation/expected_schedule.csv
SCAN_TOLERANCE_MINUTES                allowed early/late start, default 60
"""
 
import csv
import json
import os
import shutil
import time
from datetime import datetime, timezone
 
import pytest
from dotenv import load_dotenv
 
from .purview_scan_client import PurviewScanClient
from . import scan_report_builder as srb
from . import scan_validator as sv
 
load_dotenv()
 
DATA_DIR = "data/purview_scan_trigger_validation"
REPORT_DIR = "reports/purview_scan_trigger_validation"
SCAN_RUNS_JSONL = f"{DATA_DIR}/purview_scan_runs.jsonl"
VALIDATION_JSONL = f"{DATA_DIR}/validation_results.jsonl"
HISTORY_DAYS = int(os.environ.get("SCAN_HISTORY_DAYS", "30"))
TOLERANCE_MIN = int(os.environ.get("SCAN_TOLERANCE_MINUTES", "60"))
EXPECTED_CSV = os.environ.get(
    "EXPECTED_SCHEDULE_CSV", os.path.join(os.path.dirname(__file__), "expected_schedule.csv"))
 
_SESSION: dict = {
    "environment":  os.environ.get("ENVIRONMENT", "Prod"),
    "start_time":   None,
    "end_time":     None,
    "test_results": [],
    "scan_report":  None,     # filled by extract_scan_data
    "scope":        "",
    "validation":   None,     # (columns, rows) when the expected schedule was read
    "validation_error": "",   # why validation could not run
}
 
 
# ── helpers ───────────────────────────────────────────────────────────────────
 
def _log(msg: str) -> None:
    print(msg, flush=True)
 
 
def _abort(reason: str, pv=None) -> None:
    try:
        if pv:
            pv.close()
    except Exception:
        pass
    pytest.exit(
        f"\n{'='*60}\n❌  ABORTING SESSION — no tests will run.\n"
        f"    Reason: {reason}\n{'='*60}",
        returncode=2,
    )
 
 
def _purview_collection() -> str:
    return (os.environ.get("PURVIEW_COLLECTION") or os.environ.get("ENVIRONMENT") or "Prod").strip()
 
 
def _create_purview_scan_client() -> PurviewScanClient:
    missing = [k for k in ("PURVIEW_ACCOUNT_NAME", "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
               if not os.environ.get(k)]
    if missing:
        raise ConnectionError(f"missing in .env: {', '.join(missing)}")
    return PurviewScanClient(
        account_name=os.environ["PURVIEW_ACCOUNT_NAME"],
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
 
 
def _log_what_purview_has(pv: PurviewScanClient) -> None:
    """Nothing matched the filter: list what the service principal can see, to fix the .env values."""
    cols = pv.collections()
    sources = list(pv.datasources())
    _log("           ⚠️  No scans matched. What this service principal can see:")
    if not sources:
        _log("              (no data sources at all — the service principal may lack read access to scans)")
    for ds in sources:
        ref = ((ds.get("properties") or {}).get("collection") or {}).get("referenceName", "")
        _log(f"              • data source {ds['name']}  kind={ds.get('kind')}  "
             f"collection={srb.collection_path(ref, cols) or ref or '?'}")
        try:
            for sc in pv.scans(ds["name"]):
                sref = ((sc.get("properties") or {}).get("collection") or {}).get("referenceName") or ref
                _log(f"                  – scan {sc['name']}  collection={srb.collection_path(sref, cols) or sref}")
        except Exception as exc:
            _log(f"                  – scans not readable: {exc}")
 
 
def _write_jsonl(path: str, columns, rows) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({c: r.get(c, "") for c in columns}, ensure_ascii=False, default=str) + "\n")
 
 
def _write_csv(path: str, columns, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:     # utf-8-sig opens cleanly in Excel
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
 
 
# ── fixtures ──────────────────────────────────────────────────────────────────
 
@pytest.fixture(scope="session", autouse=True)
def clear_scan_results():
    """BeforeAll: wipe only this validation's folders so other validations' output is untouched."""
    _SESSION["start_time"] = _SESSION["start_time"] or time.time()
    for folder in (DATA_DIR, REPORT_DIR):
        if os.path.exists(folder):
            shutil.rmtree(folder)
            _log(f"[BeforeAll] Deleted {folder}/")
        os.makedirs(folder, exist_ok=True)
 
 
@pytest.fixture(scope="package", autouse=True)
def extract_scan_data(clear_scan_results):
    """Runs once before all tests in this package. Steps 1-2 abort the session on failure."""
    parent = _purview_collection()
    collection = os.environ.get("PURVIEW_COLLECTION_COMMERCIAL", "Commercial")
    pv = None
 
    _log(f"\n{'='*60}")
    _log("  PURVIEW SCAN TRIGGER VALIDATOR")
    _log(f"  Environment : {_SESSION['environment']}   Scope : {parent} > {collection}")
    _log(f"{'='*60}")
 
    # ── Step 1: Connect Purview ───────────────────────────────────────────────
    _log("\n[STEP 1/4] Connecting to MS Purview (Scanning API) ...")
    try:
        pv = _create_purview_scan_client()
        pv.check_connection()
        _log("           ✅  Purview OK")
    except Exception as exc:
        _abort(f"Purview connection failed: {exc}", pv=pv)
 
    # ── Step 2: Extract scans, schedules, run history ────────────────────────
    _log(f"\n[STEP 2/4] Extracting Snowflake scans, schedules and run history ({parent} > {collection}) ...")
    try:
        scans = srb.collect_scans(pv, collection=collection, parent=parent)
        _log(f"           ✓ {len(scans):,} scans, {sum(len(s['runs']) for s in scans):,} runs")
        if not scans:
            _log_what_purview_has(pv)
    except Exception as exc:
        _abort(f"Scan extraction failed: {exc}", pv=pv)
 
    # ── Step 3: Build the scan report ────────────────────────────────────────
    _log(f"\n[STEP 3/4] Building scan report (last {HISTORY_DAYS} days) ...")
    now = datetime.now(timezone.utc)
    cols, rows = srb.ordered(srb.build_scan_report(scans, HISTORY_DAYS, now), srb.SCAN_REPORT_COLUMNS)
    _write_jsonl(SCAN_RUNS_JSONL, cols, rows)
    _SESSION["scan_report"] = (cols, rows)
    _SESSION["scope"] = f"{parent} > {collection}"
    _log(f"           ✓ {len(rows):,} report rows")
 
    # ── Step 4: Validate against the expected schedule (non-fatal) ───────────
    _log(f"\n[STEP 4/4] Validating runs against expected schedule ({EXPECTED_CSV}) ...")
    try:
        expected = sv.read_expected(EXPECTED_CSV, TOLERANCE_MIN)
        if not expected:
            raise ValueError("no rows in the expected schedule")
        vcols, vrows = srb.ordered(sv.validate(expected, scans, now), sv.VALIDATION_COLUMNS)
        _write_jsonl(VALIDATION_JSONL, vcols, vrows)
        _SESSION["validation"] = (vcols, vrows)
        tally = {k: sum(r["result"] == k for r in vrows) for k in (sv.PASS, sv.WARN, sv.FAIL)}
        _log(f"           ✓ {len(vrows):,} expected runs · PASS {tally[sv.PASS]} · "
             f"WARN {tally[sv.WARN]} · FAIL {tally[sv.FAIL]}")
    except FileNotFoundError:
        _SESSION["validation_error"] = f"expected schedule file not found: {EXPECTED_CSV}"
        _log(f"           ⚠️  {_SESSION['validation_error']}")
    except Exception as exc:
        _SESSION["validation_error"] = f"expected schedule could not be read: {exc}"
        _log(f"           ⚠️  {_SESSION['validation_error']}")
    _log("\n✅ EXTRACTION COMPLETE — running scan trigger tests ...\n")
 
    yield
 
    try:
        pv.close()
    except Exception:
        pass
 
 
# ── per-test outcome collector (only tests in this folder) ────────────────────
 
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        _SESSION["test_results"].append({
            "nodeid":   item.nodeid,
            "name":     item.name,
            "outcome":  report.outcome,
            "duration": round(report.duration, 3),
            "message":  str(report.longrepr) if report.longrepr else "",
        })
 
 
# ── session start / finish: write reports ─────────────────────────────────────
 
def pytest_sessionstart(session):
    _SESSION["start_time"] = time.time()
 
 
def pytest_sessionfinish(session, exitstatus):
    if _SESSION["scan_report"] is None:         # this package didn't run
        return
    _SESSION["end_time"] = time.time()
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        cols, rows = _SESSION["scan_report"]
        _write_csv(f"{REPORT_DIR}/scan_report.csv", cols, rows)
        _write_jsonl(f"{REPORT_DIR}/scan_report.jsonl", cols, rows)
        with open(f"{REPORT_DIR}/test_results.json", "w", encoding="utf-8") as f:
            json.dump({k: _SESSION[k] for k in ("environment", "start_time", "end_time", "test_results")},
                      f, indent=2)
        validation_rows = None
        if _SESSION["validation"]:
            vcols, validation_rows = _SESSION["validation"]
            _write_csv(f"{REPORT_DIR}/validation_report.csv", vcols, validation_rows)
            _write_jsonl(f"{REPORT_DIR}/validation_report.jsonl", vcols, validation_rows)
        srb.write_html_report(
            f"{REPORT_DIR}/scan_report.html", rows, HISTORY_DAYS,
            meta={**_SESSION, "account": os.environ.get("PURVIEW_ACCOUNT_NAME", "")},
            validation=validation_rows,
        )
        print(f"\n\U0001f4ca  Scan trigger report → {REPORT_DIR}/scan_report.html")
    except Exception as exc:
        print(f"\n⚠️  Scan trigger report generation failed: {exc}")
 
 
def pytest_configure(config):
    config.addinivalue_line("markers", "scan_triggers: Purview scan trigger validation")
 