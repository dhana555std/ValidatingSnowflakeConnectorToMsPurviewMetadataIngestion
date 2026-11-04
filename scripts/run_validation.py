"""
run_validation.py
=================
Runs the validation for every region in config/regions.json (US NonProd, EU NonProd, ...),
one pytest run after the other, each producing its own report folder:
 
    reports/SnowflakeMsPurview-US-<timestamp>/
    reports/SnowflakeMsPurview-EU-<timestamp>/
 
Run from the project root:
 
    python scripts/run_validation.py                 # all regions
    python scripts/run_validation.py --region eu     # one region (repeat the flag for more)
    python scripts/run_validation.py -- -k tables    # anything after -- goes to pytest
 
Progress from each run is shown live. Exit code: 0 = all regions passed,
1 = some tests failed, 2 = a region could not run or produced no report
(connection/config error, or the virtual environment is not active).
"""
 
import argparse
import os
import subprocess
import sys
import time
 
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
 
from utils.region_config import load_regions
from utils.report_generator import find_latest_run_dir
 
 
def main() -> None:
    parser = argparse.ArgumentParser(description="Run the validator for each configured region.")
    parser.add_argument("--region", action="append",
                        help="Region key to run (repeatable). Default: every region in config/regions.json.")
    args, extra = parser.parse_known_args()
    extra = [a for a in extra if a != "--"]
 
    try:
        regions = load_regions()
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(2)
    if not regions:
        print("ERROR: config/regions.json not found — nothing to run per region.")
        print("Run plain `pytest` instead, or create config/regions.json.")
        sys.exit(2)
 
    keys = [r.strip().lower() for r in args.region] if args.region else list(regions)
    unknown = [k for k in keys if k not in regions]
    if unknown:
        print(f"ERROR: unknown region(s): {', '.join(unknown)}. Available: {', '.join(sorted(regions))}")
        sys.exit(2)
 
    results = []
    for key in keys:
        region = regions[key]
        print(f"\n{'#' * 70}\n#  REGION {key.upper()} — {region['label']} "
              f"({len(region['databases'])} database(s))\n{'#' * 70}", flush=True)
        reports_dir = os.path.join(_ROOT, "reports")
        before = find_latest_run_dir(reports_dir, tag=key.upper())
        t = time.time()
        rc = subprocess.call(
            [sys.executable, "-m", "pytest", "--region", key, *extra], cwd=_ROOT
        )
        after = find_latest_run_dir(reports_dir, tag=key.upper())
        # exit code 1 also means "pytest could not start", so only trust it when the
        # run actually wrote a NEW report folder
        report = after if after != before else None
        if rc in (0, 1) and report is None:
            rc = 3
        results.append((key, region["label"], rc, time.time() - t, report))
 
    print(f"\n{'=' * 70}\n  SUMMARY\n{'=' * 70}")
    worst = 0
    for key, label, rc, secs, report in results:
        state = {
            0: "all tests passed",
            1: "some tests failed",
            3: "no report was produced (see the output above)",
        }.get(rc, f"could not run (exit code {rc})")
        print(f"  {label:<14} {state}  [{secs / 60:.1f} min]")
        if report and rc in (0, 1):
            print(f"                 report: {os.path.relpath(report, _ROOT)}")
        worst = max(worst, 2 if rc not in (0, 1) else rc)
    sys.exit(worst)
 
 
if __name__ == "__main__":
    main()
 