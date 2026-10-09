"""
conftest (snowflake_connector)
===============================
All setup and session hooks live here for now.

Execution order
---------------
1. clear_results         — wipes data/ directories (session-scoped)
2. extract_all_data      — connects to Snowflake + Purview, extracts all metadata
                           (package-scoped; abort on failure in steps 1-4)
3. Tests run             — pure comparison assertions, no cloud calls
4. pytest_sessionfinish  — reads JSONL/JSON → computes diffs → generates HTML report
"""

import json
import os
import shutil
import time
import traceback

import pytest
from dotenv import load_dotenv

from clients.purview_client import PurviewClient
from clients.snowflake_client import SnowflakeClient

load_dotenv()

from utils.paths import (
    SNOWFLAKE_RESULTS_DIR, PURVIEW_RESULTS_DIR,
    PV_COLLECTION_PATH, REPORT_HTML, REPORT_JSON,
)

_ACTUAL_DIR             = SNOWFLAKE_RESULTS_DIR
_EXPECTED_DIR           = PURVIEW_RESULTS_DIR
_COLLECTION_REPORT_PATH = PV_COLLECTION_PATH

_SESSION: dict = {
    "environment":  os.environ.get("ENVIRONMENT", "Prod"),
    "start_time":   None,
    "end_time":     None,
    "test_results": [],
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(msg, flush=True)


def _abort(reason: str, sf=None, pv=None) -> None:
    try:
        if sf:  sf.close()
    except Exception:
        pass
    try:
        if pv:  pv._session.close()
    except Exception:
        pass
    pytest.exit(
        f"\n{'='*60}\n❌  ABORTING SESSION — no tests will run.\n"
        f"    Reason: {reason}\n{'='*60}",
        returncode=2,
    )


def _purview_collection() -> str:
    return (
        os.environ.get("PURVIEW_COLLECTION")
        or os.environ.get("ENVIRONMENT")
        or "Prod"
    ).strip()


def _create_snowflake_client() -> SnowflakeClient:
    extra_raw = os.environ.get("SNOWFLAKE_EXCLUDE_DATABASES", "")
    extra = [db.strip() for db in extra_raw.split(",") if db.strip()]
    return SnowflakeClient(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        role=os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        exclude_databases=extra,
    )


def _create_purview_client() -> PurviewClient:
    return PurviewClient(
        account_name=os.environ["PURVIEW_ACCOUNT_NAME"],
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
        domain_name=os.environ["DOMAIN_NAME"],
        collection_prod=_purview_collection(),
        collection_commercial=os.environ.get("PURVIEW_COLLECTION_COMMERCIAL", "Commercial"),
    )


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def clear_results():
    """BeforeAll: delete data/ and reports/ entirely, then recreate them fresh."""
    for folder in ("data", "reports"):
        if os.path.exists(folder):
            shutil.rmtree(folder)
            _log(f"[BeforeAll] Deleted {folder}/")
    os.makedirs(_ACTUAL_DIR, exist_ok=True)
    os.makedirs(_EXPECTED_DIR, exist_ok=True)
    os.makedirs("reports/snowflake_connector_validation", exist_ok=True)
    _log("[BeforeAll] Recreated data/ and reports/ directories.")


@pytest.fixture(scope="package", autouse=True)
def extract_all_data(clear_results):
    """
    Runs once before all tests in this package.

    Steps 1-4 abort the session on failure.
    Step 5 (collection metadata) is informational — never blocks tests.
    """
    env = _SESSION["environment"]
    sf  = None
    pv  = None

    _log(f"\n{'='*60}")
    _log("  PURVIEW ↔ SNOWFLAKE METADATA VALIDATOR")
    _log(f"  Environment : {env}")
    _log(f"{'='*60}")

    # ── Step 1: Connect Snowflake ─────────────────────────────────────────────
    _log("\n[STEP 1/5] Connecting to Snowflake ...")
    try:
        sf = _create_snowflake_client()
        sf.get_databases()
        _log("           ✅  Snowflake OK")
    except Exception as exc:
        _abort(f"Snowflake connection failed: {exc}")

    # ── Step 2: Connect Purview ───────────────────────────────────────────────
    _log("\n[STEP 2/5] Connecting to MS Purview ...")
    try:
        pv = _create_purview_client()
        pv._resolve_commercial()
        _log("           ✅  Purview OK")
    except Exception as exc:
        _abort(f"Purview connection failed: {exc}", sf=sf)

    # ── Step 3: Extract Snowflake metadata ────────────────────────────────────
    _log("\n[STEP 3/5] Extracting Snowflake metadata ...")
    _log("           (SQL mirrors query.sql exactly)")
    for label, method in [
        ("databases",         sf.extract_all_databases),
        ("schemas",           sf.extract_all_schemas),
        ("tables",            sf.extract_all_tables),
        ("views",             sf.extract_all_views),
        ("columns",           sf.extract_all_columns),
        ("stored_procedures", sf.extract_all_stored_procedures),
    ]:
        _log(f"           → {label} ...")
        try:
            result = method()
            _log(f"             ✓ {result['count']:,} records")
        except Exception as exc:
            _abort(f"Snowflake extraction failed ({label}): {exc}", sf=sf, pv=pv)

    # ── Step 4: Extract Purview metadata ─────────────────────────────────────
    _log(f"\n[STEP 4/5] Extracting Purview metadata ({_purview_collection()}/Commercial) ...")
    for label, method in [
        ("databases",         pv.extract_databases),
        ("schemas",           pv.extract_schemas),
        ("tables",            pv.extract_tables),
        ("views",             pv.extract_views),
        ("columns",           pv.extract_columns),
        ("stored_procedures", pv.extract_stored_procedures),
    ]:
        _log(f"           → {label} ...")
        try:
            result = method()
            _log(f"             ✓ {result['count']:,} records")
        except Exception as exc:
            _abort(f"Purview extraction failed ({label}): {exc}", sf=sf, pv=pv)

    # ── Step 5: Collection metadata (non-fatal) ───────────────────────────────
    _log(f"\n[STEP 5/5] Extracting Commercial collection metadata ({_purview_collection()}) ...")
    try:
        result = pv.extract_collection_report(
            environments=[_purview_collection()],
            out_path=_COLLECTION_REPORT_PATH,
        )
        for col in result["sample"]:
            label = col["environment"]
            if "error" in col:
                _log(f"           ⚠️  {label}: {col['error']}")
                continue
            counts = col["asset_counts"]
            _log(
                f"           ✓ {label:<8} {col['collection_path']} "
                f"(id {col['collection_id']}) — "
                f"{counts['total_snowflake_assets']:,} Snowflake assets"
            )
            if "warning" in col:
                _log(f"           ⚠️  {label}: {col['warning']}")
    except Exception as exc:
        _log(f"           ⚠️  Collection metadata extraction failed: {exc}")

    _log("\n✅ ALL EXTRACTION COMPLETE — running comparison tests ...\n")

    yield

    try:
        sf.close()
    except Exception:
        pass
    try:
        pv._session.close()
    except Exception:
        pass


# ── per-test outcome collector ────────────────────────────────────────────────

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report  = outcome.get_result()
    if report.when == "call":
        _SESSION["test_results"].append({
            "nodeid":   item.nodeid,
            "name":     item.name,
            "outcome":  report.outcome,
            "duration": round(report.duration, 3),
            "message":  str(report.longrepr) if report.longrepr else "",
        })


# ── session finish: generate HTML report ──────────────────────────────────────

def pytest_sessionstart(session):
    _SESSION["start_time"] = time.time()


def pytest_sessionfinish(session, exitstatus):
    _SESSION["end_time"] = time.time()

    try:
        from utils.report_data_builder import build_report_data
        from utils.report_generator import generate_report

        os.makedirs("reports/snowflake_connector_validation", exist_ok=True)

        meta = {
            "environment":  _SESSION["environment"],
            "start_time":   _SESSION["start_time"],
            "end_time":     _SESSION["end_time"],
            "test_results": _SESSION["test_results"],
        }
        with open(REPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        report_data = build_report_data(
            environment  = _SESSION["environment"],
            test_results = _SESSION["test_results"],
            start_time   = _SESSION["start_time"],
            end_time     = _SESSION["end_time"],
        )
        generate_report(report_data, REPORT_HTML)
        print("\n\U0001f4ca  Report → reports/purview_snowflake_connector_report.html")
    except Exception as exc:
        print(f"\n⚠️  Report generation failed: {exc}")
        traceback.print_exc()


def pytest_configure(config):
    for marker in ("comparison", "excluded_objects"):
        config.addinivalue_line("markers", f"{marker}: internal marker")
