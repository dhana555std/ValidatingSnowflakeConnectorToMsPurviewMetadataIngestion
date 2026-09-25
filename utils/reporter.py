"""
reporter
========
Console-based extraction summary printer for the pytest session.

Prints a human-readable summary of each extraction result to stdout so it
appears in the captured test output and in the pytest-html report's "Captured
stdout" section.
"""

from typing import Any, Dict


def log_extraction(entity_type: str, summary: Dict[str, Any]) -> None:
    """Print a formatted extraction summary to stdout.

    Parameters
    ----------
    entity_type:
        Human-readable label (e.g. ``"databases"``, ``"purview_tables"``).
    summary:
        Dict returned by the client's ``extract_*()`` methods.  Expected keys:
        ``count``, ``output`` (or ``output_dir``), ``fields``, ``sample``.
    """
    print(f"\n{'=' * 60}")
    print(f"  {entity_type.upper()} EXTRACTION SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Total records : {summary.get('count', 0):,}")
    print(f"  Output file   : {summary.get('output', 'n/a')}")

    fields = summary.get("fields", [])
    if fields:
        print(f"  Fields        : {fields}")

    sample = summary.get("sample", [])
    if sample:
        print(f"  Sample record :")
        for k, v in sample[0].items():
            preview = str(v)[:120] if v is not None else "None"
            print(f"    {k}: {preview}")
    else:
        print("  No records found.")
