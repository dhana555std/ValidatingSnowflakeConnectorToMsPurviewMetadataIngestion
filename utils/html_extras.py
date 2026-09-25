"""HTML generation helpers for pytest-html report extras."""
from typing import Any, Dict, List, Optional


_STYLE = """
<style>
  .meta-table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }
  .meta-table th {
    background: #2c3e50; color: #fff; padding: 6px 10px;
    text-align: left; white-space: nowrap;
  }
  .meta-table td { padding: 5px 10px; border-bottom: 1px solid #ddd; word-break: break-all; }
  .meta-table tr:nth-child(even) { background: #f5f7fa; }
  .meta-table tr:hover { background: #eaf2ff; }
  .section-heading {
    font-size: 15px; font-weight: 700; color: #2c3e50;
    border-left: 4px solid #3498db; padding: 4px 10px;
    margin: 14px 0 6px 0; background: #eaf4fb; border-radius: 2px;
  }
  .badge-ok   { background:#27ae60; color:#fff; border-radius:3px; padding:1px 6px; font-size:11px; }
  .badge-warn { background:#e67e22; color:#fff; border-radius:3px; padding:1px 6px; font-size:11px; }
  .badge-fail { background:#e74c3c; color:#fff; border-radius:3px; padding:1px 6px; font-size:11px; }
  .summary-box {
    background:#fafafa; border:1px solid #ddd; border-radius:4px;
    padding:8px 14px; margin: 8px 0; font-size:13px; line-height:1.7;
  }
</style>
"""


def make_table(
    headers: List[str],
    rows: List[List[Any]],
    title: Optional[str] = None,
    max_rows: int = 500,
) -> str:
    truncated = len(rows) > max_rows
    display_rows = rows[:max_rows]

    html = _STYLE
    if title:
        html += f'<div class="section-heading">{title}</div>\n'
    html += '<table class="meta-table"><thead><tr>'
    for h in headers:
        html += f"<th>{h}</th>"
    html += "</tr></thead><tbody>"
    for row in display_rows:
        html += "<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>"
    html += "</tbody></table>"
    if truncated:
        html += (
            f'<div class="summary-box"><span class="badge-warn">Truncated</span> '
            f"Showing first {max_rows:,} of {len(rows):,} rows.</div>"
        )
    return html


def make_summary_box(lines: List[str]) -> str:
    body = "".join(f"<div>{_esc(l)}</div>" for l in lines)
    return f'{_STYLE}<div class="summary-box">{body}</div>'


def make_diff_report(
    entity_type: str,
    missing: List[str],
    extra: List[str],
    sf_total: int,
    pv_total: int,
) -> str:
    badge_m = "badge-ok" if not missing else "badge-fail"
    badge_e = "badge-ok" if not extra else "badge-warn"
    html = _STYLE
    html += (
        f'<div class="summary-box">'
        f"<strong>{entity_type.upper()}</strong> — "
        f"Snowflake: {sf_total:,} &nbsp;|&nbsp; Purview: {pv_total:,} &nbsp;|&nbsp; "
        f'<span class="{badge_m}">Missing from Purview: {len(missing):,}</span> &nbsp;|&nbsp; '
        f'<span class="{badge_e}">Extra in Purview: {len(extra):,}</span>'
        f"</div>\n"
    )
    if missing:
        html += make_table(
            ["Key (missing from Purview)"],
            [[k] for k in missing],
            title=f"In Snowflake, NOT in Purview ({entity_type})",
            max_rows=200,
        )
    if extra:
        html += make_table(
            ["Key (extra in Purview)"],
            [[k] for k in extra],
            title=f"In Purview, NOT in Snowflake ({entity_type})",
            max_rows=200,
        )
    return html


def make_excluded_report(found: Dict[str, List[Dict]]) -> str:
    if not found:
        return make_summary_box(
            ["✅ No excluded database objects found in Purview."]
        )
    html = _STYLE
    for entity_type, records in found.items():
        rows = [
            [r.get("name") or r.get("COLUMN_NAME", ""), r.get("qualifiedName", r.get("qualifiedName", ""))]
            for r in records
        ]
        html += make_table(
            ["Name", "Qualified Name"],
            rows,
            title=f"Excluded objects found in Purview — {entity_type} ({len(records):,})",
        )
    return html


def make_empty_description_report(records: List[Dict], source: str) -> str:
    if not records:
        return make_summary_box([f"✅ {source}: No columns with empty description found."])

    rows = [
        [
            r.get("database_name") or r.get("TABLE_CATALOG", ""),
            r.get("schema_name")   or r.get("TABLE_SCHEMA",   ""),
            r.get("table_name")    or r.get("TABLE_NAME",     ""),
            r.get("name")          or r.get("COLUMN_NAME",    ""),
            r.get("dataType")      or r.get("DATA_TYPE",      ""),
        ]
        for r in records
    ]
    return make_table(
        ["Database", "Schema", "Table", "Column", "Data Type"],
        rows,
        title=f"{source}: Columns with empty description ({len(records):,})",
    )


def _esc(v: Any) -> str:
    s = str(v) if v is not None else ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
