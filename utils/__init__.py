"""
utils
=====
Shared utility modules for the purview-snowflake-validator framework.

Modules
-------
comparator
    Set-diff engine: loads JSONL snapshots from both sources and computes
    which objects are missing from or extra in Purview.
html_extras
    Styled HTML fragment builders that are embedded into pytest-html test
    results via the ``extra`` fixture.
reporter
    Console-based extraction summary printer.
file_helper
    Low-level JSON and CSV I/O helpers.
"""

from .file_helper import dump_json, load_json, dump_csv, load_csv
from .reporter import log_extraction

__all__ = ["dump_json", "load_json", "dump_csv", "load_csv", "log_extraction"]
