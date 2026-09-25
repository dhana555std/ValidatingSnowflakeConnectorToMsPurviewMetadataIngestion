# Developer Guide

This document covers everything you need to set up, run, extend, and contribute to **purview-snowflake-validator**.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Local Setup](#local-setup)
3. [Environment Variables](#environment-variables)
4. [Running Tests](#running-tests)
5. [Adding New Entity Types](#adding-new-entity-types)
6. [CI Secrets](#ci-secrets)
7. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python ≥ 3.12 | 3.14 tested |
| Snowflake account | User must have `ACCOUNT_USAGE` view grants |
| MS Purview account | Service principal needs **Purview Data Reader** on the Commercial collection |
| `git` | For version control |

---

## Local Setup

```bash
# 1. Clone
git clone https://github.com/YOUR_ORG/purview-snowflake-validator.git
cd purview-snowflake-validator

# 2. Virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Dependencies
pip install -r requirements.txt

# 4. Credentials
cp .env.example .env
# Fill in .env — see the table below
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in each value. **Never commit `.env`** — it is already in `.gitignore`.

### Snowflake

| Variable | Example | Description |
|----------|---------|-------------|
| `SNOWFLAKE_ACCOUNT` | `xy12345.us-east-1` | Account identifier. Find it under **Admin → Accounts** in Snowsight. |
| `SNOWFLAKE_USER` | `svc_validator` | Snowflake username. |
| `SNOWFLAKE_PASSWORD` | `...` | Password for the above user. |
| `SNOWFLAKE_WAREHOUSE` | `COMPUTE_WH` | Warehouse to execute `ACCOUNT_USAGE` queries. |
| `SNOWFLAKE_ROLE` | `ACCOUNTADMIN` | Role that can read `SNOWFLAKE.ACCOUNT_USAGE`. Defaults to `ACCOUNTADMIN`. |
| `SNOWFLAKE_EXCLUDE_DATABASES` | `SNOWFLAKE,SNOWFLAKE_SAMPLE_DATA,SNOWFLAKE_LEARNING_DB` | Comma-separated list of system / scratch databases to skip in every query and guard against in Purview. |

### Microsoft Purview

| Variable | Example | Description |
|----------|---------|-------------|
| `PURVIEW_ACCOUNT_NAME` | `my-purview` | Account name — the subdomain of `*.purview.azure.com`. |
| `AZURE_TENANT_ID` | `xxxxxxxx-xxxx-...` | Azure AD tenant (directory) ID. |
| `AZURE_CLIENT_ID` | `xxxxxxxx-xxxx-...` | Service principal application ID. |
| `AZURE_CLIENT_SECRET` | `...` | Client secret for the service principal. |
| `DOMAIN_NAME` | `mydomain` | Purview domain (used for logging; not sent to the API). |
| `PURVIEW_COLLECTION_PROD` | `Prod` | Friendly name of the parent collection (case-sensitive). |
| `PURVIEW_COLLECTION_COMMERCIAL` | `Commercial` | Friendly name of the target sub-collection (case-sensitive). |

> **Purview permissions**: the service principal must be assigned the **Purview Data Reader** role on the collection and have access to read the Atlas catalog. Grant it in Purview → Data Map → Collections → Commercial → Role Assignments.

---

## Running Tests

```bash
# Full suite (all tests, generates reports/report.html)
pytest

# Snowflake extraction only
pytest -m "databases or schemas or tables or views or columns or stored_procedures"

# Purview extraction only
pytest -m "purview_databases or purview_schemas or purview_tables or purview_views or purview_columns or purview_stored_procedures"

# Drift detection (requires both sides to have been extracted first)
pytest -m comparison

# Excluded-database guard
pytest -m excluded_objects

# Single test file
pytest tests/test_tables.py -v

# Stop on first failure
pytest -x

# Show captured stdout (extraction summaries)
pytest -s
```

The HTML report is always written to `reports/report.html`. Both `data/actual/` and `data/expected/` are **wiped at the start of every session** so you always get a clean run.

---

## Adding New Entity Types

The framework is designed to be extended. To add a new Snowflake entity (e.g. `snowflake_stage`):

### Step 1 — Extract from Snowflake

In `clients/snowflake_client.py`, add a method following the existing pattern:

```python
def extract_all_stages(self, out_path: str = "data/actual/stages.jsonl") -> Dict[str, Any]:
    return self._stream_to_jsonl(
        self._stream(f"""
            SELECT ...
            FROM SNOWFLAKE.ACCOUNT_USAGE.STAGES
            WHERE DELETED IS NULL
              {self._exclude_clause('STAGE_CATALOG')}
            ORDER BY ...
        """),
        out_path,
    )
```

### Step 2 — Extract from Purview

In `clients/purview_client.py`, add the entity type to `_ENTITY_TYPES` and add an `extract_stages()` method:

```python
_ENTITY_TYPES = {
    ...,
    "stages": "snowflake_stage",
}

def extract_stages(self, out_path: str = "data/expected/purview_stages.jsonl") -> Dict[str, Any]:
    return self._stream_to_jsonl(self._search(_ENTITY_TYPES["stages"]), out_path)
```

### Step 3 — Write tests

Create `tests/test_stages.py` and `tests/test_purview_stages.py` mirroring the existing test files.

### Step 4 — Add comparison

In `utils/comparator.py`, add key extractor functions and a `compare_stages()` function. Then add a test in `tests/test_z_comparison.py`.

### Step 5 — Register the marker

In `pytest.ini`, add `stages` and `purview_stages` to the `markers` list.

---

## CI Secrets

Add the following as **Actions secrets** in your GitHub repository settings (`Settings → Secrets and variables → Actions`):

| Secret | Maps to |
|--------|---------|
| `SNOWFLAKE_ACCOUNT` | `SNOWFLAKE_ACCOUNT` |
| `SNOWFLAKE_USER` | `SNOWFLAKE_USER` |
| `SNOWFLAKE_PASSWORD` | `SNOWFLAKE_PASSWORD` |
| `SNOWFLAKE_WAREHOUSE` | `SNOWFLAKE_WAREHOUSE` |
| `SNOWFLAKE_ROLE` | `SNOWFLAKE_ROLE` |
| `SNOWFLAKE_EXCLUDE_DATABASES` | `SNOWFLAKE_EXCLUDE_DATABASES` |
| `PURVIEW_ACCOUNT_NAME` | `PURVIEW_ACCOUNT_NAME` |
| `AZURE_TENANT_ID` | `AZURE_TENANT_ID` |
| `AZURE_CLIENT_ID` | `AZURE_CLIENT_ID` |
| `AZURE_CLIENT_SECRET` | `AZURE_CLIENT_SECRET` |
| `DOMAIN_NAME` | `DOMAIN_NAME` |
| `PURVIEW_COLLECTION_PROD` | `PURVIEW_COLLECTION_PROD` |
| `PURVIEW_COLLECTION_COMMERCIAL` | `PURVIEW_COLLECTION_COMMERCIAL` |

The CI workflow skips the full integration run when secrets are absent (e.g. forks or external PRs). Only test collection and linting run in that case.

---

## Troubleshooting

### `ReadTimeout` during Purview column extraction

Columns are fetched via the Atlas bulk entity API in batches of 20 GUIDs. Each request has a 120-second timeout with 3 automatic retries using exponential back-off. If timeouts persist:

- Reduce `_ENTITY_BULK_SIZE` in `clients/purview_client.py` (try `10`).
- Increase `_REQUEST_TIMEOUT` (try `180`).

### Column extraction returns 0 records

Purview columns are **not** indexed for the Catalog Search API. The framework resolves them through the Atlas `entity/bulk` endpoint using `referredEntities`. Verify:

1. The Commercial collection reference resolved correctly (check the `[INFO]` log line).
2. Tables were successfully extracted first (`data/expected/purview_tables.jsonl` exists and is non-empty).

### `ConnectionResetError` mid-run

The Purview API occasionally resets connections under high query rates. The client automatically creates a fresh `requests.Session` after a connection error and retries.

### Snowflake `ACCOUNT_USAGE` data is stale

`ACCOUNT_USAGE` views have a latency of up to 3 hours. Counts may differ from live `INFORMATION_SCHEMA` queries during that window.

### Collection not found

`PURVIEW_COLLECTION_PROD` and `PURVIEW_COLLECTION_COMMERCIAL` are **case-sensitive friendly names** in the Purview portal. Verify the exact casing in the Purview UI → Data Map → Collections.
