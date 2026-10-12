"""Central definition of all data and report paths used across the project."""

# ── data directories ──────────────────────────────────────────────────────────
SNOWFLAKE_RESULTS_DIR = "data/snowflake_connector_validation/snowflake_results"
PURVIEW_RESULTS_DIR   = "data/snowflake_connector_validation/purview_results"

# ── report UI (source) and report outputs ─────────────────────────────────────
REPORT_UI_DIR = "report_ui"     # html/css/js templates; shared/ holds the common branding
REPORTS_ROOT  = "reports"       # generated; reports/assets/ is shared, one sub-folder per validation

SNOWFLAKE_CONNECTOR_VALIDATION = "snowflake_connector_validation"
PURVIEW_SCAN_TRIGGER_VALIDATION = "purview_scan_trigger_validation"

SNOWFLAKE_DATA_DIR   = f"data/{SNOWFLAKE_CONNECTOR_VALIDATION}"
SNOWFLAKE_REPORT_DIR = f"{REPORTS_ROOT}/{SNOWFLAKE_CONNECTOR_VALIDATION}"
REPORT_HTML = f"{SNOWFLAKE_REPORT_DIR}/index.html"
REPORT_JSON = f"{SNOWFLAKE_REPORT_DIR}/data/test_results.json"   # session meta, read by scripts/refresh_report.py

PURVIEW_SCAN_DATA_DIR   = f"data/{PURVIEW_SCAN_TRIGGER_VALIDATION}"
PURVIEW_SCAN_REPORT_DIR = f"{REPORTS_ROOT}/{PURVIEW_SCAN_TRIGGER_VALIDATION}"

# ── derived paths (used as default args in client methods) ────────────────────
SF_DATABASES_PATH         = f"{SNOWFLAKE_RESULTS_DIR}/snowflake_databases.jsonl"
SF_SCHEMAS_PATH           = f"{SNOWFLAKE_RESULTS_DIR}/snowflake_schemas.jsonl"
SF_TABLES_PATH            = f"{SNOWFLAKE_RESULTS_DIR}/snowflake_tables.jsonl"
SF_VIEWS_PATH             = f"{SNOWFLAKE_RESULTS_DIR}/snowflake_views.jsonl"
SF_COLUMNS_DIR            = f"{SNOWFLAKE_RESULTS_DIR}/columns"
SF_STORED_PROCEDURES_PATH = f"{SNOWFLAKE_RESULTS_DIR}/snowflake_stored_procedures.jsonl"

PV_COLLECTION_PATH        = f"{PURVIEW_RESULTS_DIR}/purview_collection_commercial.json"
PV_DATABASES_PATH         = f"{PURVIEW_RESULTS_DIR}/purview_databases.jsonl"
PV_SCHEMAS_PATH           = f"{PURVIEW_RESULTS_DIR}/purview_schemas.jsonl"
PV_TABLES_PATH            = f"{PURVIEW_RESULTS_DIR}/purview_tables.jsonl"
PV_VIEWS_PATH             = f"{PURVIEW_RESULTS_DIR}/purview_views.jsonl"
PV_COLUMNS_DIR            = f"{PURVIEW_RESULTS_DIR}/columns"
PV_STORED_PROCEDURES_PATH = f"{PURVIEW_RESULTS_DIR}/purview_stored_procedures.jsonl"
