"""
report_renderer
===============
Renders a validation's HTML report from templates in ``report_ui/`` — no HTML, CSS or JS
lives in Python.

Source layout (committed)::

    report_ui/
      shared/{css,js,img,templates}/        branding used by every report
      <validation>/{css,js,img,templates}/  that validation's own pages and styling
                                            (templates/index.html is its entry point)

Generated layout (``reports/`` is git-ignored)::

    reports/
      assets/{css,js,img}/                  copy of report_ui/shared — one per run, shared by all reports
      <validation>/
        index.html
        css/ js/ img/                       copy of report_ui/<validation>/…
        data/report.json                    the data the report was built from

To add a validation, create ``report_ui/<validation>/templates/index.html`` (extending
``base.html``) plus ``css/report.css``, then call ``render_report("<validation>", context)``.
"""

import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from utils.paths import REPORTS_ROOT, REPORT_UI_DIR

_ASSET_KINDS = ("css", "js", "img")


def table_spec(headers: List[str], rows: List[List[Any]], tid: Optional[str] = None,
               wrap: Iterable[int] = (), filterable: bool = False, show_count: bool = False) -> Dict[str, Any]:
    """Data for the ``table`` macro. ``wrap`` = column indexes allowed to wrap onto several lines."""
    return {"headers": headers, "rows": rows, "id": tid, "wrap": list(wrap),
            "filterable": filterable, "show_count": show_count}


def badge(text: Any, cls: str) -> Dict[str, str]:
    """A table cell rendered as a coloured badge (cls: ok | bad | warn | mute)."""
    return {"text": "" if text is None else str(text), "badge": cls}


def _num(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def _copy_assets(src_root: str, dest_root: str) -> None:
    for kind in _ASSET_KINDS:
        src = os.path.join(src_root, kind)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest_root, kind), dirs_exist_ok=True)


def render_report(validation: str, context: Dict[str, Any], data: Optional[Any] = None,
                  reports_root: str = REPORTS_ROOT, ui_root: str = REPORT_UI_DIR) -> str:
    """Write ``<reports_root>/<validation>/index.html`` and return its path.

    context   template variables (title, subtitle, tabs, …); ``assets``, ``generated`` and
              ``year`` are added here
    data      optional JSON-serialisable data saved next to the report as data/report.json
    """
    ui_dir = os.path.join(ui_root, validation)
    shared_dir = os.path.join(ui_root, "shared")
    if not os.path.isdir(os.path.join(ui_dir, "templates")):
        raise FileNotFoundError(f"No report templates for '{validation}' in {ui_dir}/templates")

    out_dir = os.path.join(reports_root, validation)
    os.makedirs(out_dir, exist_ok=True)
    _copy_assets(shared_dir, os.path.join(reports_root, "assets"))
    _copy_assets(ui_dir, out_dir)

    env = Environment(
        loader=FileSystemLoader([os.path.join(ui_dir, "templates"), os.path.join(shared_dir, "templates")]),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["num"] = _num

    now = datetime.now()
    page = env.get_template("index.html").render(
        assets="../assets",
        generated=now.strftime("%d %b %Y, %H:%M:%S"),
        year=now.year,
        **context,
    )

    index_path = os.path.join(out_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(page)

    if data is not None:
        os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
        with open(os.path.join(out_dir, "data", "report.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    return index_path
