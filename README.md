# purview-snowflake-validator

A **browser-free Python test framework** that validates whether a Microsoft Purview Snowflake Connector has correctly ingested Snowflake metadata — and flags drift in both directions.

[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![pytest](https://img.shields.io/badge/pytest-9.x-green.svg)](https://pytest.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Table of Contents

- [What's New](#whats-new)
- [How It Works](#how-it-works)
- [Comparison Logic](#comparison-logic)
- [MS Purview APIs and SDKs](#ms-purview-apis-and-sdks)
- [Project Layout](#project-layout)
- [Quick Start](#quick-start)
- [Regions (US / EU)](#regions-us--eu)
- [Snowflake Key-Pair Authentication](#snowflake-key-pair-authentication)
- [Live Progress](#live-progress)
- [Database Exclusions](#database-exclusions)
- [HTML Report](#html-report)
- [CI/CD](#cicd)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Developer Notes](#developer-notes)

---

## What's New

| Area | Change |
|------|--------|
| Snowflake login | Key-pair (JWT) authentication, including PrivateLink. `account` is the short account identifier and the PrivateLink URL goes in `host`. Password login is no longer used locally. |
| Regions | One run validates one region (`--region us` or `--region eu`). Settings can be set per region with a `_US` / `_EU` suffix. `config/regions.json` lists the databases per region. |
| Live progress | Step-by-step output while the run is in progress, plus a "still running" line every 15 seconds during slow steps such as columns. |
| Console output | Special symbols are converted to plain ASCII, so output is readable in any Windows terminal. |
| Report | Shows the **Account** (`US_NonProd` / `EU_NonProd`) and a **collection tree** with both US and EU. Each run writes its own report folder. |
| Naming | Every object name in the report is prefixed with the region and environment, for example `US_NonProd.<DB>.<SCHEMA>.<TABLE>`. |
| Exclusions | `SNOWFLAKE_EXCLUDE_DATABASES` is no longer needed. The region's database list decides what is validated. |

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│              BeforeAll hook  (conftest.py session fixture)          │
│                                                                     │
│  STEP 1  Connect Snowflake (key-pair) → fail → pytest.exit()        │
│  STEP 2  Connect Purview               → fail → pytest.exit()       │
│                                                                     │
│  STEP 3  Extract from Snowflake ACCOUNT_USAGE (SQL = query.sql)     │
│          databases · schemas · tables · views                       │
│          columns · stored_procedures  →  data/actual/               │
│                                                                     │
│  STEP 4  Extract from MS Purview Catalog + Atlas Entity API         │
│          same 6 entity types; columns via referredEntities          │
│          →  data/expected/                                          │
│                                                                     │
│  FILTER  Keep only the region's databases (config/regions.json)     │
│  STEP 5  Collection metadata (informational, never blocks tests)    │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │  data fully on disk
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  pytest tests (comparison only)                     │
│                                                                     │
│  test_databases.py          bidirectional DB diff                   │
│  test_schemas.py            bidirectional schema diff               │
│  test_tables.py             bidirectional table diff                │
│  test_views.py              bidirectional view diff                 │
│  test_columns.py            bidirectional column diff               │
│                             + empty description audit (info only)   │
│  test_stored_procedures.py  bidirectional stored proc diff          │
└─────────────────────────────────────────────────────────────────────┘
                                   │  session finish hook
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Tabbed HTML report                                                 │
│  reports/SnowflakeMsPurview-<REGION>-<timestamp>/report.html        │
│                                                                     │
│  Overview | Databases | Schemas | Tables | Views | Columns | SPs    │
│                                                                     │
│  Overview: Account (US_NonProd / EU_NonProd), collection tree       │
│  (US + EU), stats, extraction summary, test results.                │
│                                                                     │
│  Each entity tab has three collapsible subsections (hidden by       │
│  default, click ▶ to expand):                                       │
│    In Snowflake — NOT in Purview                                    │
│    In Purview — NOT in Snowflake                                    │
│    Empty Description in Purview                                     │
│  Large result sets are paginated at 50 rows per page.               │
│  Object names are prefixed: US_NonProd.<DB>.<SCHEMA>.<TABLE>        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Comparison Logic

The comparison is a **bidirectional set diff** performed for each of the six entity types. Understanding this logic helps interpret the report correctly.

### Step 1 — Build canonical keys

Each record from Snowflake and each record from Purview is reduced to a single, normalised string key. All keys are **uppercased** so comparison is case-insensitive.

| Entity | Snowflake key (from `data/actual/`) | Purview key (parsed from `qualifiedName`) |
|--------|-------------------------------------|-------------------------------------------|
| Database | `DATABASE_NAME` | `.../databases/<DB>` |
| Schema | `DATABASE_NAME.SCHEMA_NAME` | `.../databases/<DB>/schemas/<SCHEMA>` |
| Table | `TABLE_CATALOG.TABLE_SCHEMA.TABLE_NAME` | `.../databases/<DB>/schemas/<SCHEMA>/tables/<TABLE>` |
| View | `TABLE_CATALOG.TABLE_SCHEMA.TABLE_NAME` | `.../databases/<DB>/schemas/<SCHEMA>/views/<VIEW>` |
| Column | `TABLE_CATALOG.TABLE_SCHEMA.TABLE_NAME.COLUMN_NAME` | `.../tables/<TABLE>/columns/<COL>` |
| Stored Procedure | `PROCEDURE_CATALOG.PROCEDURE_SCHEMA.PROCEDURE_NAME` | `.../databases/<DB>/schemas/<SCHEMA>/procedures/<SP>` |

Purview stores every asset's `qualifiedName` as a URI of the form:

```
snowflake://account.snowflakecomputing.com/databases/MY_DB/schemas/MY_SCHEMA/tables/MY_TABLE
```

The `parse_qualified_name()` function in `utils/comparator.py` splits this URI on `/` and extracts each segment by its preceding path keyword (`databases`, `schemas`, `tables`, `views`, `columns`).

### Step 2 — Compute the diff

```
sf_keys  = { key(r) for r in snowflake_records }
pv_keys  = { key(r) for r in purview_records   }

missing_from_purview = sf_keys - pv_keys   # in Snowflake but NOT in Purview
extra_in_purview     = pv_keys - sf_keys   # in Purview but NOT in Snowflake
```

### Step 3 — Assert

Each pytest test asserts one direction of the diff:

- `test_all_snowflake_<entity>_exist_in_purview` — fails if `missing_from_purview` is non-empty.  
  These are objects the Purview connector has **not ingested** yet.
- `test_no_extra_<entity>_in_purview` — fails if `extra_in_purview` is non-empty.  
  These are objects present in Purview that **no longer exist** in Snowflake (stale / orphaned assets).

### Step 4 — Empty description audit

After the diff, every Purview JSONL file is scanned for records where the `description` field is absent or blank. These are surfaced per entity type in the HTML report as an informational table (not a test failure, but a data-quality indicator).

### Region filter and name prefix

Before the tests run, both the Snowflake and Purview data are trimmed to the databases listed for the region in `config/regions.json`. The matching itself is unchanged.

The region prefix (for example `US_NonProd`) is added **only when the report is built**. The JSONL files and the Snowflake ↔ Purview matching never contain it, so results are identical with or without the prefix.

### Why JSONL and not live API calls in tests?

Extraction happens **once** in the BeforeAll hook and writes JSONL files. All test functions then read only from those local files — there are no live cloud calls during test execution. This means:

- Tests run in milliseconds regardless of dataset size.
- Re-running the report (`python scripts/refresh_report.py`) does not require re-connecting to Snowflake or Purview.
- The JSONL snapshots are uploaded as CI artifacts for auditing.

---

## MS Purview APIs and SDKs

`clients/purview_client.py` uses the following interfaces. All endpoints are relative to `https://<account_name>.purview.azure.com`.

### Authentication

| Library | Usage |
|---------|-------|
| [`azure-identity`](https://pypi.org/project/azure-identity/) — `ClientSecretCredential` | Obtains an OAuth 2.0 bearer token for the scope `https://purview.azure.net/.default` using a service principal (client ID + secret + tenant ID). The token is refreshed automatically per request. |
| [`azure-purview-catalog`](https://pypi.org/project/azure-purview-catalog/) — `PurviewCatalogClient` | Used for client construction / endpoint wiring. Actual HTTP calls are made directly via `requests` for full control over retries and pagination. |

### REST Endpoints Called

#### 1. List collections
```
GET /account/collections
    ?api-version=2019-11-01-preview
```
Retrieves all collections in the Purview account. Used once at startup to resolve the machine reference name of the target sub-collection (e.g. `Commercial`) by matching `friendlyName` and walking the parent chain.

Reference: [Collections API — List](https://learn.microsoft.com/en-us/rest/api/purview/accountdataplane/collections/list-collections)

#### 2. Search entities (Catalog Search API)
```
POST /catalog/api/search/query
     ?api-version=2022-08-01-preview

Body:
{
  "filter": {
    "and": [
      { "entityType": "snowflake_database" },
      { "collectionId": "<reference_name>" }
    ]
  },
  "limit": 1000,
  "offset": 0
}
```
Used to extract **databases, schemas, tables, views, and stored procedures**. Paginated — successive calls increment `offset` by `limit` until a partial page is returned.

Entity types searched: `snowflake_database`, `snowflake_schema`, `snowflake_table`, `snowflake_view`, `snowflake_procedure`.

Reference: [Catalog Search — Query](https://learn.microsoft.com/en-us/rest/api/purview/catalogdataplane/discovery/query)

#### 3. Bulk entity fetch (Atlas Entity API)
```
GET /catalog/api/atlas/v2/entity/bulk
    ?minExtInfo=true
    &ignoreRelationships=false
    &guid=<guid1>&guid=<guid2>...
```
**Columns are not indexed** (`isIndexed: false`) and therefore do not appear in search results. To retrieve them, the client:

1. Collects all table and view GUIDs from the search results above.
2. Batch-fetches entities 20 GUIDs at a time via this endpoint.
3. Extracts column entities from the `referredEntities` map in each response. Each entry whose `typeName` is `snowflake_table_column` or `snowflake_view_column` is a column record.

Reference: [Atlas Entity — Get by GUIDs](https://learn.microsoft.com/en-us/rest/api/purview/catalogdataplane/entity/get-entities-by-guids)

### Retry Strategy

All `GET` and `POST` calls are wrapped in a retry loop:

| Attempt | Wait before retry |
|---------|------------------|
| 1 → 2 | 2 s |
| 2 → 3 | 4 s |
| 3 | raises |

Retried exceptions: `requests.ConnectionError`, `ChunkedEncodingError`, `ReadTimeout`. A fresh `requests.Session` is created after each connection-level error to clear any broken TCP state.

---

## Project Layout

```
purview-snowflake-validator/
├── clients/
│   ├── snowflake_client.py       # ACCOUNT_USAGE extraction (key-pair auth, mirrors query.sql)
│   └── purview_client.py         # Purview Catalog Search + Atlas Entity API
├── config/
│   └── regions.json              # Databases + environment per region (US / EU)
├── tests/
│   ├── test_databases.py         # Bidirectional database comparison
│   ├── test_schemas.py           # Bidirectional schema comparison
│   ├── test_tables.py            # Bidirectional table comparison
│   ├── test_views.py             # Bidirectional view comparison
│   ├── test_columns.py           # Bidirectional column comparison + empty desc audit
│   └── test_stored_procedures.py # Bidirectional stored procedure comparison
├── utils/
│   ├── comparator.py             # Key extractors, diff_sets(), compare_*()
│   ├── region_config.py          # Loads regions.json, resolves NAME_<REGION> settings
│   ├── report_data_builder.py    # Reads JSONL files → builds report data (applies name prefix)
│   └── report_generator.py       # Renders self-contained tabbed HTML report
├── templates/
│   └── report.html.j2            # Report template (Account, collection tree, tabs)
├── static/                       # Report CSS, JS and images
├── scripts/
│   ├── run_validation.py         # Runs every region, one after another
│   └── refresh_report.py         # Regenerate report from existing JSONL (no re-extract)
├── data/
│   ├── actual/                   # Snowflake JSONL (written by BeforeAll hook)
│   └── expected/                 # Purview JSONL  (written by BeforeAll hook)
├── reports/
│   └── SnowflakeMsPurview-<REGION>-<timestamp>/   # One folder per run (report.html, css, js, jsonl)
├── docs/
│   ├── ARCHITECTURE.md
│   └── DEVELOPER_GUIDE.md
├── query.sql                     # Reference SQL matching SnowflakeClient exactly
├── conftest.py                   # BeforeAll hook + session-finish report hook
├── pytest.ini
├── requirements.txt
├── .env.example
└── .github/workflows/ci.yml
```

---

## Quick Start

### Prerequisites

- Python 3.12+
- Snowflake service user with **key-pair authentication** (see [below](#snowflake-key-pair-authentication)). Its role must have `IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE` to query `ACCOUNT_USAGE`. Use a read-only scanner role, not `ACCOUNTADMIN`.
- Azure AD service principal with the **Purview Data Reader** role on the Purview account

### 1. Clone and install

```bash
git clone https://github.com/YOUR_ORG/purview-snowflake-validator.git
cd purview-snowflake-validator
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in your values. Region-specific settings use a `_US` / `_EU` suffix; values that are the same for both regions are written once. The validator looks for `NAME_<REGION>` first and then `NAME`.

```bash
# ── Shared (both regions) ──────────────────────────────────────────────────
AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_SECRET=...
PURVIEW_ACCOUNT_NAME=my-purview-account
DOMAIN_NAME=myorg.com
PURVIEW_COLLECTION_COMMERCIAL=Commercial   # optional — default is "Commercial"

# ── Snowflake: US ──────────────────────────────────────────────────────────
SNOWFLAKE_ACCOUNT_US=<short account identifier>   # not the full URL
SNOWFLAKE_USER_US=<service user>
SNOWFLAKE_ROLE_US=<read-only scanner role>
SNOWFLAKE_WAREHOUSE_US=<warehouse>
SNOWFLAKE_HOST_US=<account>.privatelink.snowflakecomputing.com   # PrivateLink host
SNOWFLAKE_PRIVATE_KEY_PATH_US=C:\\path\\to\\snowflake_us_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE_US=...

# ── Snowflake: EU ──────────────────────────────────────────────────────────
SNOWFLAKE_ACCOUNT_EU=<short account identifier>
SNOWFLAKE_USER_EU=<service user>
SNOWFLAKE_ROLE_EU=<read-only scanner role>
SNOWFLAKE_WAREHOUSE_EU=<warehouse>
SNOWFLAKE_HOST_EU=<account>.privatelink.snowflakecomputing.com
SNOWFLAKE_PRIVATE_KEY_PATH_EU=C:\\path\\to\\snowflake_eu_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE_EU=...

# ── Environment (only used when config/regions.json is not present) ────────
ENVIRONMENT=Prod                           # Prod or NonProd
```

Optional per-region overrides: `DOMAIN_NAME_<REGION>`, `PURVIEW_ACCOUNT_NAME_<REGION>`, `PURVIEW_COLLECTION_<REGION>`.

> **Always excluded from every query** — no configuration needed:
> `SNOWFLAKE`, `SNOWFLAKE_SAMPLE_DATA`, `SNOWFLAKE_LEARNING_DB`, and any database matching `USER$*`.

### 3. Run

One run validates one region:

```bash
pytest --region us
pytest --region eu
```

To run every region one after another:

```bash
python scripts/run_validation.py
```

`--region` takes a single value (`--region eu us` does not work). If several regions are configured and no `--region` is given, the run stops and tells you to pick one.

### 4. Run a specific entity type only

```bash
pytest --region us -m databases
pytest --region us -m "tables or views"
pytest --region us -m columns -v
```

### 5. Regenerate the report without re-running extraction

```bash
python scripts/refresh_report.py
```

Uses existing JSONL files in `data/` — no cloud connections required. To include the region prefix in a refreshed report, pass `prefix="US_NonProd"` (or `"EU_NonProd"`) in its call to `build_report_data`.

---

## Regions (US / EU)

`config/regions.json` lists the databases and environment for each region:

```json
{
  "regions": {
    "us": {
      "label": "US NonProd",
      "environment": "NonProd",
      "databases": ["<DB1>", "<DB2>"]
    },
    "eu": {
      "label": "EU NonProd",
      "environment": "NonProd",
      "collection_root": "Vantive EU",
      "databases": ["<DB1>", "<DB2>"]
    }
  }
}
```

| Key | Meaning |
|-----|---------|
| `label` | Name shown in the report header (for example "US NonProd"). |
| `environment` | Purview collection to validate (for example NonProd). Also forms the report prefix, `<REGION>_<environment>`. |
| `databases` | The databases kept for this region. Both the Snowflake and Purview sides are trimmed to this list. |
| `collection_root` | Optional. Exact top-level collection name shown in the report tree. Defaults to `Vantive <REGION>`. |

Without `config/regions.json`, the validator behaves as before and can be limited with `VALIDATE_DATABASES` in `.env`.

---

## Snowflake Key-Pair Authentication

Snowflake uses a private key (JWT) instead of a password.

**1. Create a key pair** (one per region). The private key is protected by a passphrase:

```python
import getpass
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

pw = getpass.getpass("Choose a passphrase: ").encode()
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

with open("snowflake_key.p8", "wb") as f:
    f.write(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(pw),
    ))

pub = key.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()
open("snowflake_key.pub", "w").write(pub)
```

**2. Register the public key** on the Snowflake service user for that region. Paste the key body only, without the BEGIN / END lines:

```sql
ALTER USER <service_user> SET RSA_PUBLIC_KEY='<public key body>';
```

**3. Verify the fingerprint** if login fails. The fingerprint of your key must equal `RSA_PUBLIC_KEY_FP` from `DESC USER <service_user>;`.

**4. Point `.env`** at the private key and passphrase (see [Quick Start](#2-configure-environment-variables)).

### PrivateLink

- `SNOWFLAKE_ACCOUNT_<REGION>` is the short account identifier, not the full URL.
- `SNOWFLAKE_HOST_<REGION>` is the full PrivateLink host name.
- The client sends `authenticator=SNOWFLAKE_JWT` and passes `host` separately from `account`.

---

## Live Progress

`conftest.py` prints each step as it runs, with a record count and elapsed time, even though pytest normally captures output inside fixtures.

During slow steps (for example Snowflake or Purview columns) a line such as `... Purview columns still running (45s)` is printed every 15 seconds, so a long run never looks frozen. Output uses plain ASCII symbols (`->`, `OK`, `<->`), so it reads correctly in any Windows console.

---

## Database Exclusions

The following databases are hardcoded in `SnowflakeClient` and excluded from **every query** automatically:

| Database | Reason |
|----------|--------|
| `SNOWFLAKE` | Internal Snowflake system database |
| `SNOWFLAKE_SAMPLE_DATA` | Sample dataset |
| `SNOWFLAKE_LEARNING_DB` | Training database |
| `USER$*` (LIKE pattern) | Personal user workspace databases |

Which databases are validated for a region is decided by the `databases` list in `config/regions.json`, so `SNOWFLAKE_EXCLUDE_DATABASES` is no longer needed. It is still accepted if set.

---

## HTML Report

Each run writes its own folder, `reports/SnowflakeMsPurview-<REGION>-<timestamp>/`, containing `report.html` and its CSS, JS, images and JSONL downloads. The path is printed at the end of the run. Open `report.html` in any browser.

**Overview tab**

- Header and run line: **Account** (`US_NonProd` or `EU_NonProd`), region, environment, duration.
- **Collection Metadata** with a **collection tree** showing both US and EU. The region validated in this run is highlighted.
- Stat cards (pass / fail / skip), extraction counts per entity, and the full test result list with duration and failure details.

**Per-entity tabs** (Databases · Schemas · Tables · Views · Columns · Stored Procedures):

Each tab shows a status banner with counts, then three collapsible sections (all hidden by default — click `▶` to expand):

| Section | Content |
|---------|---------|
| ▶ In Snowflake — NOT in Purview | Objects the connector has not yet ingested |
| ▶ In Purview — NOT in Snowflake | Stale / orphaned assets in Purview |
| ▶ Empty Description in Purview | Data quality audit — assets with no description |

Tables with many rows are paginated at 50 rows per page with Prev / Next buttons. Tabs with mismatches are marked with a red dot.

### Name prefix

Every object name in the report starts with the region and environment:

```
US_NonProd.<DB>
US_NonProd.<DB>.<SCHEMA>
US_NonProd.<DB>.<SCHEMA>.<TABLE>
US_NonProd.<DB>.<SCHEMA>.<TABLE>.<COLUMN>
```

In the empty-description lists, the prefixed name is in the `fullName` column. The prefix is a display change only; see [Comparison Logic](#region-filter-and-name-prefix).

---

## CI/CD

The workflow (`.github/workflows/ci.yml`) is **manually triggered only** — it does not run on push or pull request.

> **Note:** the workflow still uses Snowflake password secrets. Local runs now use key-pair authentication and `--region`, so `ci.yml` must be updated to match (key-pair secrets and a `--region` argument) before it is used against a key-pair-only account.

### How to run

1. Go to **Actions → CI → Run workflow**
2. Select **Environment**: `Prod` or `NonProd`
3. Click **Run workflow**

The workflow runs two jobs in sequence:

| Job | What it does |
|-----|-------------|
| `lint-and-collect` | Ruff lint + `pytest --collect-only` — validates code quality with no cloud credentials |
| `integration-tests` | Full extraction + comparison test suite against live Snowflake and Purview |

### Artifact

Each run uploads a single artifact named `validation-results-<env>-<run_number>` (retained 30 days) containing:

```
reports/report.html      ← HTML validation report
data/actual/             ← Snowflake JSONL snapshots
data/expected/           ← Purview JSONL snapshots
```

### GitHub Secrets required

| Secret | Required | Description |
|--------|----------|-------------|
| `SNOWFLAKE_ACCOUNT` | ✅ | Account identifier (e.g. `xy12345.us-east-1`) |
| `SNOWFLAKE_USER` | ✅ | Snowflake username |
| `SNOWFLAKE_PASSWORD` | ✅ | Snowflake password (to be replaced by key-pair secrets, see note above) |
| `SNOWFLAKE_WAREHOUSE` | ⬜ optional | Compute warehouse — uses account default if blank |
| `SNOWFLAKE_ROLE` | ⬜ optional | Role — uses user default if blank |
| `SNOWFLAKE_EXCLUDE_DATABASES` | ⬜ optional | Comma-separated additional databases to exclude |
| `PURVIEW_ACCOUNT_NAME` | ✅ | Purview account name |
| `AZURE_TENANT_ID` | ✅ | Azure AD tenant ID |
| `AZURE_CLIENT_ID` | ✅ | Service principal client ID |
| `AZURE_CLIENT_SECRET` | ✅ | Service principal secret |
| `DOMAIN_NAME` | ✅ | Organisation domain (e.g. `acme.com`) |

---

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `Several regions are configured… Run with --region <name>` | Add `--region us` or `--region eu`, or use `python scripts/run_validation.py`. |
| `390144 (28000): JWT token is invalid` | Snowflake has no matching public key for that user and account. Register the public key, compare fingerprints, and check the account and user values. |
| `unexpected keyword argument 'host'` | `clients/snowflake_client.py` is an old version. Update it (the constructor takes `host`). |
| `Missing setting … in .env` | Add the named variable, with the `_US` / `_EU` suffix if it is region-specific. |
| Permission error on `ACCOUNT_USAGE` | The role needs imported privileges on the `SNOWFLAKE` database. Set `SNOWFLAKE_ROLE_<REGION>` to a role that has them. |
| No output during the columns step | Normal for large catalogs. A heartbeat prints every 15 seconds; wait for the step to finish. |
| Garbled characters in the terminal | Update `conftest.py` (output is converted to plain ASCII). |
| Account line or collection tree missing from the report | The report generator is not passing the new fields to the template. Check `utils/report_generator.py`. |

---

## Security

- Never commit `.env`, `*.p8` or `*.pub`. Make sure they are listed in `.gitignore`.
- Keep each private key and passphrase on the machine that runs the validator.
- Rotate any key or passphrase that has been shared, shown in a screenshot, or pasted into a chat.
- Use a read-only role for the scanner user.

---

## Developer Notes

### Snowflake queries

All SQL in `SnowflakeClient` mirrors `query.sql` exactly — same column selections, same `WHERE` clauses, same `ORDER BY`. The `.sql` file can be run in Snowflake Worksheets for manual verification.

### Purview column extraction challenge

`snowflake_table_column` and `snowflake_view_column` have `isIndexed: false` in the Purview data model — they are invisible to the Catalog Search API. The workaround: collect all table/view GUIDs from search results, then call the Atlas Bulk Entity API (`/entity/bulk?minExtInfo=true`) in batches of 20 GUIDs, and extract columns from the `referredEntities` map in each response.

### Per-schema column files

Columns are written to `data/actual/columns/<DB>/<SCHEMA>.jsonl` and `data/expected/columns/<DB>/<SCHEMA>.jsonl`. This avoids creating single multi-GB files that would be impractical to open or diff.

### Per-region settings

`conftest.py` resolves every setting through `_setting(name)`, which looks for `NAME_<REGION>` first and falls back to `NAME`. Required values use `_need(name)` and stop the run with a clear message if missing.

### Report data

`utils/report_data_builder.py` takes an optional `prefix` argument. It prefixes the missing / extra names and adds `fullName` to the empty-description records. `conftest.py` also passes the collection tree (`collection_tree`) for the Overview tab.

---

## Further Reading

- [Architecture Overview](docs/ARCHITECTURE.md)
- [Developer Guide](docs/DEVELOPER_GUIDE.md)
- [Reference SQL](query.sql)

---

## License

MIT. See [LICENSE](LICENSE).
