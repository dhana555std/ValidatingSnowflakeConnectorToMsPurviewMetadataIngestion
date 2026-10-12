"""
utils
=====
Shared utility modules for the purview-snowflake-validator framework.

Modules
-------
comparator
    Set-diff engine: loads JSONL snapshots from both sources and computes
    which objects are missing from or extra in Purview.
report_renderer
    Renders a validation's HTML report from the templates, CSS and JS in
    ``report_ui/`` (shared branding + per-validation pages).
report_generator
    Shapes the Snowflake connector validation data for its report templates.
reporter
    Console-based extraction summary printer.
file_helper
    Low-level JSON and CSV I/O helpers.
"""

from .file_helper import dump_json, load_json, dump_csv, load_csv
from .reporter import log_extraction

__all__ = ["dump_json", "load_json", "dump_csv", "load_csv", "log_extraction"]
