"""Bidirectional schema comparison: Snowflake ↔ MS Purview.

Data is pre-extracted by the session fixture in conftest.py.
"""

import pytest
from utils.comparator import compare_schemas

_ACTUAL = "data/snowflake_results"
_EXPECTED = "data/purview_snowflake_results"


@pytest.mark.schemas
def test_all_snowflake_schemas_exist_in_purview():
    """Every Snowflake schema must have a corresponding asset in Purview."""
    missing, _ = compare_schemas(_ACTUAL, _EXPECTED)
    assert not missing, (
        f"{len(missing)} Snowflake schema(s) NOT found in Purview:\n"
        + "\n".join(f"  • {k}" for k in missing[:50])
        + (f"\n  … and {len(missing) - 50} more" if len(missing) > 50 else "")
    )


@pytest.mark.schemas
def test_no_extra_schemas_in_purview():
    """Purview must not contain schemas absent from Snowflake."""
    _, extra = compare_schemas(_ACTUAL, _EXPECTED)
    assert not extra, (
        f"{len(extra)} schema(s) in Purview NOT found in Snowflake:\n"
        + "\n".join(f"  • {k}" for k in extra[:50])
        + (f"\n  … and {len(extra) - 50} more" if len(extra) > 50 else "")
    )
