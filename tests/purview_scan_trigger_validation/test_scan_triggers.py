"""Purview scan trigger checks on the scan report built by conftest.py.
 
Data is pre-extracted by the package fixture in conftest.py.
These tests only read data/purview_scan_trigger_validation/purview_scan_runs.jsonl.
"""
 
import json
import os
 
import pytest
 
_SCAN_RUNS = "data/purview_scan_trigger_validation/purview_scan_runs.jsonl"
 
 
def _rows():
    if not os.path.exists(_SCAN_RUNS):
        return []
    with open(_SCAN_RUNS, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
 
 
def _scans():
    """One row per scan (the first row of each scan; scan columns repeat on every row)."""
    seen = {}
    for r in _rows():
        seen.setdefault((r["data_source"], r["scan_name"]), r)
    return list(seen.values())
 
 
@pytest.mark.scan_triggers
def test_scans_found_in_purview():
    """At least one Snowflake scan must exist in the configured collection."""
    assert _scans(), "No Snowflake scans found in Purview for the configured collection."
 
 
@pytest.mark.scan_triggers
def test_all_scans_have_an_enabled_schedule():
    """Every scan must have an enabled schedule (trigger) in Purview."""
    manual = [s for s in _scans() if s["schedule"] != "Schedule"]
    assert not manual, (f"{len(manual)} scan(s) with NO enabled schedule:\n"
                        + "\n".join(f"  • {s['scan_name']}  ({s['schedule']})" for s in manual))
 
 
@pytest.mark.scan_triggers
def test_all_scheduled_scans_ran_recently():
    """Every scheduled scan must have at least one run in the history window."""
    idle = [s for s in _scans() if s["schedule"] == "Schedule" and str(s["run_status"]).startswith("No runs")]
    assert not idle, (f"{len(idle)} scheduled scan(s) with no runs in the window:\n"
                      + "\n".join(f"  • {s['scan_name']}  (last scan: {s['last_scan_time'] or 'never'})"
                                  for s in idle))
 
 
@pytest.mark.scan_triggers
def test_latest_run_did_not_fail():
    """The latest run of every scan must not be Failed."""
    failed = [s for s in _scans() if s["last_run_status"] == "Failed"]
    assert not failed, (f"{len(failed)} scan(s) whose latest run FAILED:\n"
                        + "\n".join(f"  • {s['scan_name']}  ({s['last_scan_time']})" for s in failed))
 
 
@pytest.mark.scan_triggers
def test_no_failed_scheduled_runs_in_window():
    """No scheduler-triggered run in the window may have failed."""
    failed = [r for r in _rows() if r["triggered_by"] == "Schedule" and r["run_status"] == "Failed"]
    assert not failed, (f"{len(failed)} scheduled run(s) FAILED:\n"
                        + "\n".join(f"  • {r['scan_name']}  {r['run_start']}  {r['error'][:120]}" for r in failed))
 