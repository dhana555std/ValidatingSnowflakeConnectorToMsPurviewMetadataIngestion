"""
conftest (purview_scan_trigger_validation)
==========================================
All setup and session hooks for the Purview scan trigger validation.

Execution order
---------------
1. clear_scan_results    - wipes this validation's data/ and reports/ folders only
2. extract_scan_data     - connects to Purview, pulls every Snowflake scan's schedule and
                           run history, then validates the runs against expected_schedule.csv
                           (package-scoped; aborts the session if Purview fails)
3. Tests run             - pure assertions on the JSONL written in step 2, no cloud calls
4. pytest_sessionfinish  - writes index.html and report evidence. If report generation is
                           verified successfully, temporary scan data is deleted. If report
                           generation fails, temporary scan data is retained for troubleshooting.

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
from pathlib import Path

import pytest
from dotenv import load_dotenv

from .purview_scan_client import PurviewScanClient
from . import scan_report_builder as srb
from . import scan_validator as sv
from utils.paths import PURVIEW_SCAN_DATA_DIR, PURVIEW_SCAN_REPORT_DIR

load_dotenv()

DATA_DIR = PURVIEW_SCAN_DATA_DIR
REPORT_DIR = PURVIEW_SCAN_REPORT_DIR
REPORT_DATA_DIR = f"{REPORT_DIR}/data"
SCAN_RUNS_JSONL = f"{DATA_DIR}/purview_scan_runs.jsonl"
VALIDATION_JSONL = f"{DATA_DIR}/validation_results.jsonl"
HISTORY_DAYS = int(os.environ.get("SCAN_HISTORY_DAYS", "30"))
TOLERANCE_MIN = int(os.environ.get("SCAN_TOLERANCE_MINUTES", "60"))
EXPECTED_CSV = os.environ.get(
    "EXPECTED_SCHEDULE_CSV",
    os.path.join(os.path.dirname(__file__), "expected_schedule.csv"),
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SESSION: dict = {
    "environment": os.environ.get("ENVIRONMENT", "Prod"),
    "start_time": None,
    "end_time": None,
    "test_results": [],
    "scan_report": None,
    "scope": "",
    "validation": None,
    "validation_error": "",
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _abort(reason: str, pv=None) -> None:
    try:
        if pv:
            pv.close()
    except Exception:
        pass
    pytest.exit(
        f"\n{'=' * 60}\nABORTING SESSION - no tests will run.\n"
        f"    Reason: {reason}\n{'=' * 60}",
        returncode=2,
    )


def _purview_collection() -> str:
    return (
        os.environ.get("PURVIEW_COLLECTION")
        or os.environ.get("ENVIRONMENT")
        or "Prod"
    ).strip()


def _create_purview_scan_client() -> PurviewScanClient:
    required = (
        "PURVIEW_ACCOUNT_NAME",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
    )
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ConnectionError(f"missing in .env: {', '.join(missing)}")
    return PurviewScanClient(
        account_name=os.environ["PURVIEW_ACCOUNT_NAME"],
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )


def _log_what_purview_has(pv: PurviewScanClient) -> None:
    """List what the service principal can see when no scans match."""
    cols = pv.collections()
    sources = list(pv.datasources())
    _log("           No scans matched. What this service principal can see:")
    if not sources:
        _log("              (no data sources - check scan read access)")
    for ds in sources:
        ref = ((ds.get("properties") or {}).get("collection") or {}).get(
            "referenceName", ""
        )
        _log(
            f"              data source {ds['name']}  kind={ds.get('kind')}  "
            f"collection={srb.collection_path(ref, cols) or ref or '?'}"
        )
        try:
            for scan in pv.scans(ds["name"]):
                scan_ref = (
                    ((scan.get("properties") or {}).get("collection") or {}).get(
                        "referenceName"
                    )
                    or ref
                )
                _log(
                    f"                  scan {scan['name']}  "
                    f"collection={srb.collection_path(scan_ref, cols) or scan_ref}"
                )
        except Exception as exc:
            _log(f"                  scans not readable: {exc}")


def _write_jsonl(path: str, columns, rows) -> None:
    with open(path, "w", encoding="utf-8") as file:
        for row in rows:
            file.write(
                json.dumps(
                    {column: row.get(column, "") for column in columns},
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )


def _write_csv(path: str, columns, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def _verified_report_file(report_path: str) -> Path:
    """Return a verified non-empty report path or raise an exception."""
    report_file = Path(report_path)
    if not report_file.is_absolute():
        report_file = (PROJECT_ROOT / report_file).resolve()
    if not report_file.is_file():
        raise FileNotFoundError(
            "Report generation returned a path, but the HTML file was not created: "
            f"{report_file}"
        )
    if report_file.stat().st_size == 0:
        raise RuntimeError(f"The generated HTML report is empty: {report_file}")
    return report_file


def _delete_temporary_scan_data() -> None:
    """Delete temporary scan data only after report generation succeeds."""
    data_path = Path(DATA_DIR)
    if not data_path.exists():
        return
    shutil.rmtree(data_path)
    _log(
        "[Report] Deleted temporary Purview scan-trigger data after successful "
        f"report generation: {data_path}"
    )


@pytest.fixture(scope="session", autouse=True)
def clear_scan_results():
    """BeforeAll: wipe only this validation's folders."""
    _SESSION["start_time"] = _SESSION["start_time"] or time.time()
    for folder in (DATA_DIR, REPORT_DIR):
        if os.path.exists(folder):
            shutil.rmtree(folder)
            _log(f"[BeforeAll] Deleted {folder}/")
        os.makedirs(folder, exist_ok=True)


@pytest.fixture(scope="package", autouse=True)
def extract_scan_data(clear_scan_results):
    """Run extraction and expected-schedule validation once for this package."""
    parent = _purview_collection()
    collection = os.environ.get("PURVIEW_COLLECTION_COMMERCIAL", "Commercial")
    pv = None

    _log(f"\n{'=' * 60}")
    _log("  PURVIEW SCAN TRIGGER VALIDATOR")
    _log(
        f"  Environment : {_SESSION['environment']}   "
        f"Scope : {parent} > {collection}"
    )
    _log(f"{'=' * 60}")

    _log("\n[STEP 1/4] Connecting to MS Purview (Scanning API) ...")
    try:
        pv = _create_purview_scan_client()
        pv.check_connection()
        _log("           Purview OK")
    except Exception as exc:
        _abort(f"Purview connection failed: {exc}", pv=pv)

    _log(
        "\n[STEP 2/4] Extracting Snowflake scans, schedules and run history "
        f"({parent} > {collection}) ..."
    )
    try:
        scans = srb.collect_scans(pv, collection=collection, parent=parent)
        _log(
            f"           {len(scans):,} scans, "
            f"{sum(len(scan['runs']) for scan in scans):,} runs"
        )
        if not scans:
            _log_what_purview_has(pv)
    except Exception as exc:
        _abort(f"Scan extraction failed: {exc}", pv=pv)

    _log(f"\n[STEP 3/4] Building scan report (last {HISTORY_DAYS} days) ...")
    now = datetime.now(timezone.utc)
    cols, rows = srb.ordered(
        srb.build_scan_report(scans, HISTORY_DAYS, now),
        srb.SCAN_REPORT_COLUMNS,
    )
    _write_jsonl(SCAN_RUNS_JSONL, cols, rows)
    _SESSION["scan_report"] = (cols, rows)
    _SESSION["scope"] = f"{parent} > {collection}"
    _log(f"           {len(rows):,} report rows")

    _log(
        "\n[STEP 4/4] Validating runs against expected schedule "
        f"({EXPECTED_CSV}) ..."
    )
    try:
        expected = sv.read_expected(EXPECTED_CSV, TOLERANCE_MIN)
        if not expected:
            raise ValueError("no rows in the expected schedule")
        vcols, vrows = srb.ordered(
            sv.validate(expected, scans, now),
            sv.VALIDATION_COLUMNS,
        )
        _write_jsonl(VALIDATION_JSONL, vcols, vrows)
        _SESSION["validation"] = (vcols, vrows)
        tally = {
            key: sum(row["result"] == key for row in vrows)
            for key in (sv.PASS, sv.WARN, sv.FAIL)
        }
        _log(
            f"           {len(vrows):,} expected runs - "
            f"PASS {tally[sv.PASS]} - WARN {tally[sv.WARN]} - "
            f"FAIL {tally[sv.FAIL]}"
        )
    except FileNotFoundError:
        _SESSION["validation_error"] = (
            f"expected schedule file not found: {EXPECTED_CSV}"
        )
        _log(f"           {_SESSION['validation_error']}")
    except Exception as exc:
        _SESSION["validation_error"] = (
            f"expected schedule could not be read: {exc}"
        )
        _log(f"           {_SESSION['validation_error']}")

    _log("\nEXTRACTION COMPLETE - running scan trigger tests ...\n")
    yield

    try:
        pv.close()
    except Exception:
        pass


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        _SESSION["test_results"].append(
            {
                "nodeid": item.nodeid,
                "name": item.name,
                "outcome": report.outcome,
                "duration": round(report.duration, 3),
                "message": str(report.longrepr) if report.longrepr else "",
            }
        )


def pytest_sessionstart(session):
    _SESSION["start_time"] = time.time()


def pytest_sessionfinish(session, exitstatus):
    """
    Generate report evidence and HTML.

    If HTML generation succeeds and the output is verified, delete the temporary
    data/purview_scan_trigger_validation folder. If any report step fails, retain
    the temporary data unchanged for troubleshooting.
    """
    if _SESSION["scan_report"] is None:
        return

    _SESSION["end_time"] = time.time()

    try:
        os.makedirs(REPORT_DATA_DIR, exist_ok=True)
        cols, rows = _SESSION["scan_report"]

        _write_csv(f"{REPORT_DATA_DIR}/scan_report.csv", cols, rows)
        _write_jsonl(f"{REPORT_DATA_DIR}/scan_report.jsonl", cols, rows)

        with open(
            f"{REPORT_DATA_DIR}/test_results.json",
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                {
                    key: _SESSION[key]
                    for key in (
                        "environment",
                        "start_time",
                        "end_time",
                        "test_results",
                    )
                },
                file,
                indent=2,
            )

        validation_rows = None
        if _SESSION["validation"]:
            vcols, validation_rows = _SESSION["validation"]
            _write_csv(
                f"{REPORT_DATA_DIR}/validation_report.csv",
                vcols,
                validation_rows,
            )
            _write_jsonl(
                f"{REPORT_DATA_DIR}/validation_report.jsonl",
                vcols,
                validation_rows,
            )

        report_path = srb.write_html_report(
            rows,
            HISTORY_DAYS,
            meta={
                **_SESSION,
                "account": os.environ.get("PURVIEW_ACCOUNT_NAME", ""),
            },
            validation=validation_rows,
        )
        report_file = _verified_report_file(report_path)

    except Exception as exc:
        print(f"\nScan trigger report generation failed: {exc}")
        print(
            "Temporary Purview scan-trigger data retained for troubleshooting: "
            f"{DATA_DIR}"
        )
        return

    print(f"\nScan trigger report generated successfully: {report_file}")

    try:
        _delete_temporary_scan_data()
    except Exception as cleanup_error:
        print(
            "Report generation succeeded, but temporary scan data could not be "
            f"deleted: {cleanup_error}"
        )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "scan_triggers: Purview scan trigger validation",
    )
