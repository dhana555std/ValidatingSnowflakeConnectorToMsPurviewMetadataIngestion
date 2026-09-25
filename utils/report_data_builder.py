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
"""

import os
from typing import Any, Dict, List

from utils import comparator as cmp

_ACTUAL_DIR   = "data/actual"
_EXPECTED_DIR = "data/expected"


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


# ── public entry point ────────────────────────────────────────────────────────

def build_report_data(
    environment: str = "Prod",
    test_results: List[Dict[str, Any]] = None,
    start_time: float = None,
    end_time: float = None,
    actual_dir: str = _ACTUAL_DIR,
    expected_dir: str = _EXPECTED_DIR,
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
    """
    import time as _time
    now = _time.time()
    start_time = start_time if start_time is not None else now
    end_time   = end_time   if end_time   is not None else now
    test_results = test_results or []

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
                "missing":  missing,
                "extra":    extra,
                "sf_count": extraction[f"sf_{entity}"]["count"],
                "pv_count": extraction[f"pv_{entity}"]["count"],
            }
        except Exception as exc:
            comparison[entity] = {"error": str(exc)}

    # ── empty descriptions in Purview ─────────────────────────────────────────
    empty_desc: Dict[str, List] = {}
    for entity, path in pv_paths.items():
        records = cmp.load_jsonl(path)
        empty_desc[entity] = [
            {"name": r.get("name", ""), "qualifiedName": r.get("qualifiedName", "")}
            for r in records
            if not (r.get("description") or "").strip()
        ]

    col_records = cmp.load_jsonl_dir(os.path.join(expected_dir, "columns"))
    empty_desc["columns"] = [
        {
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

    return {
        "environment":    environment,
        "start_time":     start_time,
        "end_time":       end_time,
        "session_failed": False,
        "extraction":     extraction,
        "comparison":     comparison,
        "empty_desc":     empty_desc,
        "test_results":   test_results,
    }
