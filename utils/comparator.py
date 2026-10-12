"""Comparison utilities for Snowflake vs Purview metadata validation."""
import json
import os
from typing import Any, Dict, List, Set, Tuple


# ── loaders ───────────────────────────────────────────────────────────────────

def load_jsonl(path: str) -> List[Dict[str, Any]]:
    records = []
    if not os.path.exists(path):
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_jsonl_dir(dir_path: str) -> List[Dict[str, Any]]:
    """Load all .jsonl files under a directory tree (sorted for determinism)."""
    records = []
    if not os.path.isdir(dir_path):
        return records
    for root, _, files in os.walk(dir_path):
        for fname in sorted(files):
            if fname.endswith(".jsonl"):
                records.extend(load_jsonl(os.path.join(root, fname)))
    return records


# ── qualifiedName parser ──────────────────────────────────────────────────────

def parse_qualified_name(qn: str) -> Dict[str, str]:
    """Extract database/schema/table/view/column names from a Purview qualifiedName."""
    parts = qn.split("/")
    result: Dict[str, str] = {}
    for segment in ("databases", "schemas", "tables", "views", "columns"):
        if segment in parts:
            idx = parts.index(segment)
            if idx + 1 < len(parts):
                result[segment.rstrip("s")] = parts[idx + 1].upper()
    return result


# ── key extractors ────────────────────────────────────────────────────────────

def _sf_db_key(r: Dict) -> str:
    return r.get("DATABASE_NAME", "").upper()

def _pv_db_key(r: Dict) -> str:
    p = parse_qualified_name(r.get("qualifiedName", ""))
    return p.get("database", "")

def _sf_schema_key(r: Dict) -> str:
    return f"{r.get('DATABASE_NAME','')}.{r.get('SCHEMA_NAME','')}".upper()

def _pv_schema_key(r: Dict) -> str:
    p = parse_qualified_name(r.get("qualifiedName", ""))
    return f"{p.get('database','')}.{p.get('schema','')}".upper() if p.get("schema") else ""

def _sf_table_key(r: Dict) -> str:
    return f"{r.get('TABLE_CATALOG','')}.{r.get('TABLE_SCHEMA','')}.{r.get('TABLE_NAME','')}".upper()

def _pv_table_key(r: Dict) -> str:
    p = parse_qualified_name(r.get("qualifiedName", ""))
    db, sc, tb = p.get("database",""), p.get("schema",""), p.get("table","")
    return f"{db}.{sc}.{tb}".upper() if tb else ""

def _sf_view_key(r: Dict) -> str:
    return f"{r.get('TABLE_CATALOG','')}.{r.get('TABLE_SCHEMA','')}.{r.get('TABLE_NAME','')}".upper()

def _pv_view_key(r: Dict) -> str:
    p = parse_qualified_name(r.get("qualifiedName", ""))
    db, sc, vw = p.get("database",""), p.get("schema",""), p.get("view","") or p.get("table","")
    return f"{db}.{sc}.{vw}".upper() if (sc and vw) else ""

def _sf_col_key(r: Dict) -> str:
    return (
        f"{r.get('TABLE_CATALOG','')}.{r.get('TABLE_SCHEMA','')}"
        f".{r.get('TABLE_NAME','')}.{r.get('COLUMN_NAME','')}".upper()
    )

def _pv_col_key(r: Dict) -> str:
    p = parse_qualified_name(r.get("qualifiedName", ""))
    db = p.get("database","")
    sc = p.get("schema","")
    tb = p.get("table","") or p.get("view","")
    col = p.get("column","")
    return f"{db}.{sc}.{tb}.{col}".upper() if col else ""


# ── generic diff ──────────────────────────────────────────────────────────────

def diff_sets(
    sf_records: List[Dict],
    pv_records: List[Dict],
    sf_key_fn,
    pv_key_fn,
) -> Tuple[List[str], List[str]]:
    """
    Returns (missing_from_purview, extra_in_purview) as sorted key lists.
    """
    sf_keys: Set[str] = {sf_key_fn(r) for r in sf_records if sf_key_fn(r)}
    pv_keys: Set[str] = {pv_key_fn(r) for r in pv_records if pv_key_fn(r)}
    missing = sorted(sf_keys - pv_keys)
    extra   = sorted(pv_keys - sf_keys)
    return missing, extra


# ── per-entity comparisons ────────────────────────────────────────────────────

def compare_databases(actual_dir: str, expected_dir: str):
    sf = load_jsonl(os.path.join(actual_dir, "snowflake_databases.jsonl"))
    pv = load_jsonl(os.path.join(expected_dir, "purview_databases.jsonl"))
    return diff_sets(sf, pv, _sf_db_key, _pv_db_key)


def compare_schemas(actual_dir: str, expected_dir: str):
    sf = load_jsonl(os.path.join(actual_dir, "snowflake_schemas.jsonl"))
    pv = load_jsonl(os.path.join(expected_dir, "purview_schemas.jsonl"))
    return diff_sets(sf, pv, _sf_schema_key, _pv_schema_key)


def compare_tables(actual_dir: str, expected_dir: str):
    sf = load_jsonl(os.path.join(actual_dir, "snowflake_tables.jsonl"))
    pv = load_jsonl(os.path.join(expected_dir, "purview_tables.jsonl"))
    return diff_sets(sf, pv, _sf_table_key, _pv_table_key)


def compare_views(actual_dir: str, expected_dir: str):
    sf = load_jsonl(os.path.join(actual_dir, "snowflake_views.jsonl"))
    pv = load_jsonl(os.path.join(expected_dir, "purview_views.jsonl"))
    return diff_sets(sf, pv, _sf_view_key, _pv_view_key)


def _sf_sp_key(r: Dict) -> str:
    return (
        f"{r.get('PROCEDURE_CATALOG','')}.{r.get('PROCEDURE_SCHEMA','')}"
        f".{r.get('PROCEDURE_NAME','')}".upper()
    )

def _pv_sp_key(r: Dict) -> str:
    p = parse_qualified_name(r.get("qualifiedName", ""))
    db = p.get("database","")
    sc = p.get("schema","")
    name = r.get("name","").upper()
    return f"{db}.{sc}.{name}".upper() if (db and sc and name) else ""


def compare_stored_procedures(actual_dir: str, expected_dir: str):
    sf = load_jsonl(os.path.join(actual_dir, "snowflake_stored_procedures.jsonl"))
    pv = load_jsonl(os.path.join(expected_dir, "purview_stored_procedures.jsonl"))
    return diff_sets(sf, pv, _sf_sp_key, _pv_sp_key)


def compare_columns(actual_dir: str, expected_dir: str):
    sf = load_jsonl_dir(os.path.join(actual_dir, "columns"))
    pv = load_jsonl_dir(os.path.join(expected_dir, "columns"))
    return diff_sets(sf, pv, _sf_col_key, _pv_col_key)


# ── excluded objects check ────────────────────────────────────────────────────

def find_excluded_in_purview(
    expected_dir: str,
    exclude_databases: List[str],
) -> Dict[str, List[Dict]]:
    """
    Returns a dict mapping entity_type → list of Purview records whose
    qualifiedName references an excluded database.
    """
    excluded_upper = {db.upper() for db in exclude_databases}
    results: Dict[str, List[Dict]] = {}

    checks = {
        "databases": os.path.join(expected_dir, "purview_databases.jsonl"),
        "schemas":   os.path.join(expected_dir, "purview_schemas.jsonl"),
        "tables":    os.path.join(expected_dir, "purview_tables.jsonl"),
        "views":     os.path.join(expected_dir, "purview_views.jsonl"),
    }

    for entity_type, path in checks.items():
        records = load_jsonl(path)
        leaking = []
        for r in records:
            p = parse_qualified_name(r.get("qualifiedName", ""))
            if p.get("database", "").upper() in excluded_upper:
                leaking.append(r)
        if leaking:
            results[entity_type] = leaking

    # also check columns dir
    col_records = load_jsonl_dir(os.path.join(expected_dir, "columns"))
    leaking_cols = [
        r for r in col_records
        if (r.get("database_name") or "").upper() in excluded_upper
    ]
    if leaking_cols:
        results["columns"] = leaking_cols

    return results


# ── empty description helpers ─────────────────────────────────────────────────

def find_purview_columns_empty_description(expected_dir: str) -> List[Dict]:
    records = load_jsonl_dir(os.path.join(expected_dir, "columns"))
    return [
        r for r in records
        if not (r.get("description") or "").strip()
    ]


def find_snowflake_columns_empty_comment(actual_dir: str) -> List[Dict]:
    records = load_jsonl_dir(os.path.join(actual_dir, "columns"))
    return [
        r for r in records
        if not (r.get("COMMENT") or "").strip()
    ]
