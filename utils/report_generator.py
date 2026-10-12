"""
report_generator
================
Builds the Snowflake connector validation report (Purview ↔ Snowflake Metadata Validator).

This module only shapes data for the templates in
``report_ui/snowflake_connector_validation/``; the HTML, CSS and JS live there and
rendering is done by :mod:`utils.report_renderer`.

Sections
--------
Overview tab
    Commercial collection card for the configured environment (ENVIRONMENT in .env),
    test stats, extraction summary and test results.

Each entity type (Databases, Schemas, Tables, Views, Columns, Stored Procedures) gets
its own tab showing extraction counts, objects missing from / extra in Purview and
objects with empty descriptions — each list collapsed by default.
"""

import datetime
from typing import Any, Dict, List, Optional

from utils.paths import SNOWFLAKE_CONNECTOR_VALIDATION
from utils.report_renderer import render_report, table_spec

_ENTITY_LABELS = {
    "databases":         "Databases",
    "schemas":           "Schemas",
    "tables":            "Tables",
    "views":             "Views",
    "columns":           "Columns",
    "stored_procedures": "Stored Procedures",
}

_ICONS = {
    "databases":         "🗄️",
    "schemas":           "📂",
    "tables":            "📋",
    "views":             "👁️",
    "columns":           "🔠",
    "stored_procedures": "⚙️",
}

_ENV_CLASSES = {"Prod": "env-prod", "NonProd": "env-nonprod"}   # anything else → neutral grey

# Purview stores UTC; the Purview UI shows local (India) time, so the report does too.
# A fixed +05:30 offset is used (India has no daylight saving) so no tz database is needed.
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")


# ── formatting helpers ────────────────────────────────────────────────────────

def _fmt_ts(value: Any) -> str:
    """ISO UTC timestamp → 'May 13, 2026, 6:38 PM IST' (same style as the Purview UI).

    Anything unparseable is shown as-is.
    """
    if not value:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:                       # no zone given → Purview means UTC
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        dt = dt.astimezone(_IST)
        hour = dt.hour % 12 or 12
        return f"{dt:%B} {dt.day}, {dt.year}, {hour}:{dt:%M} {dt:%p} IST"
    except ValueError:
        return str(value)


def _fmt_principal(principal_id: Any, principal_type: Any) -> str:
    """Entra object ID with its type, e.g. '3f2a…-…-…  (User)'."""
    if not principal_id:
        return "—"
    return f"{principal_id} ({principal_type})" if principal_type else str(principal_id)


# ── collection card ───────────────────────────────────────────────────────────

def _collection_for_env(data: dict) -> Optional[Dict[str, Any]]:
    """The collection validated in this run (PURVIEW_COLLECTION in .env).

    Prefers the entry flagged validated_in_this_run; otherwise matches the
    report's environment; otherwise the only entry present.
    """
    cols = (data.get("collection_metadata") or {}).get("collections") or []
    flagged = [c for c in cols if c.get("validated_in_this_run")]
    if flagged:
        return flagged[0]
    env = str(data.get("environment", "")).lower()
    match = next((c for c in cols if str(c.get("environment", "")).lower() == env), None)
    if match:
        return match
    return cols[0] if len(cols) == 1 else None


def _collection_context(data: dict) -> Dict[str, Any]:
    env = data.get("environment", "")
    col = _collection_for_env(data)
    meta = data.get("collection_metadata") or {}

    if col is None:
        reason = meta.get("error") or f"No collection metadata found for environment '{env}'."
        return {"missing_reason": reason}

    col_env = col.get("environment")
    path = col.get("collection_path") or col.get("collection_name") or ""
    ctx: Dict[str, Any] = {
        "missing_reason": "",
        "env_cls": _ENV_CLASSES.get(col_env, ""),
        "env_label": str(col_env or env),
        "path": path,
        "error": col.get("error") or "",
        "rows": [],
    }
    if not ctx["error"]:
        path_nodes = [p for p in str(col.get("collection_path") or "").split("/") if p]
        rows = [
            {"label": "Environment",     "value": col.get("environment")},
            {"label": "Collection Name", "value": col.get("collection_name")},
            {"label": "Collection ID",   "value": col.get("collection_id")},
            {"label": "Collection Path", "value": "—", "path_nodes": path_nodes},
            {"label": "Created By",      "value": _fmt_principal(col.get("created_by"), col.get("created_by_type"))},
            {"label": "Created At",      "value": _fmt_ts(col.get("created_at"))},
            {"label": "Updated By",      "value": _fmt_principal(col.get("updated_by"), col.get("updated_by_type"))},
            {"label": "Updated At",      "value": _fmt_ts(col.get("updated_at"))},
        ]
        ctx["rows"] = [{"path_nodes": [], **r} for r in rows]
    return ctx


# ── overview ──────────────────────────────────────────────────────────────────

def _overview_context(data: dict) -> Dict[str, Any]:
    results = data.get("test_results", [])
    count = lambda outcome: sum(1 for r in results if r["outcome"] == outcome)
    stats = [
        {"label": "Total Tests", "value": len(results),     "cls": ""},
        {"label": "Passed",      "value": count("passed"),  "cls": "ok"},
        {"label": "Failed",      "value": count("failed"),  "cls": "bad"},
        {"label": "Skipped",     "value": count("skipped"), "cls": "mute"},
    ]

    extraction_rows = []
    for entity, label in _ENTITY_LABELS.items():
        sf  = data.get("extraction", {}).get(f"sf_{entity}", {})
        pv  = data.get("extraction", {}).get(f"pv_{entity}", {})
        cmp = data.get("comparison", {}).get(entity, {})
        missing = len(cmp.get("missing", []))
        extra   = len(cmp.get("extra",   []))
        status  = "✅ OK" if (missing == 0 and extra == 0 and "error" not in cmp) else (
            "⚠️ Error" if "error" in cmp else "❌ Mismatch"
        )
        extraction_rows.append([
            f"{_ICONS.get(entity, '')} {label}",
            f"{sf.get('count', 0):,}",
            f"{pv.get('count', 0):,}",
            f"{missing:,}",
            f"{extra:,}",
            status,
        ])

    test_rows = []
    for r in results:
        icon = {"passed": "✅", "failed": "❌", "skipped": "⏭️"}.get(r["outcome"], "❓")
        msg  = (r.get("message", "") or "").split("\n")[0][:150]
        test_rows.append([f"{icon} {r['name']}", r["outcome"].upper(), f"{r['duration']:.3f}s", msg])

    return {
        "stats": stats,
        "duration": f"{(data.get('end_time') or 0) - (data.get('start_time') or 0):.1f}",
        "extraction_table": table_spec(
            ["Entity", "Snowflake Count", "Purview Count", "Missing from Purview", "Extra in Purview", "Status"],
            extraction_rows, show_count=True),
        "test_table": table_spec(["Test Name", "Outcome", "Duration", "Details"], test_rows, show_count=True),
    }


# ── entity tabs ───────────────────────────────────────────────────────────────

def _empty_description_table(entity: str, empty: List[Dict[str, Any]]) -> Dict[str, Any]:
    if entity == "columns":
        return table_spec(
            ["Database", "Schema", "Table", "Column", "Data Type"],
            [[r.get("database_name", ""), r.get("schema_name", ""), r.get("table_name", ""),
              r.get("name", ""), r.get("dataType", "")] for r in empty],
            show_count=True)
    return table_spec(["Name", "Qualified Name"],
                      [[r.get("name", ""), r.get("qualifiedName", "")] for r in empty],
                      show_count=True)


def _entity_context(entity: str, data: dict) -> Dict[str, Any]:
    label = _ENTITY_LABELS[entity]
    cmp   = data.get("comparison", {}).get(entity, {})
    empty = data.get("empty_desc", {}).get(entity, [])
    missing = cmp.get("missing", [])
    extra   = cmp.get("extra",   [])

    ctx: Dict[str, Any] = {
        "key": entity,
        "tid": f"tab-{entity.replace('_', '-')}",
        "label": label,
        "icon": _ICONS.get(entity, ""),
        "flag": bool(missing or extra or "error" in cmp),
        "state": "error" if "error" in cmp else "ok" if cmp else "no_data",
        "error": cmp.get("error", ""),
        "sf_count": cmp.get("sf_count", 0),
        "pv_count": cmp.get("pv_count", 0),
        "in_sync": not missing and not extra,
        "missing": missing,
        "extra": extra,
        "empty": empty,
        "missing_table": table_spec([f"{label} Key"], [[k] for k in missing], show_count=True),
        "extra_table": table_spec([f"{label} Key"], [[k] for k in extra], show_count=True),
        "empty_table": _empty_description_table(entity, empty),
    }
    return ctx


# ── public entry point ────────────────────────────────────────────────────────

def build_context(data: dict) -> Dict[str, Any]:
    """Template variables for report_ui/snowflake_connector_validation/templates/index.html."""
    entities = [_entity_context(e, data) for e in _ENTITY_LABELS]
    tabs = [{"id": "tab-overview", "label": "📊 Overview"}] + [
        {"id": e["tid"], "label": f"{e['icon']} {e['label']}", "flag": e["flag"], "flag_title": "Mismatch"}
        for e in entities
    ]
    return {
        "title": "Purview ↔ Snowflake Validator",
        "subtitle": "Metadata drift detection & audit report",
        "env": data.get("environment", "Prod"),
        "tabs": tabs,
        "collection": _collection_context(data),
        "entities": entities,
        **_overview_context(data),
    }


def generate_report(data: dict) -> str:
    """Render reports/snowflake_connector_validation/index.html and return its path.

    Parameters
    ----------
    data:
        Report data from ``utils.report_data_builder.build_report_data``; also saved as
        ``data/report.json`` next to the report.
    """
    return render_report(SNOWFLAKE_CONNECTOR_VALIDATION, build_context(data), data=data)
