"""Bidirectional table comparison: Snowflake ↔ MS Purview.

Data is pre-extracted by the session fixture in conftest.py.
"""

import pytest
from utils.comparator import compare_tables

_ACTUAL = "data/actual"
_EXPECTED = "data/expected"


@pytest.mark.tables
def test_all_snowflake_tables_exist_in_purview():
    """Every Snowflake base table must have a corresponding asset in Purview."""
    missing, _ = compare_tables(_ACTUAL, _EXPECTED)
    assert not missing, (
        f"{len(missing)} Snowflake table(s) NOT found in Purview:\n"
        + "\n".join(f"  • {k}" for k in missing[:100])
        + (f"\n  … and {len(missing) - 100} more" if len(missing) > 100 else "")
    )


@pytest.mark.tables
def test_no_extra_tables_in_purview():
    """Purview must not contain tables absent from Snowflake."""
    _, extra = compare_tables(_ACTUAL, _EXPECTED)
    assert not extra, (
        f"{len(extra)} table(s) in Purview NOT found in Snowflake:\n"
        + "\n".join(f"  • {k}" for k in extra[:100])
        + (f"\n  … and {len(extra) - 100} more" if len(extra) > 100 else "")
    )
