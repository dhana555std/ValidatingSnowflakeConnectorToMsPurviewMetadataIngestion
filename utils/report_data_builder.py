"""
report_data_builder
===================
Builds the _REPORT_DATA dict from on-disk JSONL files.
 
Used by both:
  - conftest.pytest_sessionfinish  (after a live pytest run)
  - scripts/refresh_report.py      (standalone regeneration from existing data)
 
All counts are derived directly from the JSONL files so they always match
what was actually written during extraction, regardless of what the extraction
methods reported at runtime.
 
Region prefix
-------------
When a `prefix` such as "US_NonProd" or "EU_NonProd" is passed, every object
name shown in the report is prefixed with it:
 
    US_NonProd.<DB>
    US_NonProd.<DB>.<SCHEMA>
    US_NonProd.<DB>.<SCHEMA>.<TABLE>
 
The prefix is applied here, on the report side only. The extracted JSONL files
and the Snowflake/Purview matching are not changed, so comparison results stay
exactly the same. Without a prefix, names are shown as before.
"""
 
import os
import json
from typing import Any, Dict, List
 
from utils import comparator as cmp
 
_ACTUAL_DIR   = "data/actual"
_EXPECTED_DIR = "data/expected"
_COLLECTION_FILE = "purview_collection_commercial.json"
 
# Display order in the report — anything else sorts after these, alphabetically
_ENV_ORDER = {"Prod": 0, "NonProd": 1}
 
 
def _count_jsonl(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())
 
 
def _count_jsonl_dir(directory: str) -> int:
    if not os.path.isdir(directory):
        return 0
    total = 0
    for root, _, files in os.walk(directory):
        for fname in files:
            if fname.endswith(".jsonl"):
                total += _count_jsonl(os.path.join(root, fname))
    return total
 
 
def _with_prefix(prefix: str, name: str) -> str:
    """'US_NonProd' + 'DB.SCHEMA' -> 'US_NonProd.DB.SCHEMA' (unchanged when no prefix)."""
    return f"{prefix}.{name}" if prefix and name else name
 
 
def _full_name(entity: str, rec: Dict[str, Any], prefix: str) -> str:
    """Prefixed, dotted name of a Purview record, built the same way the
    comparator builds its keys (so names in the report line up with missing/extra)."""
    key_fns = {
        "databases":         getattr(cmp, "_pv_db_key", None),
        "schemas":           getattr(cmp, "_pv_schema_key", None),
        "tables":            getattr(cmp, "_pv_table_key", None),
        "views":             getattr(cmp, "_pv_view_key", None),
        "stored_procedures": getattr(cmp, "_pv_sp_key", None),
        "columns":           getattr(cmp, "_pv_col_key", None),
    }
    fn = key_fns.get(entity)
    name = fn(rec) if fn else ""
 
    if not name and entity == "columns":
        parts = [
            rec.get("database_name", ""),
            rec.get("schema_name", ""),
            rec.get("table_name", ""),
            rec.get("name", ""),
        ]
        name = ".".join(str(p).upper() for p in parts if p)
    if not name:
        name = str(rec.get("name", "")).upper()
 
    return _with_prefix(prefix, name)
 
 
def _load_collection_metadata(expected_dir: str) -> Dict[str, Any]:
    """Read the Commercial collection metadata written in conftest Step 5.
 
    Always returns the same shape, so the report generator never has to guess:
 
        {
          "generated_at": str | None,
          "purview_account": str | None,
          "validated_environment": str | None,
          "collections": [ {environment, collection_name, ..., asset_counts}, ... ],
          "error": str            # only if the file is missing or unreadable
        }
 
    Older runs wrote a single collection dict instead of a list; that format
    is still accepted so refresh_report.py works on old data folders.
    """
    empty = {
        "generated_at": None,
        "purview_account": None,
        "validated_environment": None,
        "collections": [],
    }
    path = os.path.join(expected_dir, _COLLECTION_FILE)
    if not os.path.exists(path):
        return {**empty, "error": f"{_COLLECTION_FILE} not found — collection step did not run"}
 
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {**empty, "error": f"Could not read {_COLLECTION_FILE}: {exc}"}
 
    if isinstance(data, dict) and "collections" in data:          # current format
        result = {**empty, **data}
    elif isinstance(data, dict) and "environment" in data:        # old single-collection format
        result = {**empty, "collections": [data]}
    else:
        return {**empty, "error": f"Unrecognised format in {_COLLECTION_FILE}"}
 
    result["collections"] = sorted(
        result.get("collections") or [],
        key=lambda c: (_ENV_ORDER.get(c.get("environment"), 99), str(c.get("environment"))),
    )
    return result
 
 
# ── public entry point ────────────────────────────────────────────────────────
 
def build_report_data(
    environment: str = "Prod",
    test_results: List[Dict[str, Any]] = None,
    start_time: float = None,
    end_time: float = None,
    actual_dir: str = _ACTUAL_DIR,
    expected_dir: str = _EXPECTED_DIR,
    prefix: str = "",
) -> Dict[str, Any]:
    """Read JSONL files and return a fully-populated report data dict.
 
    Parameters
    ----------
    environment:
        Value of the ENVIRONMENT env var (Prod / NonProd).
    test_results:
        List of per-test dicts collected by conftest; empty list when called
        standalone (refresh_report.py).
    start_time / end_time:
        Unix timestamps from the pytest session; set to now when standalone.
    actual_dir / expected_dir:
        Override data directories (useful for tests).
    prefix:
        Region prefix such as "US_NonProd" / "EU_NonProd". Put in front of every
        object name in the report and shown as the report's Account.
    """
    import time as _time
    now = _time.time()
    start_time = start_time if start_time is not None else now
    end_time   = end_time   if end_time   is not None else now
    test_results = test_results or []
    prefix = (prefix or "").strip()
 
    # ── extraction counts (from JSONL files, not from runtime memory) ─────────
    sf_paths = {
        "databases":         os.path.join(actual_dir, "databases.jsonl"),
        "schemas":           os.path.join(actual_dir, "schemas.jsonl"),
        "tables":            os.path.join(actual_dir, "tables.jsonl"),
        "views":             os.path.join(actual_dir, "views.jsonl"),
        "stored_procedures": os.path.join(actual_dir, "stored_procedures.jsonl"),
    }
    pv_paths = {
        "databases":         os.path.join(expected_dir, "purview_databases.jsonl"),
        "schemas":           os.path.join(expected_dir, "purview_schemas.jsonl"),
        "tables":            os.path.join(expected_dir, "purview_tables.jsonl"),
        "views":             os.path.join(expected_dir, "purview_views.jsonl"),
        "stored_procedures": os.path.join(expected_dir, "purview_stored_procedures.jsonl"),
    }
 
    extraction: Dict[str, Dict] = {}
    for entity, path in sf_paths.items():
        extraction[f"sf_{entity}"] = {"count": _count_jsonl(path), "output": path}
    extraction["sf_columns"] = {
        "count":      _count_jsonl_dir(os.path.join(actual_dir, "columns")),
        "output_dir": os.path.join(actual_dir, "columns"),
    }
    for entity, path in pv_paths.items():
        extraction[f"pv_{entity}"] = {"count": _count_jsonl(path), "output": path}
    extraction["pv_columns"] = {
        "count":      _count_jsonl_dir(os.path.join(expected_dir, "columns")),
        "output_dir": os.path.join(expected_dir, "columns"),
    }
 
    # ── bidirectional comparisons ─────────────────────────────────────────────
    entity_fns = {
        "databases":         cmp.compare_databases,
        "schemas":           cmp.compare_schemas,
        "tables":            cmp.compare_tables,
        "views":             cmp.compare_views,
        "columns":           cmp.compare_columns,
        "stored_procedures": cmp.compare_stored_procedures,
    }
    comparison: Dict[str, Any] = {}
    for entity, fn in entity_fns.items():
        try:
            missing, extra = fn(actual_dir, expected_dir)
            comparison[entity] = {
                "missing":  [_with_prefix(prefix, k) for k in missing],
                "extra":    [_with_prefix(prefix, k) for k in extra],
                "sf_count": extraction[f"sf_{entity}"]["count"],
                "pv_count": extraction[f"pv_{entity}"]["count"],
            }
        except Exception as exc:
            comparison[entity] = {"error": str(exc)}
 
    # ── empty descriptions in Purview ─────────────────────────────────────────
    # "fullName" is the prefixed, dotted name (for example US_NonProd.DB.SCHEMA.TABLE)
    empty_desc: Dict[str, List] = {}
    for entity, path in pv_paths.items():
        records = cmp.load_jsonl(path)
        empty_desc[entity] = [
            {
                "fullName":      _full_name(entity, r, prefix),
                "name":          r.get("name", ""),
                "qualifiedName": r.get("qualifiedName", ""),
            }
            for r in records
            if not (r.get("description") or "").strip()
        ]
 
    col_records = cmp.load_jsonl_dir(os.path.join(expected_dir, "columns"))
    empty_desc["columns"] = [
        {
            "fullName":      _full_name("columns", r, prefix),
            "name":          r.get("name", ""),
            "qualifiedName": r.get("qualifiedName", ""),
            "database_name": r.get("database_name", ""),
            "schema_name":   r.get("schema_name", ""),
            "table_name":    r.get("table_name", ""),
            "dataType":      r.get("dataType", ""),
        }
        for r in col_records
        if not (r.get("description") or "").strip()
    ]
 
    # ── Commercial collection metadata (Prod + NonProd) ───────────────────────
    collection_metadata = _load_collection_metadata(expected_dir)
 
    return {
        "environment":         environment,
        "account":             prefix or None,
        "name_prefix":         prefix,
        "start_time":          start_time,
        "end_time":            end_time,
        "session_failed":      False,
        "extraction":          extraction,
        "comparison":          comparison,
        "empty_desc":          empty_desc,
        "test_results":        test_results,
        "collection_metadata": collection_metadata,
    }
 