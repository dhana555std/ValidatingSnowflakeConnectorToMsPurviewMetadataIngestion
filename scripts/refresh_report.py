"""
refresh_report.py
=================
Re-renders a report folder from the JSONL files already on disk
(data/actual/ and data/expected/) WITHOUT re-running extraction or tests.
 
Run from the project root:
 
    python scripts/refresh_report.py                 # re-render the latest run folder
    python scripts/refresh_report.py --new           # write a NEW timestamped folder
    python scripts/refresh_report.py --run reports/SnowflakeMsPurview-20261104_121500
 
Use this when:
  - You change a Jinja template (templates/*.j2) and want to see the result immediately
  - You want to rebuild the report after a completed pytest run
  - CI uploads the data snapshot and you want to rebuild the report locally
 
Test results and run metadata are read from the run folder's
jsonl/test_results.jsonl and jsonl/run_meta.jsonl.
"""
 
import argparse
import os
import sys
 
# Ensure project root is on the path when run as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
from dotenv import load_dotenv
load_dotenv()
 
from utils.report_data_builder import build_report_data
from utils.report_generator import (
    create_run_dir,
    find_latest_run_dir,
    generate_report,
    load_test_results,
)
 
_REPORTS_DIR = "reports"
 
 
def main() -> None:
    parser = argparse.ArgumentParser(description="Re-render the validation report.")
    parser.add_argument("--run", help="Existing run folder to re-render (default: latest).")
    parser.add_argument("--new", action="store_true",
                        help="Create a new timestamped folder instead of overwriting.")
    args = parser.parse_args()
 
    actual_dir   = "data/actual"
    expected_dir = "data/expected"
 
    # Verify data exists before proceeding
    missing_dirs = [d for d in (actual_dir, expected_dir) if not os.path.isdir(d)]
    if missing_dirs:
        print(f"ERROR: Data directories not found: {missing_dirs}")
        print("Run pytest first to extract data, then re-run this script.")
        sys.exit(1)
 
    # Which run folder do we read test results from / write into?
    source_dir = args.run or find_latest_run_dir(_REPORTS_DIR)
 
    test_results, meta = [], {}
    if source_dir and os.path.isdir(source_dir):
        test_results, meta = load_test_results(source_dir)
        print(f"Loaded {len(test_results)} test result(s) from {source_dir}/jsonl/")
    else:
        print(f"No previous run folder found under {_REPORTS_DIR}/ — test results will be empty.")
        print("Run pytest at least once to populate test results.")
 
    env = meta.get("environment") or os.environ.get("ENVIRONMENT", "Prod")
    print(f"Building report for environment: {env}")
    print(f"  Reading Snowflake data from : {actual_dir}/")
    print(f"  Reading Purview data from   : {expected_dir}/")
 
    report_data = build_report_data(
        environment  = env,
        test_results = test_results,
        start_time   = meta.get("start_time"),
        end_time     = meta.get("end_time"),
        actual_dir   = actual_dir,
        expected_dir = expected_dir,
    )
 
    # Print summary
    print("\nExtraction counts (from JSONL files):")
    for entity in ("databases", "schemas", "tables", "views", "columns", "stored_procedures"):
        sf  = report_data["extraction"].get(f"sf_{entity}", {}).get("count", 0)
        pv  = report_data["extraction"].get(f"pv_{entity}", {}).get("count", 0)
        cmp = report_data["comparison"].get(entity, {})
        missing = len(cmp.get("missing", []))
        extra   = len(cmp.get("extra",   []))
        status  = "OK" if (missing == 0 and extra == 0) else f"MISMATCH (missing={missing}, extra={extra})"
        print(f"  {entity:<20} SF={sf:>8,}  PV={pv:>8,}  {status}")
 
    if args.new or not source_dir or not os.path.isdir(source_dir):
        run_dir = create_run_dir(_REPORTS_DIR)
    else:
        run_dir = source_dir
 
    report_path = generate_report(report_data, run_dir)
    print(f"\nReport folder : {run_dir}")
    print(f"Open          : {report_path}")
 
 
if __name__ == "__main__":
    main()