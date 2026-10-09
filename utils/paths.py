"""Central definition of all data and report paths used across the project."""

# ── data directories ──────────────────────────────────────────────────────────
SNOWFLAKE_RESULTS_DIR = "data/snowflake_results"
PURVIEW_RESULTS_DIR   = "data/purview_snowflake_results"

# ── report outputs ────────────────────────────────────────────────────────────
REPORT_HTML   = "reports/purview_snowflake_connector_report.html"
REPORT_JSON   = "reports/purview_snowflake_connector_results.json"

# ── derived paths (used as default args in client methods) ────────────────────
SF_DATABASES_PATH         = f"{SNOWFLAKE_RESULTS_DIR}/databases.jsonl"
SF_SCHEMAS_PATH           = f"{SNOWFLAKE_RESULTS_DIR}/schemas.jsonl"
SF_TABLES_PATH            = f"{SNOWFLAKE_RESULTS_DIR}/tables.jsonl"
SF_VIEWS_PATH             = f"{SNOWFLAKE_RESULTS_DIR}/views.jsonl"
SF_COLUMNS_DIR            = f"{SNOWFLAKE_RESULTS_DIR}/columns"
SF_STORED_PROCEDURES_PATH = f"{SNOWFLAKE_RESULTS_DIR}/stored_procedures.jsonl"

PV_COLLECTION_PATH        = f"{PURVIEW_RESULTS_DIR}/purview_collection_commercial.json"
PV_DATABASES_PATH         = f"{PURVIEW_RESULTS_DIR}/purview_databases.jsonl"
PV_SCHEMAS_PATH           = f"{PURVIEW_RESULTS_DIR}/purview_schemas.jsonl"
PV_TABLES_PATH            = f"{PURVIEW_RESULTS_DIR}/purview_tables.jsonl"
PV_VIEWS_PATH             = f"{PURVIEW_RESULTS_DIR}/purview_views.jsonl"
PV_COLUMNS_DIR            = f"{PURVIEW_RESULTS_DIR}/columns"
PV_STORED_PROCEDURES_PATH = f"{PURVIEW_RESULTS_DIR}/purview_stored_procedures.jsonl"
