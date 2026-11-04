"""
report_generator
================
Builds the Purview ↔ Snowflake validation report as a folder:
 
    reports/SnowflakeMsPurview-<YYYYMMDD_HHMMSS>/
        report.html              rendered from templates/report.html.j2
        css/report.css           rendered from templates/report.css.j2
        js/report.js             rendered from templates/report.js.j2
        js/manifest.js           dataset index (counts, headers, shard counts)
        js/data/<name>.NNNNN.js  paged data shards loaded on demand by the page
        images/                  copied from static/images/ (logo, favicon, icons)
        jsonl/                   downloadable data: summary, test_results, run_meta,
                                 and <entity>_missing / _extra / _empty_description
 
This module contains Python only. All markup lives in templates/*.j2 and all
images in static/images/.
 
Why the page no longer runs out of memory
-----------------------------------------
Large row sets are not written into the HTML. They are written as JSONL (for
download) and as small JS shards that the page loads one at a time, showing
PAGE_SIZE rows per page. The browser only ever holds one shard.
"""
 
import datetime
import json
import os
import shutil
from typing import Any, Dict, Iterable, List, Optional, Tuple
 
from jinja2 import Environment, FileSystemLoader, select_autoescape
 
# ── layout / paging settings ──────────────────────────────────────────────────
RUN_PREFIX  = "SnowflakeMsPurview-"
PAGE_SIZE   = 100      # rows shown per page in the browser
SHARD_SIZE  = 2_000    # rows per data shard (must be a multiple of PAGE_SIZE)
SEARCH_CAP  = 5_000    # max matches kept by the in-page filter
 
assert SHARD_SIZE % PAGE_SIZE == 0, "SHARD_SIZE must be a multiple of PAGE_SIZE"
 
_ROOT             = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR      = os.path.join(_ROOT, "templates")
STATIC_IMAGES_DIR = os.path.join(_ROOT, "static", "images")
 
# ── theme (injected into the CSS and HTML templates) ──────────────────────────
THEME: Dict[str, Any] = {
    "navy": "#1B3A5C", "blue": "#2C6FAC", "blue_lt": "#E8F0F8",
    "accent": "#E07B29", "white": "#FFFFFF", "bg": "#F5F7FA",
    "text": "#1A2636", "muted": "#6B7A8D", "success": "#1B7A3E",
    "error": "#B5291C", "warn_bg": "#FFF4E0", "err_bg": "#FDECEA",
    "ok_bg": "#EAF7ED",
    # environment → (strong colour, light background)
    "env_colours": {"Prod": ("#1B3A5C", "#E3ECF6"), "NonProd": ("#E07B29", "#FDF0E4")},
    "env_default": ("#6B7A8D", "#F0F0F0"),
}
 
ENTITY_LABELS = {
    "databases":         "Databases",
    "schemas":           "Schemas",
    "tables":            "Tables",
    "views":             "Views",
    "columns":           "Columns",
    "stored_procedures": "Stored Procedures",
}
ENTITY_ICONS = {
    "databases": "🗄️", "schemas": "📂", "tables": "📋",
    "views": "👁️", "columns": "🔠", "stored_procedures": "⚙️",
}
 
# Empty-description rows: which record fields become table columns
_EMPTY_COLUMNS_FIELDS  = (["database_name", "schema_name", "table_name", "name", "dataType"],
                          ["Database", "Schema", "Table", "Column", "Data Type"])
_EMPTY_DEFAULT_FIELDS  = (["name", "qualifiedName"], ["Name", "Qualified Name"])
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Run-folder helpers
# ══════════════════════════════════════════════════════════════════════════════
 
def create_run_dir(base: str = "reports", when: Optional[datetime.datetime] = None) -> str:
    """Create reports/SnowflakeMsPurview-<timestamp>/ with its sub-folders."""
    stamp = (when or datetime.datetime.now()).strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(base, f"{RUN_PREFIX}{stamp}")
    _ensure_layout(run_dir)
    return run_dir
 
 
def find_latest_run_dir(base: str = "reports") -> Optional[str]:
    """Newest run folder under *base* (timestamps sort lexicographically)."""
    if not os.path.isdir(base):
        return None
    runs = sorted(
        d for d in os.listdir(base)
        if d.startswith(RUN_PREFIX) and os.path.isdir(os.path.join(base, d))
    )
    return os.path.join(base, runs[-1]) if runs else None
 
 
def _ensure_layout(run_dir: str) -> None:
    for sub in ("css", "images", "jsonl", os.path.join("js", "data")):
        os.makedirs(os.path.join(run_dir, sub), exist_ok=True)
 
 
# ══════════════════════════════════════════════════════════════════════════════
# JSONL persistence (test results + run metadata)
# ══════════════════════════════════════════════════════════════════════════════
 
def _write_jsonl(path: str, records: Iterable[Dict[str, Any]]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            count += 1
    return count
 
 
def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
 
 
def write_test_results(run_dir: str, data: Dict[str, Any]) -> None:
    """jsonl/test_results.jsonl (one test per line) + jsonl/run_meta.jsonl."""
    _ensure_layout(run_dir)
    _write_jsonl(os.path.join(run_dir, "jsonl", "test_results.jsonl"),
                 data.get("test_results") or [])
    _write_jsonl(os.path.join(run_dir, "jsonl", "run_meta.jsonl"), [{
        "environment": data.get("environment"),
        "start_time":  data.get("start_time"),
        "end_time":    data.get("end_time"),
    }])
 
 
def load_test_results(run_dir: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read back (test_results, meta) from a run folder; empty if not present."""
    results = _read_jsonl(os.path.join(run_dir, "jsonl", "test_results.jsonl"))
    meta = (_read_jsonl(os.path.join(run_dir, "jsonl", "run_meta.jsonl")) or [{}])[0]
    return results, meta
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Dataset writer: one pass → .jsonl (download) + JS shards (browser paging)
# ══════════════════════════════════════════════════════════════════════════════
 
def _cell(v: Any) -> str:
    return "" if v is None else (v if isinstance(v, str) else str(v))
 
 
def _write_shard(run_dir: str, name: str, idx: int, rows: List[List[str]]) -> None:
    path = os.path.join(run_dir, "js", "data", f"{name}.{idx:05d}.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'__rpt_cb({json.dumps(name)},{idx},')
        f.write(json.dumps(rows, separators=(",", ":")))   # ensure_ascii → script-safe
        f.write(");\n")
 
 
def _write_dataset(
    run_dir: str,
    name: str,
    headers: List[str],
    fields: List[str],
    records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Write <name>.jsonl and its shards. Returns the manifest entry."""
    entry = {"name": name, "file": f"{name}.jsonl", "count": 0, "shards": 0,
             "headers": headers}
    if not records:
        return entry
 
    chunk: List[List[str]] = []
    shard_idx = 0
    jsonl_path = os.path.join(run_dir, "jsonl", entry["file"])
    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for rec in records:
            jf.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            chunk.append([_cell(rec.get(f)) for f in fields])
            entry["count"] += 1
            if len(chunk) >= SHARD_SIZE:
                _write_shard(run_dir, name, shard_idx, chunk)
                shard_idx += 1
                chunk = []
    if chunk:
        _write_shard(run_dir, name, shard_idx, chunk)
        shard_idx += 1
    entry["shards"] = shard_idx
    return entry
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Formatting helpers (used as template inputs)
# ══════════════════════════════════════════════════════════════════════════════
 
# Purview stores UTC; the Purview UI shows local (India) time, so the report does too.
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
 
 
def _fmt_ts(value: Any) -> str:
    """ISO UTC timestamp → 'May 13, 2026, 6:38 PM IST'."""
    if not value:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        dt = dt.astimezone(_IST)
        hour = dt.hour % 12 or 12
        return f"{dt:%B} {dt.day}, {dt.year}, {hour}:{dt:%M} {dt:%p} IST"
    except ValueError:
        return str(value)
 
 
def _fmt_principal(principal_id: Any, principal_type: Any) -> str:
    if not principal_id:
        return "—"
    return f"{principal_id} ({principal_type})" if principal_type else str(principal_id)
 
 
def _collection_for_env(data: dict) -> Optional[Dict[str, Any]]:
    """The collection validated in this run (PURVIEW_COLLECTION in .env)."""
    cols = (data.get("collection_metadata") or {}).get("collections") or []
    flagged = [c for c in cols if c.get("validated_in_this_run")]
    if flagged:
        return flagged[0]
    env = str(data.get("environment", "")).lower()
    match = next((c for c in cols if str(c.get("environment", "")).lower() == env), None)
    if match:
        return match
    return cols[0] if len(cols) == 1 else None
 
 
def _collection_view(data: dict) -> Tuple[Optional[Dict[str, Any]], str]:
    """Template-ready collection dict, or (None, reason)."""
    col = _collection_for_env(data)
    meta = data.get("collection_metadata") or {}
    if col is None:
        env = data.get("environment", "")
        return None, meta.get("error") or f"No collection metadata found for environment '{env}'."
    path = col.get("collection_path") or col.get("collection_name") or ""
    return {
        "environment": col.get("environment") or data.get("environment", ""),
        "name":        col.get("collection_name"),
        "id":          col.get("collection_id"),
        "path":        path,
        "path_parts":  [p for p in str(path).split("/") if p],
        "created_by":  _fmt_principal(col.get("created_by"), col.get("created_by_type")),
        "created_at":  _fmt_ts(col.get("created_at")),
        "updated_by":  _fmt_principal(col.get("updated_by"), col.get("updated_by_type")),
        "updated_at":  _fmt_ts(col.get("updated_at")),
        "error":       col.get("error"),
    }, ""
 
 
# ══════════════════════════════════════════════════════════════════════════════
# View-model builders
# ══════════════════════════════════════════════════════════════════════════════
 
def _entity_view(entity: str, data: dict, run_dir: str) -> Dict[str, Any]:
    label = ENTITY_LABELS[entity]
    cmp_  = (data.get("comparison") or {}).get(entity, {})
    ext   = data.get("extraction") or {}
    sf    = ext.get(f"sf_{entity}", {}).get("count", 0)
    pv    = ext.get(f"pv_{entity}", {}).get("count", 0)
 
    view: Dict[str, Any] = {
        "key": entity, "tab_id": "tab-" + entity.replace("_", "-"),
        "label": label, "icon": ENTITY_ICONS.get(entity, ""),
        "error": cmp_.get("error"), "has_data": bool(cmp_),
        "sf": sf, "pv": pv, "missing_n": 0, "extra_n": 0, "empty_n": 0,
        "in_sync": False, "mismatch": bool(cmp_.get("error")),
        "missing": None, "extra": None, "empty": None,
    }
    if view["error"] or not cmp_:
        return view
 
    missing = cmp_.get("missing", [])
    extra   = cmp_.get("extra", [])
    empty   = (data.get("empty_desc") or {}).get(entity, [])
    emp_fields, emp_headers = (
        _EMPTY_COLUMNS_FIELDS if entity == "columns" else _EMPTY_DEFAULT_FIELDS
    )
 
    view["missing"] = _write_dataset(run_dir, f"{entity}_missing",
                                     [f"{label} Key"], ["key"],
                                     [{"key": k} for k in missing])
    view["extra"]   = _write_dataset(run_dir, f"{entity}_extra",
                                     [f"{label} Key"], ["key"],
                                     [{"key": k} for k in extra])
    view["empty"]   = _write_dataset(run_dir, f"{entity}_empty_description",
                                     emp_headers, emp_fields, empty)
 
    view.update(missing_n=len(missing), extra_n=len(extra), empty_n=len(empty),
                in_sync=not missing and not extra, mismatch=bool(missing or extra))
    return view
 
 
def _jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(enabled_extensions=("html.j2",), default=False),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["num"] = lambda n: f"{int(n or 0):,}"
    return env
 
 
def _copy_images(run_dir: str) -> None:
    dst = os.path.join(run_dir, "images")
    if os.path.isdir(STATIC_IMAGES_DIR):
        shutil.copytree(STATIC_IMAGES_DIR, dst, dirs_exist_ok=True)
    else:
        print(f"⚠️  static images folder not found: {STATIC_IMAGES_DIR}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════
 
def generate_report(data: dict, run_dir: str) -> str:
    """Write the complete report into *run_dir* and return the path to report.html.
 
    Parameters
    ----------
    data:
        Dict produced by ``utils.report_data_builder.build_report_data``.
    run_dir:
        Folder from :func:`create_run_dir` (or an existing run folder to re-render).
    """
    _ensure_layout(run_dir)
    now = datetime.datetime.now()
 
    # ── persist test results as JSONL (replaces reports/test_results.json) ───
    write_test_results(run_dir, data)
 
    # ── data files: JSONL + shards, one set per entity ───────────────────────
    entities = [_entity_view(e, data, run_dir) for e in ENTITY_LABELS]
 
    manifest: Dict[str, Any] = {"page_size": PAGE_SIZE, "shard_size": SHARD_SIZE, "datasets": {}}
    for ev in entities:
        for kind in ("missing", "extra", "empty"):
            ds = ev[kind]
            if ds and ds["count"]:
                manifest["datasets"][ds["name"]] = ds
 
    summary_rows = []
    for ev in entities:
        if ev["error"]:
            status = "⚠️ Error"
        elif ev["missing_n"] == 0 and ev["extra_n"] == 0:
            status = "✅ OK"
        else:
            status = "❌ Mismatch"
        summary_rows.append({
            "icon": ev["icon"], "label": ev["label"], "sf": ev["sf"], "pv": ev["pv"],
            "missing": ev["missing_n"], "extra": ev["extra_n"], "status": status,
        })
    _write_jsonl(os.path.join(run_dir, "jsonl", "summary.jsonl"), [
        {"entity": ev["key"], "snowflake_count": r["sf"], "purview_count": r["pv"],
         "missing_from_purview": r["missing"], "extra_in_purview": r["extra"],
         "empty_description": ev["empty_n"], "status": r["status"]}
        for ev, r in zip(entities, summary_rows)
    ])
 
    # ── overview inputs ──────────────────────────────────────────────────────
    results = data.get("test_results") or []
    outcome_icon = {"passed": "✅", "failed": "❌", "skipped": "⏭️"}
    tests = [{
        "icon":     outcome_icon.get(r.get("outcome"), "❓"),
        "name":     r.get("name", ""),
        "outcome":  r.get("outcome", ""),
        "duration": float(r.get("duration") or 0),
        "message":  ((r.get("message") or "").split("\n")[0])[:150],
    } for r in results]
    stats = {
        "total":    len(results),
        "passed":   sum(1 for r in results if r.get("outcome") == "passed"),
        "failed":   sum(1 for r in results if r.get("outcome") == "failed"),
        "skipped":  sum(1 for r in results if r.get("outcome") == "skipped"),
        "duration": (data.get("end_time") or 0) - (data.get("start_time") or 0),
    }
    coll, coll_error = _collection_view(data)
 
    # ── render templates ─────────────────────────────────────────────────────
    env = _jinja_env()
    context = {
        "environment": data.get("environment", "Prod"),
        "generated_at": now.strftime("%d %b %Y, %H:%M:%S"),
        "year": now.year,
        "theme": THEME,
        "coll": coll, "coll_error": coll_error,
        "stats": stats, "summary_rows": summary_rows, "tests": tests,
        "entities": entities,
    }
    report_path = os.path.join(run_dir, "report.html")
    _write_text(report_path, env.get_template("report.html.j2").render(**context))
    _write_text(os.path.join(run_dir, "css", "report.css"),
                env.get_template("report.css.j2").render(t=THEME))
    _write_text(os.path.join(run_dir, "js", "report.js"),
                env.get_template("report.js.j2").render(
                    page_size=PAGE_SIZE, shard_size=SHARD_SIZE, search_cap=SEARCH_CAP))
    _write_text(os.path.join(run_dir, "js", "manifest.js"),
                "window.REPORT_MANIFEST = " + json.dumps(manifest, separators=(",", ":")) + ";\n")
 
    _copy_images(run_dir)
    return report_path
 
 
def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)