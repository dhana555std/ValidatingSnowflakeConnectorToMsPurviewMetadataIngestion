"""Bidirectional view comparison: Snowflake ↔ MS Purview.

Data is pre-extracted by the session fixture in conftest.py.
"""

import pytest
from utils.comparator import compare_views

_ACTUAL = "data/actual"
_EXPECTED = "data/expected"


@pytest.mark.views
def test_all_snowflake_views_exist_in_purview():
    """Every Snowflake view must have a corresponding asset in Purview."""
    missing, _ = compare_views(_ACTUAL, _EXPECTED)
    assert not missing, (
        f"{len(missing)} Snowflake view(s) NOT found in Purview:\n"
        + "\n".join(f"  • {k}" for k in missing[:100])
        + (f"\n  … and {len(missing) - 100} more" if len(missing) > 100 else "")
    )


@pytest.mark.views
def test_no_extra_views_in_purview():
    """Purview must not contain views absent from Snowflake."""
    _, extra = compare_views(_ACTUAL, _EXPECTED)
    assert not extra, (
        f"{len(extra)} view(s) in Purview NOT found in Snowflake:\n"
        + "\n".join(f"  • {k}" for k in extra[:100])
        + (f"\n  … and {len(extra) - 100} more" if len(extra) > 100 else "")
    )
