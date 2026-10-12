"""Bidirectional database comparison: Snowflake ↔ MS Purview.

Data is pre-extracted by the session fixture in conftest.py.
These tests load JSONL files and assert set equality between both sources.
"""

import pytest
from utils.comparator import compare_databases

from utils.paths import SNOWFLAKE_RESULTS_DIR, PURVIEW_RESULTS_DIR

_ACTUAL   = SNOWFLAKE_RESULTS_DIR
_EXPECTED = PURVIEW_RESULTS_DIR


@pytest.mark.databases
def test_all_snowflake_databases_exist_in_purview():
    """Every Snowflake database must have a corresponding asset in Purview."""
    missing, _ = compare_databases(_ACTUAL, _EXPECTED)
    assert not missing, (
        f"{len(missing)} Snowflake database(s) NOT found in Purview:\n"
        + "\n".join(f"  • {k}" for k in missing)
    )


@pytest.mark.databases
def test_no_extra_databases_in_purview():
    """Purview must not contain databases absent from Snowflake."""
    _, extra = compare_databases(_ACTUAL, _EXPECTED)
    assert not extra, (
        f"{len(extra)} database(s) in Purview NOT found in Snowflake:\n"
        + "\n".join(f"  • {k}" for k in extra)
    )
