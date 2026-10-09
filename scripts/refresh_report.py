"""
refresh_report.py
=================
Regenerates reports/purview_snowflake_connector_report.html from the JSONL files already on disk
(data/snowflake_connector_validation/snowflake_results/ and data/snowflake_connector_validation/purview_results/) WITHOUT re-running extraction or tests.

Run from the project root:
    python scripts/refresh_report.py

Use this when:
  - You change report_generator.py and want to see the result immediately
  - You want to reshare the report after a completed pytest run
  - CI uploads the data snapshot and you want to rebuild the HTML locally
"""

import os
import sys

# Ensure project root is on the path when run as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from utils.paths import SNOWFLAKE_RESULTS_DIR, PURVIEW_RESULTS_DIR, REPORT_HTML, REPORT_JSON
from utils.report_data_builder import build_report_data
from utils.report_generator import generate_report


def main() -> None:
    import json

    actual_dir   = SNOWFLAKE_RESULTS_DIR
    expected_dir = PURVIEW_RESULTS_DIR
    results_file = REPORT_JSON

    # Verify data exists before proceeding
    missing_dirs = [d for d in (actual_dir, expected_dir) if not os.path.isdir(d)]
    if missing_dirs:
        print(f"ERROR: Data directories not found: {missing_dirs}")
        print("Run pytest first to extract data, then re-run this script.")
        sys.exit(1)

    # Load persisted test results from the last pytest run
    test_results = []
    start_time   = None
    end_time     = None
    env          = os.environ.get("ENVIRONMENT", "Prod")

    if os.path.exists(results_file):
        with open(results_file, encoding="utf-8") as f:
            meta = json.load(f)
        test_results = meta.get("test_results", [])
        start_time   = meta.get("start_time")
        end_time     = meta.get("end_time")
        env          = meta.get("environment", env)
        print(f"Loaded {len(test_results)} test result(s) from {results_file}")
    else:
        print(f"No {results_file} found — test results section will be empty.")
        print("Run pytest at least once to populate test results.")

    print(f"Building report for environment: {env}")
    print(f"  Reading Snowflake data from : {actual_dir}/")
    print(f"  Reading Purview data from   : {expected_dir}/")

    report_data = build_report_data(
        environment  = env,
        test_results = test_results,
        start_time   = start_time,
        end_time     = end_time,
        actual_dir   = actual_dir,
        expected_dir = expected_dir,
    )

    # Print summary
    print("\nExtraction counts (from JSONL files):")
    for entity in ("databases", "schemas", "tables", "views", "columns", "stored_procedures"):
        sf = report_data["extraction"].get(f"sf_{entity}", {}).get("count", 0)
        pv = report_data["extraction"].get(f"pv_{entity}", {}).get("count", 0)
        cmp = report_data["comparison"].get(entity, {})
        missing = len(cmp.get("missing", []))
        extra   = len(cmp.get("extra",   []))
        status  = "OK" if (missing == 0 and extra == 0) else f"MISMATCH (missing={missing}, extra={extra})"
        print(f"  {entity:<20} SF={sf:>8,}  PV={pv:>8,}  {status}")

    os.makedirs("reports/snowflake_connector_validation", exist_ok=True)
    generate_report(report_data, REPORT_HTML)
    print(f"\nReport written to: {REPORT_HTML}")


if __name__ == "__main__":
    main()
