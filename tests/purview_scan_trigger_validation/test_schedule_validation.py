"""Purview scan trigger validation: expected schedule ↔ actual Purview scan runs.
 
The comparison is done by the package fixture in conftest.py.
These tests only read data/purview_scan_trigger_validation/validation_results.jsonl.
"""
 
import json
import os
 
import pytest
 
_RESULTS = "data/purview_scan_trigger_validation/validation_results.jsonl"
 
 
def _load():
    if not os.path.exists(_RESULTS):
        return None
    with open(_RESULTS, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
 
 
def _results(category):
    rows = _load()
    if rows is None:
        pytest.fail("Validation did not run — check expected_schedule.csv (see STEP 4 in the output).")
    return [r for r in rows if r["category"] == category]
 
 
def _lines(rows):
    return "\n".join(f"  • {r['scan_name']}  [{r['expected_run_time'] or r['expected_schedule']}]  {r['reason']}"
                     for r in rows)
 
 
@pytest.mark.scan_triggers
def test_expected_schedule_rows_are_valid():
    """Every row of the expected schedule CSV must be readable."""
    bad = _results("invalid_csv")
    assert not bad, f"{len(bad)} invalid row(s) in the expected schedule CSV:\n" + _lines(bad)
 
 
@pytest.mark.scan_triggers
def test_expected_scans_exist_in_purview():
    """Every scan in the expected schedule must exist in Purview."""
    missing = _results("not_found")
    assert not missing, f"{len(missing)} expected scan(s) NOT found in Purview:\n" + _lines(missing)
 
 
@pytest.mark.scan_triggers
def test_expected_scans_have_an_enabled_schedule():
    """Every expected scan must have an enabled schedule in Purview."""
    unscheduled = _results("no_schedule")
    assert not unscheduled, f"{len(unscheduled)} expected scan(s) with NO enabled schedule:\n" + _lines(unscheduled)
 
 
@pytest.mark.scan_triggers
def test_no_missed_scheduled_runs():
    """Each expected run must have a Purview run starting within the tolerance."""
    missed = _results("missed")
    assert not missed, f"{len(missed)} expected run(s) MISSED:\n" + _lines(missed)
 
 
@pytest.mark.scan_triggers
def test_expected_runs_succeeded():
    """Runs that happened at the expected time must not have failed."""
    failed = _results("failed")
    assert not failed, f"{len(failed)} expected run(s) FAILED:\n" + _lines(failed)