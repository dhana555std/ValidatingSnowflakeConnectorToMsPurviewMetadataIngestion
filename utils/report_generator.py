
"""
report_generator
================
Generates a tabbed, self-contained HTML validation report for the
Purview ↔ Snowflake Metadata Validator.
 
Sections
--------
Overview tab
    Commercial collection card for the configured environment (ENVIRONMENT
    in .env), with its metadata; the Collection Path row is drawn in the style of
    the Purview UI, followed by test stats, extraction summary and test results.
 
Each entity type (Databases, Schemas, Tables, Views, Columns,
Stored Procedures) gets its own tab showing:
 
  • Extraction counts (Snowflake vs Purview)
  • Objects missing from Purview      — collapsed by default
  • Objects extra in Purview          — collapsed by default
  • Objects with empty descriptions   — collapsed by default
 
All rows are shown inline; collapsible sections keep the page manageable.
"""
 
import datetime
import html
from typing import Any, Dict, List, Optional
 
 
# ── generic colour palette ────────────────────────────────────────────────────
_NAVY    = "#1B3A5C"
_BLUE    = "#2C6FAC"
_BLUE_LT = "#E8F0F8"
_ACCENT  = "#E07B29"
_WHITE   = "#FFFFFF"
_BG      = "#F5F7FA"
_TEXT    = "#1A2636"
_MUTED   = "#6B7A8D"
_SUCCESS = "#1B7A3E"
_ERROR   = "#B5291C"
_WARN_BG = "#FFF4E0"
_ERR_BG  = "#FDECEA"
_OK_BG   = "#EAF7ED"
 
# Environment colours: (text/border colour, light background)
_ENV_COLOURS = {
    "Prod":    (_NAVY,   "#E3ECF6"),
    "NonProd": (_ACCENT, "#FDF0E4"),
}
_ENV_DEFAULT_COLOURS = (_MUTED, "#F0F0F0")
 
 
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
 
# ── generic tool SVG mark (icons only — title rendered as HTML text beside it) ──
_LOGO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 130 52" height="44"
     aria-label="Purview Snowflake Validator">
  <!-- database cylinder -->
  <ellipse cx="26" cy="14" rx="18" ry="7" fill="none" stroke="#5BAAD8" stroke-width="2.5"/>
  <line x1="8"  y1="14" x2="8"  y2="38" stroke="#5BAAD8" stroke-width="2.5"/>
  <line x1="44" y1="14" x2="44" y2="38" stroke="#5BAAD8" stroke-width="2.5"/>
  <ellipse cx="26" cy="38" rx="18" ry="7" fill="none" stroke="#5BAAD8" stroke-width="2.5"/>
  <!-- arrow -->
  <line x1="52" y1="26" x2="72" y2="26" stroke="#E07B29" stroke-width="3" stroke-linecap="round"/>
  <polyline points="66,20 72,26 66,32" fill="none" stroke="#E07B29"
            stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
  <!-- cloud -->
  <path d="M100 34 Q92 34 92 26 Q92 18 100 18 Q102 11 110 11
           Q120 11 120 20 Q126 20 126 27 Q126 34 118 34 Z"
        fill="none" stroke="#5BAAD8" stroke-width="2.5" stroke-linejoin="round"/>
</svg>
"""
 
 
def _esc(v: Any) -> str:
    return html.escape(str(v) if v is not None else "")
 
 
def _badge(text: str, colour: str, bg: str) -> str:
    return (
        f'<span style="background:{bg};color:{colour};border-radius:4px;'
        f'padding:2px 8px;font-size:12px;font-weight:700;">{_esc(text)}</span>'
    )
 
 
def _table(headers: List[str], rows: List[List[Any]]) -> str:
    """Render a plain scrollable table — all rows, no pagination."""
    header_html = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    rows_html   = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return (
        f'<div style="overflow-x:auto;">'
        f'<table class="vt-table"><thead><tr>{header_html}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div>'
        f'<p style="color:{_MUTED};font-size:12px;margin-top:4px;">{len(rows):,} row(s)</p>'
    )
 
 
def _collapsible(title: str, content: str, colour: str = _BLUE) -> str:
    """Wrap content in an HTML <details> block (collapsed by default)."""
    return f"""
<details class="collapsible">
  <summary style="border-left:4px solid {colour};">{title}</summary>
  <div class="collapsible-body">{content}</div>
</details>"""
 
 
# ── collection helpers ────────────────────────────────────────────────────────
 
# Purview stores UTC; the Purview UI shows local (India) time, so the report does too.
# A fixed +05:30 offset is used (India has no daylight saving) so no tz database is needed.
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
 
 
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
 
 
def _env_badge(env: str) -> str:
    colour, bg = _ENV_COLOURS.get(env, _ENV_DEFAULT_COLOURS)
    return (
        f'<span class="env-badge" style="background:{colour};color:{_WHITE};">'
        f'{_esc(str(env).upper())}</span>'
    )
 
 
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
 
 
class _Html(str):
    """Marks a value as ready-made HTML so _kv_table doesn't escape it."""
 
 
def _kv_table(rows: List[List[Any]]) -> str:
    """Two-column field/value table.  Values wrapped in _Html are inserted as-is."""
    body = "".join(
        f"<tr><th>{_esc(k)}</th><td>{v if isinstance(v, _Html) else _esc(v)}</td></tr>"
        for k, v in rows
    )
    return f'<table class="kv-table"><tbody>{body}</tbody></table>'
 
 
# Icons copied in spirit from the Purview "Collection path" panel
_ROOT_ICON = """<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="#424242"
  stroke-width="1.2" stroke-linejoin="round" aria-hidden="true">
  <path d="M5.2 15.5h9.6a3.3 3.3 0 0 0 .6-6.5 4.6 4.6 0 0 0-8.9-.9A3.7 3.7 0 0 0 5.2 15.5z"/>
  <path d="M7.5 15.5v-3.2l2.5-1.6 2.5 1.6v3.2"/>
</svg>"""
 
_COLLECTION_ICON = """<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="#424242"
  stroke-width="1.2" stroke-linejoin="round" aria-hidden="true">
  <path d="M7 6.5V3.5h9.5V13h-3"/>
  <rect x="3.5" y="6.5" width="9.5" height="10" rx="0.5"/>
</svg>"""
 
 
def _collection_path_visual(path: str) -> str:
    """Vertical path like the Purview UI: root → Prod → Commercial, joined by lines.
 
    Rendered inside the "Collection Path" row of the metadata table.
    """
    parts = [p for p in str(path or "").split("/") if p]
    if not parts:
        return "—"
    nodes = []
    for i, name in enumerate(parts):
        if i:
            nodes.append('<div class="cpath-line"></div>')
        icon = _ROOT_ICON if i == 0 else _COLLECTION_ICON
        nodes.append(
            f'<div class="cpath-node"><span class="cpath-icon">{icon}</span>'
            f'<span class="cpath-name">{_esc(name)}</span></div>'
        )
    return '<div class="cpath">' + "".join(nodes) + "</div>"
 
 
def _collection_overview(data: dict) -> str:
    """Collection card shown at the top of the Overview tab (configured environment only)."""
    env = data.get("environment", "")
    col = _collection_for_env(data)
    meta = data.get("collection_metadata") or {}
 
    if col is None:
        reason = meta.get("error") or f"No collection metadata found for environment '{env}'."
        return f'<div class="err-box">⚠️ Collection: {_esc(reason)}</div>'
 
    colour, bg = _ENV_COLOURS.get(col.get("environment"), _ENV_DEFAULT_COLOURS)
    path = col.get("collection_path") or col.get("collection_name") or ""
    head = (
        f'<div class="coll-card-head" style="background:{bg};">'
        f'{_env_badge(col.get("environment", env))} '
        f'<strong style="font-size:15px;">{_esc(path)}</strong></div>'
    )
 
    if col.get("error"):
        body = f'<div class="err-box">⚠️ Could not extract metadata: {_esc(col["error"])}</div>'
    else:
        rows = [
            ["Environment",     col.get("environment")],
            ["Collection Name", col.get("collection_name")],
            ["Collection ID",   col.get("collection_id")],
            ["Collection Path", _Html(_collection_path_visual(col.get("collection_path")))],
            ["Created By",      _fmt_principal(col.get("created_by"), col.get("created_by_type"))],
            ["Created At",      _fmt_ts(col.get("created_at"))],
            ["Updated By",      _fmt_principal(col.get("updated_by"), col.get("updated_by_type"))],
            ["Updated At",      _fmt_ts(col.get("updated_at"))],
        ]
        body = f'<div class="coll-subhead">Collection Metadata</div>{_kv_table(rows)}'
 
    return (
        f'<div class="coll-card" style="border-top:5px solid {colour};">{head}'
        f'<div class="coll-card-body">{body}</div></div>'
    )
 
 
# ── CSS & JS ──────────────────────────────────────────────────────────────────
 
_CSS = f"""
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: Arial, Helvetica, sans-serif; background: {_BG};
        color: {_TEXT}; font-size: 14px; line-height: 1.5; }}
header {{
  background: linear-gradient(135deg, {_NAVY} 0%, #264D7A 100%);
  padding: 14px 28px; display: flex; align-items: center;
  justify-content: space-between; box-shadow: 0 3px 8px rgba(0,0,0,.3);
}}
header .meta {{ color: #A8C8E8; font-size: 12px; text-align: right; }}
footer {{
  background: {_NAVY}; color: #7A9AB8; text-align: center;
  padding: 14px; font-size: 12px; margin-top: 40px;
}}
.container {{ max-width: 1300px; margin: 0 auto; padding: 24px 20px; }}
/* ── Tabs ── */
.tab-bar {{
  display: flex; gap: 3px; background: {_NAVY}; padding: 0 10px;
  border-radius: 6px 6px 0 0; overflow-x: auto; flex-wrap: nowrap;
}}
.tab-btn {{
  background: transparent; border: none; color: #A8C8E8; cursor: pointer;
  padding: 11px 16px; font-size: 13px; font-weight: 600; white-space: nowrap;
  border-bottom: 3px solid transparent; transition: .15s;
}}
.tab-btn:hover {{ color: {_WHITE}; border-bottom-color: {_BLUE}; }}
.tab-btn.active {{ color: {_WHITE}; border-bottom-color: {_ACCENT};
                   background: rgba(255,255,255,.07); }}
.tab-content {{
  background: {_WHITE}; border-radius: 0 0 8px 8px; padding: 24px;
  box-shadow: 0 2px 8px rgba(0,0,0,.08);
}}
.tab-pane {{ display: none; animation: fadeIn .2s ease; }}
.tab-pane.active {{ display: block; }}
@keyframes fadeIn {{ from {{ opacity:0; transform:translateY(3px); }} to {{ opacity:1; transform:none; }} }}
/* ── Tables ── */
.vt-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.vt-table th {{
  background: {_NAVY}; color: {_WHITE}; padding: 8px 12px;
  text-align: left; white-space: nowrap; position: sticky; top: 0; z-index: 1;
}}
.vt-table td {{ padding: 6px 12px; border-bottom: 1px solid #DDE5F0;
                word-break: break-all; }}
.vt-table tr:nth-child(even) {{ background: {_BLUE_LT}; }}
.vt-table tr:hover {{ background: #D0E4F4; }}
/* ── Stat cards ── */
.stat-row {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 18px; }}
.stat-card {{
  flex: 1; min-width: 120px; border-radius: 8px; padding: 16px 20px;
  box-shadow: 0 1px 4px rgba(0,0,0,.1);
}}
.stat-val {{ font-size: 30px; font-weight: 800; }}
.stat-lbl {{ font-size: 12px; color: {_MUTED}; margin-top: 4px; }}
/* ── Collapsible ── */
.collapsible {{ margin: 14px 0; border: 1px solid #D0DAE8; border-radius: 6px; }}
.collapsible summary {{
  cursor: pointer; padding: 10px 14px; font-size: 13px; font-weight: 700;
  color: {_NAVY}; background: {_BLUE_LT}; border-radius: 6px;
  list-style: none; display: flex; align-items: center; gap: 8px;
  user-select: none;
}}
.collapsible summary::-webkit-details-marker {{ display: none; }}
.collapsible summary::before {{
  content: "▶"; font-size: 10px; transition: transform .2s; color: {_BLUE};
}}
.collapsible[open] > summary::before {{ transform: rotate(90deg); }}
.collapsible-body {{ padding: 14px; }}
/* ── Misc ── */
.entity-banner {{
  background: #EEF4FC; border-radius: 6px; padding: 12px 16px;
  margin-bottom: 14px; font-size: 13px; line-height: 1.9;
  border: 1px solid #C5D8EE;
}}
.ok-box {{
  background: {_OK_BG}; border: 1px solid #B0DCBD; border-radius: 6px;
  padding: 12px 16px; color: {_SUCCESS}; font-weight: 600; margin: 6px 0;
}}
.err-box {{
  background: {_ERR_BG}; border: 1px solid #E8A89A; border-radius: 6px;
  padding: 10px 14px; color: {_ERROR}; font-weight: 600; margin: 6px 0;
}}
.warn-box {{
  background: {_WARN_BG}; border: 1px solid #F0C98A; border-radius: 6px;
  padding: 10px 14px; color: #8A5300; font-weight: 600; margin: 6px 0 10px;
}}
.section-h3 {{
  font-size: 14px; font-weight: 700; color: {_NAVY};
  border-left: 4px solid {_BLUE}; padding: 4px 10px; margin: 18px 0 8px;
  background: {_BLUE_LT}; border-radius: 0 4px 4px 0;
}}
/* ── Collection card (Overview) ── */
.coll-card {{ border: 1px solid #D0DAE8; border-radius: 8px; overflow: hidden;
              box-shadow: 0 1px 4px rgba(0,0,0,.08); background: {_WHITE}; margin-bottom: 18px; }}
.coll-card-head {{ padding: 12px 16px; display: flex; align-items: center;
                   gap: 10px; flex-wrap: wrap; border-bottom: 1px solid #D0DAE8; }}
.coll-card-body {{ padding: 12px 16px 16px; }}
.coll-subhead {{ font-size: 12px; font-weight: 700; color: {_MUTED};
                 text-transform: uppercase; letter-spacing: .5px; margin: 4px 0 6px; }}
.env-badge {{ border-radius: 4px; padding: 3px 10px; font-size: 12px;
              font-weight: 800; letter-spacing: .6px; }}
.kv-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.kv-table th {{ text-align: left; width: 38%; color: {_MUTED}; font-weight: 600;
                padding: 6px 8px; border-bottom: 1px solid #E6ECF3; vertical-align: top; }}
.kv-table td {{ padding: 6px 8px; border-bottom: 1px solid #E6ECF3; word-break: break-all; }}
/* ── Collection path (Purview style) ── */
.cpath {{ font-family: "Segoe UI", Arial, Helvetica, sans-serif; padding: 2px 0; }}
.cpath-node {{ display: flex; align-items: center; gap: 12px; }}
.cpath-icon {{ width: 20px; height: 20px; display: inline-flex; flex: 0 0 20px; }}
.cpath-name {{ font-size: 14px; font-weight: 600; color: #0F3D78; word-break: normal; }}
.cpath-line {{ width: 1px; height: 14px; background: #A0A0A0; margin: 3px 0 3px 10px; }}
 
"""
 
_TAB_JS = """
function showTab(id) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  document.querySelector('[data-tab="' + id + '"]').classList.add('active');
}
"""
 
 
# ── section builders ──────────────────────────────────────────────────────────
 
def _overview_section(data: dict) -> str:
    results = data.get("test_results", [])
    passed  = sum(1 for r in results if r["outcome"] == "passed")
    failed  = sum(1 for r in results if r["outcome"] == "failed")
    skipped = sum(1 for r in results if r["outcome"] == "skipped")
    total   = len(results)
    dur = (data.get("end_time") or 0) - (data.get("start_time") or 0)
 
    cards = []
    for label, val, colour, bg in [
        ("Total Tests", total,   _TEXT,    _BG),
        ("Passed",      passed,  _SUCCESS, _OK_BG),
        ("Failed",      failed,  _ERROR,   _ERR_BG),
        ("Skipped",     skipped, _MUTED,   "#F0F0F0"),
    ]:
        cards.append(
            f'<div class="stat-card" style="border-top:4px solid {colour};background:{bg};">'
            f'<div class="stat-val" style="color:{colour};">{val}</div>'
            f'<div class="stat-lbl">{label}</div></div>'
        )
 
    out = _collection_overview(data)
    out += f"""
<div class="stat-row">{"".join(cards)}</div>
<p style="color:{_MUTED};font-size:13px;margin-bottom:16px;">
  Environment: <strong>{_esc(data.get('environment',''))}</strong>
  &nbsp;|&nbsp; Duration: <strong>{dur:.1f} s</strong>
</p>
<h3 class="section-h3">Extraction Summary</h3>
"""
 
    rows = []
    for entity, label in _ENTITY_LABELS.items():
        sf  = data.get("extraction", {}).get(f"sf_{entity}", {})
        pv  = data.get("extraction", {}).get(f"pv_{entity}", {})
        cmp = data.get("comparison", {}).get(entity, {})
        missing = len(cmp.get("missing", []))
        extra   = len(cmp.get("extra",   []))
        status  = "✅ OK" if (missing == 0 and extra == 0 and "error" not in cmp) else (
            "⚠️ Error" if "error" in cmp else "❌ Mismatch"
        )
        rows.append([
            f"{_ICONS.get(entity,'')} {label}",
            f"{sf.get('count',0):,}",
            f"{pv.get('count',0):,}",
            f"{missing:,}",
            f"{extra:,}",
            status,
        ])
 
    out += _table(
        ["Entity", "Snowflake Count", "Purview Count", "Missing from Purview", "Extra in Purview", "Status"],
        rows,
    )
 
    # Test results
    out += '<h3 class="section-h3">Test Results</h3>'
    test_rows = []
    for r in results:
        icon = {"passed": "✅", "failed": "❌", "skipped": "⏭️"}.get(r["outcome"], "❓")
        msg  = (r.get("message", "") or "").split("\n")[0][:150]
        test_rows.append([
            f"{icon} {r['name']}",
            r["outcome"].upper(),
            f"{r['duration']:.3f}s",
            msg,
        ])
    out += _table(["Test Name", "Outcome", "Duration", "Details"], test_rows)
 
    return out
 
 
def _entity_section(entity: str, data: dict) -> str:
    label = _ENTITY_LABELS.get(entity, entity)
    cmp   = data.get("comparison", {}).get(entity, {})
    empty = data.get("empty_desc", {}).get(entity, [])
 
    # Extraction error
    if "error" in cmp:
        return (
            f'<div class="err-box">⚠️ Comparison error: {_esc(cmp["error"])}</div>'
        )
 
    # No data at all
    if not cmp:
        return (
            '<div class="entity-banner">'
            '<strong>No extraction data available.</strong> '
            'Check console — a connection or extraction error may have caused early exit.'
            '</div>'
        )
 
    sf_cnt  = cmp.get("sf_count", 0)
    pv_cnt  = cmp.get("pv_count", 0)
    missing = cmp.get("missing", [])
    extra   = cmp.get("extra",   [])
    in_sync = not missing and not extra
    status_colour = _SUCCESS if in_sync else _ERROR
    status_text   = "✅ PASS — Sets are equal" if in_sync else "❌ FAIL — Mismatch detected"
 
    out = f"""
<div class="entity-banner" style="border-left:5px solid {status_colour};">
  <strong style="color:{status_colour};">{status_text}</strong><br/>
  Snowflake: <strong>{sf_cnt:,}</strong>
  &nbsp;|&nbsp; Purview: <strong>{pv_cnt:,}</strong>
  &nbsp;|&nbsp; Missing from Purview: <strong style="color:{_ERROR if missing else _SUCCESS};">{len(missing):,}</strong>
  &nbsp;|&nbsp; Extra in Purview: <strong style="color:{_ERROR if extra else _SUCCESS};">{len(extra):,}</strong>
  &nbsp;|&nbsp; Empty description: <strong style="color:{_ACCENT if empty else _SUCCESS};">{len(empty):,}</strong>
</div>
"""
 
    # ── Missing from Purview (collapsed) ────────────────────────────────────
    if missing:
        body = _table([f"{label} Key"], [[k] for k in missing])
        out += _collapsible(
            f"⬇️ In Snowflake — NOT in Purview &nbsp;({len(missing):,})",
            body,
            _ERROR,
        )
    else:
        out += f'<div class="ok-box">✅ All {label.lower()} are present in Purview.</div>'
 
    # ── Extra in Purview (collapsed) ─────────────────────────────────────────
    if extra:
        body = _table([f"{label} Key"], [[k] for k in extra])
        out += _collapsible(
            f"⬆️ In Purview — NOT in Snowflake &nbsp;({len(extra):,})",
            body,
            _ACCENT,
        )
    else:
        out += f'<div class="ok-box">✅ No extra {label.lower()} in Purview.</div>'
 
    # ── Empty description (collapsed) ────────────────────────────────────────
    if empty:
        if entity == "columns":
            col_rows = [[
                r.get("database_name", ""),
                r.get("schema_name",   ""),
                r.get("table_name",    ""),
                r.get("name",          ""),
                r.get("dataType",      ""),
            ] for r in empty]
            body = _table(["Database", "Schema", "Table", "Column", "Data Type"], col_rows)
        else:
            body = _table(
                ["Name", "Qualified Name"],
                [[r.get("name", ""), r.get("qualifiedName", "")] for r in empty],
            )
        out += _collapsible(
            f"📝 Empty Description in Purview &nbsp;({len(empty):,})",
            body,
            _BLUE,
        )
    else:
        out += f'<div class="ok-box">✅ All {label.lower()} have descriptions in Purview.</div>'
 
    return out
 
 
# ── full page ─────────────────────────────────────────────────────────────────
 
def generate_report(data: dict, out_path: str) -> None:
    """Write the HTML validation report to out_path.
 
    Parameters
    ----------
    data:
        The ``_REPORT_DATA`` dict populated by conftest.py.
    out_path:
        Destination file path (e.g. ``reports/report.html``).
    """
    env  = data.get("environment", "Prod")
    ts   = datetime.datetime.now().strftime("%d %b %Y, %H:%M:%S")
    year = datetime.datetime.now().year
 
    tabs_html  = []
    panes_html = []
 
    # Overview tab
    tabs_html.append(
        '<button class="tab-btn active" data-tab="tab-overview" '
        'onclick="showTab(\'tab-overview\')">📊 Overview</button>'
    )
    panes_html.append(
        f'<div class="tab-pane active" id="tab-overview">{_overview_section(data)}</div>'
    )
 
    # Per-entity tabs
    for entity in _ENTITY_LABELS:
        tid   = f"tab-{entity.replace('_','-')}"
        label = _ENTITY_LABELS[entity]
        icon  = _ICONS.get(entity, "")
        cmp   = data.get("comparison", {}).get(entity, {})
        dot   = (
            f' <span style="color:{_ERROR};font-size:10px;" title="Mismatch">●</span>'
            if (cmp.get("missing") or cmp.get("extra") or "error" in cmp)
            else ""
        )
        tabs_html.append(
            f'<button class="tab-btn" data-tab="{tid}" '
            f'onclick="showTab(\'{tid}\')">{icon} {label}{dot}</button>'
        )
        panes_html.append(
            f'<div class="tab-pane" id="{tid}">'
            f'<h2 style="font-size:18px;margin-bottom:14px;">{icon} {label}</h2>'
            f'{_entity_section(entity, data)}'
            f'</div>'
        )
 
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Purview ↔ Snowflake Validator — {_esc(env)}</title>
  <style>{_CSS}</style>
</head>
<body>
<header>
  <div style="display:flex;align-items:center;gap:14px;">
    {_LOGO_SVG}
    <div>
      <div style="color:#FFFFFF;font-size:18px;font-weight:700;letter-spacing:0.3px;">
        Purview &#8596; Snowflake Validator
      </div>
      <div style="color:#A8C8E8;font-size:12px;margin-top:2px;">
        Metadata drift detection &amp; audit report
      </div>
    </div>
  </div>
  <div class="meta">
    Environment: <strong style="color:#FFFFFF;">{_esc(env)}</strong><br/>
    Generated: {_esc(ts)}
  </div>
</header>
 
<div class="container">
  <div class="tab-bar">{"".join(tabs_html)}</div>
  <div class="tab-content">{"".join(panes_html)}</div>
</div>
 
<footer>
  &copy; {year} Purview–Snowflake Validator &nbsp;|&nbsp;
  Open-source metadata validation tool &nbsp;|&nbsp;
  Report generated {_esc(ts)}
</footer>
 
<script>{_TAB_JS}</script>
</body>
</html>"""
 
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)