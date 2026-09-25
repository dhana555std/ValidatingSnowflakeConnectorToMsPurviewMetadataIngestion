"""Bidirectional stored procedure comparison: Snowflake ↔ MS Purview.

Data is pre-extracted by the session fixture in conftest.py.
"""

import pytest
from utils.comparator import compare_stored_procedures

_ACTUAL = "data/actual"
_EXPECTED = "data/expected"


@pytest.mark.stored_procedures
def test_all_snowflake_stored_procedures_exist_in_purview():
    """Every Snowflake stored procedure must have a corresponding asset in Purview."""
    missing, _ = compare_stored_procedures(_ACTUAL, _EXPECTED)
    assert not missing, (
        f"{len(missing)} Snowflake stored procedure(s) NOT found in Purview:\n"
        + "\n".join(f"  • {k}" for k in missing[:100])
        + (f"\n  … and {len(missing) - 100} more" if len(missing) > 100 else "")
    )


@pytest.mark.stored_procedures
def test_no_extra_stored_procedures_in_purview():
    """Purview must not contain stored procedures absent from Snowflake."""
    _, extra = compare_stored_procedures(_ACTUAL, _EXPECTED)
    assert not extra, (
        f"{len(extra)} stored procedure(s) in Purview NOT found in Snowflake:\n"
        + "\n".join(f"  • {k}" for k in extra[:100])
        + (f"\n  … and {len(extra) - 100} more" if len(extra) > 100 else "")
    )
