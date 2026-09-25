"""
conftest
========
BeforeAll hook: extracts ALL metadata from Snowflake and MS Purview into
data/actual/ and data/expected/ BEFORE a single test function is called.

If Snowflake or Purview cannot be reached, or if any extraction step fails,
pytest.exit() is called immediately and no test runs.

Execution order
---------------
1. clear_results        — wipes and re-creates data/ directories
2. extract_all_data     — establishes connections (exit on failure),
                          then extracts all 6 entity types from both sources
3. Tests run            — pure comparison assertions, no cloud calls
4. pytest_sessionfinish — reads JSONL files → computes diffs + empty-desc
                          → generates the HTML report
"""

import os
import shutil
import time

import pytest
from dotenv import load_dotenv

from clients.purview_client import PurviewClient
from clients.snowflake_client import SnowflakeClient

load_dotenv()

_ACTUAL_DIR   = "data/actual"
_EXPECTED_DIR = "data/expected"

# ── session state (test results collected per-test; rest built at session end) ─
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
        collection_prod=os.environ.get("ENVIRONMENT", "Prod"),
        collection_commercial=os.environ.get("PURVIEW_COLLECTION_COMMERCIAL", "Commercial"),
    )


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def clear_results():
    """BeforeAll step 0: wipe data directories so stale files never pollute a run."""
    for folder in (_ACTUAL_DIR, _EXPECTED_DIR):
        if os.path.exists(folder):
            shutil.rmtree(folder)
        os.makedirs(folder)
    os.makedirs("reports", exist_ok=True)


@pytest.fixture(scope="session", autouse=True)
def extract_all_data(clear_results):
    """
    BeforeAll steps 1-4: connect then extract all metadata.

    No test can start until this fixture's setup block completes.
    Any failure calls pytest.exit() so zero test functions are executed.
    """
    _SESSION["start_time"] = time.time()
    env = _SESSION["environment"]
    sf  = None
    pv  = None

    _log(f"\n{'='*60}")
    _log("  PURVIEW ↔ SNOWFLAKE METADATA VALIDATOR")
    _log(f"  Environment : {env}")
    _log(f"{'='*60}")

    # ── Step 1: Connect Snowflake ─────────────────────────────────────────────
    _log("\n[STEP 1/4] Connecting to Snowflake ...")
    try:
        sf = _create_snowflake_client()
        sf.get_databases()
        _log("           ✅  Snowflake OK")
    except Exception as exc:
        _abort(f"Snowflake connection failed: {exc}")

    # ── Step 2: Connect Purview ───────────────────────────────────────────────
    _log("\n[STEP 2/4] Connecting to MS Purview ...")
    try:
        pv = _create_purview_client()
        pv._resolve_commercial()
        _log("           ✅  Purview OK")
    except Exception as exc:
        _abort(f"Purview connection failed: {exc}", sf=sf)

    # ── Step 3: Extract Snowflake metadata ────────────────────────────────────
    _log("\n[STEP 3/4] Extracting Snowflake metadata ...")
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
    _log("\n[STEP 4/4] Extracting Purview metadata ...")
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

    _log("\n✅  ALL EXTRACTION COMPLETE — running comparison tests ...\n")

    yield

    try:
        sf.close()
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


# ── session finish: build report from JSONL files → generate HTML ─────────────

def pytest_sessionfinish(session, exitstatus):
    import json
    import traceback

    _SESSION["end_time"] = time.time()

    try:
        from utils.report_data_builder import build_report_data
        from utils.report_generator import generate_report

        os.makedirs("reports", exist_ok=True)

        # Persist test results so refresh_report.py can reload them later
        meta = {
            "environment": _SESSION["environment"],
            "start_time":  _SESSION["start_time"],
            "end_time":    _SESSION["end_time"],
            "test_results": _SESSION["test_results"],
        }
        with open("reports/test_results.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        report_data = build_report_data(
            environment  = _SESSION["environment"],
            test_results = _SESSION["test_results"],
            start_time   = _SESSION["start_time"],
            end_time     = _SESSION["end_time"],
        )
        generate_report(report_data, "reports/report.html")
        print("\n\U0001f4ca  Report → reports/report.html")
    except Exception as exc:
        print(f"\n⚠️  Report generation failed: {exc}")
        traceback.print_exc()


# ── minor hooks ───────────────────────────────────────────────────────────────

def pytest_configure(config):
    for marker in ("comparison", "excluded_objects"):
        config.addinivalue_line("markers", f"{marker}: internal marker")
