"""Bidirectional column comparison: Snowflake ↔ MS Purview.
 
Data is pre-extracted by the session fixture in conftest.py.
Columns are stored per-schema under data/snowflake_connector_validation/snowflake_results/columns/ and data/snowflake_connector_validation/purview_results/columns/.
"""
 
import os
 
import pytest
from utils.comparator import compare_columns, load_jsonl_dir
 
from utils.paths import SNOWFLAKE_RESULTS_DIR, PURVIEW_RESULTS_DIR

_ACTUAL   = SNOWFLAKE_RESULTS_DIR
_EXPECTED = PURVIEW_RESULTS_DIR
_MAX_LISTED = 100   # how many offending columns to print in the failure message
 
 
@pytest.mark.columns
def test_all_snowflake_columns_exist_in_purview():
    """Every Snowflake column must have a corresponding asset in Purview."""
    missing, _ = compare_columns(_ACTUAL, _EXPECTED)
    assert not missing, (
        f"{len(missing)} Snowflake column(s) NOT found in Purview:\n"
        + "\n".join(f"  • {k}" for k in missing[:100])
        + (f"\n  … and {len(missing) - 100} more" if len(missing) > 100 else "")
    )
 
 
@pytest.mark.columns
def test_no_extra_columns_in_purview():
    """Purview must not contain columns absent from Snowflake."""
    _, extra = compare_columns(_ACTUAL, _EXPECTED)
    assert not extra, (
        f"{len(extra)} column(s) in Purview NOT found in Snowflake:\n"
        + "\n".join(f"  • {k}" for k in extra[:100])
        + (f"\n  … and {len(extra) - 100} more" if len(extra) > 100 else "")
    )
 
 
@pytest.mark.columns
def test_purview_columns_have_descriptions():
    """Every Purview column must have a non-empty description.
 
    Passes only when ALL columns have a description.  A single column with a
    missing, empty, or whitespace-only description fails the test.
    """
    pv_dir = os.path.join(_EXPECTED, "columns")
 
    # No data means nothing was verified — that is a failure, not a pass.
    assert os.path.isdir(pv_dir), (
        f"Purview column data not found at '{pv_dir}' — extraction did not run, "
        "so column descriptions could not be verified."
    )
    records = load_jsonl_dir(pv_dir)
    total = len(records)
    assert total > 0, (
        "No Purview columns were extracted — column descriptions could not be verified."
    )
 
    # Same rule as the HTML report: None, "" and whitespace-only all count as empty.
    empty = [r for r in records if not (r.get("description") or "").strip()]
    pct = len(empty) / total * 100
    print(f"\nPurview columns with empty description: {len(empty):,} / {total:,} ({pct:.1f}%)")
 
    def _key(r):
        return ".".join(
            str(r.get(k) or "?")
            for k in ("database_name", "schema_name", "table_name", "name")
        )
 
    assert not empty, (
        f"{len(empty):,} of {total:,} Purview column(s) ({pct:.1f}%) have no description:\n"
        + "\n".join(f"  • {_key(r)}" for r in empty[:_MAX_LISTED])
        + (f"\n  … and {len(empty) - _MAX_LISTED:,} more" if len(empty) > _MAX_LISTED else "")
    )
 