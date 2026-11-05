"""
conftest
========
BeforeAll hook: extracts ALL metadata from Snowflake and MS Purview into
data/actual/ and data/expected/ BEFORE a single test function is called.
 
If Snowflake or Purview cannot be reached, or if any entity extraction step
fails, pytest.exit() is called immediately and no test runs.
 
Snowflake authentication uses key-pair (SNOWFLAKE_PRIVATE_KEY_PATH).
 
Regions (US NonProd / EU NonProd)
---------------------------------
config/regions.json lists the databases per region. One pytest run validates ONE
region:
 
    pytest --region us
    pytest --region eu
    python scripts/run_validation.py          # every region, one after another
 
Settings that differ per region can be given in .env with a region suffix, e.g.
SNOWFLAKE_ACCOUNT_EU, DOMAIN_NAME_EU (see utils/region_config.py). Without
config/regions.json the validator behaves as before (optionally limited by
VALIDATE_DATABASES in .env).
 
Live progress
-------------
pytest normally captures everything printed inside a fixture and only shows it at
the end. _log() suspends capture for each write, so the progress lines below
appear immediately while the (long) extraction runs.
 
_heartbeat() prints a "still running" line every 15 seconds during slow steps
(for example the columns extraction) so the run never looks frozen.
 
Execution order
---------------
1. clear_results        — wipes and re-creates data/ directories
2. extract_all_data     — establishes connections (exit on failure),
                          extracts all 6 entity types from both sources,
                          trims both sides to the region's databases,
                          then extracts collection metadata (informational)
3. Tests run            — pure comparison assertions, no cloud calls
4. pytest_sessionfinish — reads JSONL/JSON files → computes diffs + empty-desc
                          → writes reports/SnowflakeMsPurview-<REGION>-<timestamp>/
"""
 
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from typing import List, Optional
 
import pytest
from dotenv import load_dotenv
 
from clients.purview_client import PurviewClient
from clients.snowflake_client import SnowflakeClient
from utils.region_config import get_setting, load_regions, select_region
 
load_dotenv()
 
_ACTUAL_DIR   = "data/actual"
_EXPECTED_DIR = "data/expected"
_COLLECTION_REPORT_PATH = f"{_EXPECTED_DIR}/purview_collection_commercial.json"
 
# ── session state (test results collected per-test; rest built at session end) ─
_SESSION: dict = {
    "environment":  os.environ.get("ENVIRONMENT", "Prod"),
    "start_time":   None,
    "end_time":     None,
    "test_results": [],
}
 
# ── region state, filled in by pytest_configure ───────────────────────────────
_RUN: dict = {
    "regions":   {},     # everything in config/regions.json
    "key":       None,   # the region this run validates ("us" / "eu" / None)
    "label":     None,   # e.g. "EU NonProd"
    "databases": [],     # databases to keep
}
_STATE: dict = {"config": None}
 
 
# ── helpers ───────────────────────────────────────────────────────────────────
 
_ASCII = {
    "↔": "<->",
    "→": "->",
    "✓": "OK",
    "✅": "OK",
    "❌": "X",
    "⚠️": "!",
    "—": "-",
    "\U0001f4ca": "[REPORT]",
}
 
 
def _ascii(msg: str) -> str:
    """Swap special symbols for plain ASCII so any Windows console shows them."""
    for k, v in _ASCII.items():
        msg = msg.replace(k, v)
    return msg
 
 
def _log(msg: str) -> None:
    """Print right now, even though pytest is capturing output.
 
    Without this, everything printed from a fixture stays hidden until the whole
    run (10+ minutes here) is over.
    """
    capman = None
    cfg = _STATE["config"]
    if cfg is not None:
        try:
            capman = cfg.pluginmanager.get_plugin("capturemanager")
        except Exception:
            capman = None
    if capman is not None:
        try:
            with capman.global_and_fixture_disabled():
                print(_ascii(msg), flush=True)
            return
        except Exception:
            pass
    print(_ascii(msg), flush=True)
 
 
@contextmanager
def _heartbeat(label: str, interval: int = 15):
    """Print a 'still running' line every `interval` seconds while a step runs."""
    stop = threading.Event()
    t0 = time.time()
 
    def _run():
        while not stop.wait(interval):
            _log(f"             ... {label} still running ({int(time.time() - t0)}s)")
 
    threading.Thread(target=_run, daemon=True).start()
    try:
        yield
    finally:
        stop.set()
 
 
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
        f"\n{'='*60}\nX  ABORTING SESSION - no tests will run.\n"
        f"    Reason: {reason}\n{'='*60}",
        returncode=2,
    )
 
 
def _setting(name: str) -> Optional[str]:
    """Env value for *name*, preferring NAME_<REGION> (e.g. SNOWFLAKE_ACCOUNT_EU)."""
    return get_setting(name, _RUN["key"])
 
 
def _need(name: str) -> str:
    value = _setting(name)
    if not value:
        hint = f" (or {name}_{_RUN['key'].upper()})" if _RUN["key"] else ""
        raise RuntimeError(f"Missing setting {name}{hint} in .env")
    return value
 
 
def _databases_to_validate() -> List[str]:
    """Region databases from config/regions.json, else VALIDATE_DATABASES from .env."""
    if _RUN["databases"]:
        return _RUN["databases"]
    return [d.strip() for d in os.environ.get("VALIDATE_DATABASES", "").split(",") if d.strip()]
 
 
def _create_snowflake_client() -> SnowflakeClient:
    extra_raw = os.environ.get("SNOWFLAKE_EXCLUDE_DATABASES", "")
    extra = [db.strip() for db in extra_raw.split(",") if db.strip()]
    return SnowflakeClient(
        account=_need("SNOWFLAKE_ACCOUNT"),
        user=_need("SNOWFLAKE_USER"),
        private_key_path=_need("SNOWFLAKE_PRIVATE_KEY_PATH"),
        private_key_passphrase=_setting("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None,
        warehouse=_setting("SNOWFLAKE_WAREHOUSE") or None,
        database=_setting("SNOWFLAKE_DATABASE") or None,
        role=_setting("SNOWFLAKE_ROLE") or "ACCOUNTADMIN",
        exclude_databases=extra,
        host=_setting("SNOWFLAKE_HOST") or None,
    )
 
 
def _purview_collection() -> str:
    """Parent collection to validate (e.g. NonProd).
 
    PURVIEW_COLLECTION (or PURVIEW_COLLECTION_<REGION>) wins; otherwise the
    region's "environment" from regions.json, then ENVIRONMENT, then "Prod".
    """
    return (_setting("PURVIEW_COLLECTION") or _SESSION["environment"] or "Prod").strip()
 
 
def _region_prefix() -> str:
    """Prefix put in front of every object name in the report, e.g. US_NonProd."""
    if not _RUN["key"]:
        return ""
    return f"{_RUN['key'].upper()}_{_SESSION['environment']}"
 
 
def _collection_tree() -> list:
    """Collection hierarchy for EVERY region in config/regions.json.
 
    Used by the report to draw one tree (US and EU side by side). A region may set
    "collection_root" in regions.json (for example "Vantive EU"); otherwise the
    root is "Vantive <KEY>".
    """
    commercial = (_setting("PURVIEW_COLLECTION_COMMERCIAL") or "Commercial").strip()
    tree = []
    for key, region in _RUN["regions"].items():
        tree.append({
            "key":         key.upper(),
            "label":       region.get("label") or key.upper(),
            "root":        region.get("collection_root") or f"Vantive {key.upper()}",
            "environment": region.get("environment") or "NonProd",
            "collection":  commercial,
            "current":     key == _RUN["key"],
        })
    return tree
 
 
def _create_purview_client() -> PurviewClient:
    return PurviewClient(
        account_name=_need("PURVIEW_ACCOUNT_NAME"),
        tenant_id=_need("AZURE_TENANT_ID"),
        client_id=_need("AZURE_CLIENT_ID"),
        client_secret=_need("AZURE_CLIENT_SECRET"),
        domain_name=_need("DOMAIN_NAME"),
        collection_prod=_purview_collection(),
        collection_commercial=_setting("PURVIEW_COLLECTION_COMMERCIAL") or "Commercial",
    )
 
 
# ── optional: validate only a chosen set of databases ─────────────────────────
 
_DB_KEYS = ("DATABASE_NAME", "TABLE_CATALOG", "PROCEDURE_CATALOG", "CATALOG_NAME", "database_name")
_DB_IN_QN = re.compile(r"/databases/([^/]+)", re.IGNORECASE)
 
 
def _record_db(rec: dict, is_db_file: bool) -> Optional[str]:
    """Database a record belongs to (upper-case), or None if it can't be told."""
    for k in _DB_KEYS:
        if rec.get(k):
            return str(rec[k]).upper()
    m = _DB_IN_QN.search(rec.get("qualifiedName") or "")
    if m:
        return m.group(1).upper()
    if is_db_file and rec.get("name"):
        return str(rec["name"]).upper()
    return None
 
 
def _filter_to_databases(names: List[str], actual_dir: str = _ACTUAL_DIR,
                         expected_dir: str = _EXPECTED_DIR) -> None:
    """Rewrite every .jsonl under data/actual and data/expected, keeping only
    records that belong to the given databases (both Snowflake and Purview sides)."""
    keep = {n.strip().upper() for n in names if n.strip()}
    if not keep:
        return
    _log(f"\n[FILTER]   Keeping only {len(keep)} database(s): {', '.join(sorted(keep))}")
    for root in (actual_dir, expected_dir):
        kept_total = seen_total = 0
        for dirpath, _, files in os.walk(root):
            for fname in files:
                if not fname.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, fname)
                tmp = path + ".tmp"
                is_db_file = fname in ("databases.jsonl", "purview_databases.jsonl")
                with open(path, encoding="utf-8") as src, open(tmp, "w", encoding="utf-8") as dst:
                    for line in src:
                        if not line.strip():
                            continue
                        seen_total += 1
                        if _record_db(json.loads(line), is_db_file) in keep:
                            dst.write(line if line.endswith("\n") else line + "\n")
                            kept_total += 1
                os.replace(tmp, path)
        _log(f"           {root}: kept {kept_total:,} of {seen_total:,} records")
 
 
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
    BeforeAll steps 1-5: connect then extract all metadata.
 
    No test can start until this fixture's setup block completes.
    Failures in steps 1-4 call pytest.exit() so zero test functions are
    executed.  Step 5 (collection metadata) is informational: a failure is
    logged and recorded in the JSON, but tests still run.
    """
    if _RUN["regions"] and not _RUN["key"]:
        _abort(
            "Several regions are configured in config/regions.json "
            f"({', '.join(sorted(_RUN['regions']))}). "
            "Run with --region <name>, or use: python scripts/run_validation.py"
        )
 
    _SESSION["start_time"] = time.time()
    env = _SESSION["environment"]
    sf  = None
    pv  = None
 
    _log(f"\n{'='*60}")
    _log("  PURVIEW ↔ SNOWFLAKE METADATA VALIDATOR")
    if _RUN["label"]:
        _log(f"  Region      : {_RUN['label']}  ({len(_RUN['databases'])} database(s))")
    _log(f"  Environment : {env}")
    _log(f"{'='*60}")
 
    # ── Step 1: Connect Snowflake (key-pair auth) ─────────────────────────────
    _log("\n[STEP 1/5] Connecting to Snowflake (key-pair) ...")
    t = time.time()
    try:
        sf = _create_snowflake_client()
        sf.get_databases()
        _log(f"           ✅  Snowflake OK  ({time.time() - t:.1f}s)")
    except Exception as exc:
        _abort(f"Snowflake connection failed: {exc}")
 
    # ── Step 2: Connect Purview ───────────────────────────────────────────────
    _log("\n[STEP 2/5] Connecting to MS Purview ...")
    t = time.time()
    try:
        pv = _create_purview_client()
        pv._resolve_commercial()
        _log(f"           ✅  Purview OK  ({time.time() - t:.1f}s)")
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
        t = time.time()
        try:
            with _heartbeat(f"Snowflake {label}"):
                result = method()
            _log(f"             ✓ {result['count']:,} records  ({time.time() - t:.1f}s)")
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
        t = time.time()
        try:
            with _heartbeat(f"Purview {label}"):
                result = method()
            _log(f"             ✓ {result['count']:,} records  ({time.time() - t:.1f}s)")
        except Exception as exc:
            _abort(f"Purview extraction failed ({label}): {exc}", sf=sf, pv=pv)
 
    # ── Trim both sides to the region's databases (config/regions.json) ───────
    try:
        _filter_to_databases(_databases_to_validate())
    except Exception as exc:
        _abort(f"Database filter failed: {exc}", sf=sf, pv=pv)
 
    # ── Step 5: Collection metadata for the configured environment (non-fatal) ─
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
        # Informational only — never block the comparison tests over this.
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
 
 
# ── session finish: build report from JSONL files → generate HTML ─────────────
 
def pytest_sessionfinish(session, exitstatus):
    import traceback
 
    if _SESSION["start_time"] is None:   # extraction never ran (e.g. --collect-only)
        return
 
    _SESSION["end_time"] = time.time()
 
    try:
        from utils.report_data_builder import build_report_data
        from utils.report_generator import create_run_dir, generate_report
 
        report_data = build_report_data(
            environment  = _SESSION["environment"],
            test_results = _SESSION["test_results"],
            start_time   = _SESSION["start_time"],
            end_time     = _SESSION["end_time"],
            prefix       = _region_prefix(),
        )
        report_data["region"]       = _RUN["key"]
        report_data["region_label"] = _RUN["label"]
        report_data["collection_tree"] = _collection_tree()
 
        # reports/SnowflakeMsPurview-<REGION>-<timestamp>/  — html, css, js, images, jsonl
        tag = _RUN["key"].upper() if _RUN["key"] else None
        run_dir = create_run_dir("reports", tag=tag)
        report_path = generate_report(report_data, run_dir)
        _log(f"\n\U0001f4ca  Report folder → {run_dir}")
        _log(f"    Open → {report_path}")
    except Exception as exc:
        _log(f"\n⚠️  Report generation failed: {exc}")
        traceback.print_exc()
 
 
# ── options and minor hooks ───────────────────────────────────────────────────
 
def pytest_addoption(parser):
    parser.addoption(
        "--region",
        action="store",
        default=None,
        help="Region to validate: a key from config/regions.json (for example us or eu).",
    )
 
 
def pytest_configure(config):
    _STATE["config"] = config
 
    for marker in ("comparison", "excluded_objects"):
        config.addinivalue_line("markers", f"{marker}: internal marker")
 
    try:
        regions = load_regions()
        key = select_region(
            regions,
            config.getoption("--region") or os.environ.get("VALIDATION_REGION"),
        )
    except ValueError as exc:
        raise pytest.UsageError(str(exc))
 
    _RUN["regions"] = regions
    _RUN["key"] = key
    if key:
        region = regions[key]
        _RUN["label"] = region["label"]
        _RUN["databases"] = region["databases"]
        if region.get("environment"):
            _SESSION["environment"] = region["environment"]