"""Bidirectional column comparison: Snowflake ↔ MS Purview.

Data is pre-extracted by the session fixture in conftest.py.
Columns are stored per-schema under data/actual/columns/ and data/expected/columns/.
"""

import os

import pytest
from utils.comparator import compare_columns, load_jsonl_dir, find_purview_columns_empty_description

_ACTUAL = "data/actual"
_EXPECTED = "data/expected"


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
    """Report columns in Purview that are missing a description.

    This test always passes — it is informational and surfaces the
    count in the console. The HTML report shows the full tabular list.
    """
    pv_dir = os.path.join(_EXPECTED, "columns")
    if not os.path.isdir(pv_dir):
        pytest.skip("Purview column data not yet extracted")

    empty = find_purview_columns_empty_description(_EXPECTED)
    total = len(load_jsonl_dir(pv_dir))
    pct = (len(empty) / total * 100) if total else 0
    print(
        f"\nPurview columns with empty description: {len(empty):,} / {total:,} ({pct:.1f}%)"
    )
    # Non-blocking — surfaced in HTML report; remove the comment below to enforce
    # assert not empty, f"{len(empty):,} Purview column(s) have no description"
