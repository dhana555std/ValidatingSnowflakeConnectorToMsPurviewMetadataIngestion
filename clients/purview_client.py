"""
purview_client
==============
Extraction client for MS Purview metadata via the Catalog Search API and the
Atlas Entity REST API.

Design notes
------------
Searchable entities (databases, schemas, tables, views, stored procedures)
    Retrieved page-by-page using ``POST /catalog/api/search/query`` filtered
    to the Commercial collection's machine reference name, which is resolved
    once at startup and cached for the session.

Columns (``snowflake_table_column``, ``snowflake_view_column``)
    Purview does **not** index column entities for search (``isIndexed: false``
    in the data model).  Columns are embedded as ``referredEntities`` inside
    their parent table or view entity.  The client collects all table/view
    GUIDs, then batch-fetches them 20 at a time via the Atlas
    ``/entity/bulk?minExtInfo=true`` endpoint, yielding column entities from
    the ``referredEntities`` map.

Resilience
    Each HTTP request is made through a shared ``requests.Session`` with a
    120-second timeout.  ``ConnectionError``, ``ChunkedEncodingError``, and
    ``ReadTimeout`` are retried up to three times using exponential back-off
    (2 s, 4 s, 8 s).  A fresh ``Session`` is created after each connection-
    level error.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Generator, List, Optional

import requests
from azure.identity import ClientSecretCredential
from azure.purview.catalog import PurviewCatalogClient

logger = logging.getLogger(__name__)

_BATCH_SIZE = 1_000
_ENTITY_BULK_SIZE = 20        # GUIDs per bulk entity fetch (keeps response size manageable)
_BULK_DELAY_S = 0.1           # pause between bulk calls
_MAX_RETRIES = 3
_REQUEST_TIMEOUT = 120        # seconds per request
_COLLECTIONS_API_VERSION = "2019-11-01-preview"
_SEARCH_API_VERSION = "2022-08-01-preview"

_ENTITY_TYPES = {
    "databases": "snowflake_database",
    "schemas": "snowflake_schema",
    "tables": "snowflake_table",
    "views": "snowflake_view",
    "stored_procedures": "snowflake_procedure",
}

_COLUMN_TYPES = {"snowflake_table_column", "snowflake_view_column"}


class PurviewClient:
    """MS Purview metadata extractor.

    Authenticates via a service principal (``ClientSecretCredential``) and
    provides streaming extraction methods for each Snowflake entity type
    registered in the Commercial sub-collection.

    Parameters
    ----------
    account_name:
        Purview account name — the subdomain of ``<name>.purview.azure.com``.
    tenant_id:
        Azure AD tenant (directory) ID.
    client_id:
        Service principal application (client) ID.
    client_secret:
        Client secret for the service principal.
    domain_name:
        Purview domain name (used for logging only).
    collection_prod:
        Friendly name of the parent Purview collection (case-sensitive).
        Defaults to ``"Prod"``.
    collection_commercial:
        Friendly name of the target sub-collection (case-sensitive).
        Defaults to ``"Commercial"``.
    """

    def __init__(
        self,
        account_name: str,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        domain_name: str,
        collection_prod: str = "Prod",
        collection_commercial: str = "Commercial",
    ):
        self._account_name = account_name
        self._domain_name = domain_name
        self._collection_prod = collection_prod
        self._collection_commercial = collection_commercial
        self._endpoint = f"https://{account_name}.purview.azure.com"

        self._credential = ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        self._catalog = PurviewCatalogClient(
            endpoint=self._endpoint,
            credential=self._credential,
        )
        self._commercial_ref: Optional[str] = None
        self._session = requests.Session()

    # ── auth helpers ───────────────────────────────────────────────

    def _token(self) -> str:
        return self._credential.get_token("https://purview.azure.net/.default").token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def _get_json(self, url: str, params: Optional[Dict] = None) -> Any:
        _retryable = (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ReadTimeout,
        )
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.get(
                    url, headers=self._headers(), params=params or {}, timeout=_REQUEST_TIMEOUT
                )
                resp.raise_for_status()
                return resp.json()
            except _retryable as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "GET failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, _MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)
                self._session = requests.Session()

    def _post_json(self, url: str, body: Dict, params: Optional[Dict] = None) -> Any:
        resp = self._session.post(
            url, headers=self._headers(), params=params or {}, json=body, timeout=_REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()

    # ── collection resolution ──────────────────────────────────────

    def _resolve_commercial(self) -> str:
        """Resolve the machine reference name of the Commercial sub-collection."""
        if self._commercial_ref:
            return self._commercial_ref

        url = f"{self._endpoint}/account/collections"
        data = self._get_json(url, params={"api-version": _COLLECTIONS_API_VERSION})
        collections = data.get("value", [])

        col_by_name: Dict[str, Any] = {c["name"]: c for c in collections}

        for col in collections:
            if col.get("friendlyName") == self._collection_commercial:
                parent_ref = col.get("parentCollection", {}).get("referenceName", "")
                parent = col_by_name.get(parent_ref, {})
                if parent.get("friendlyName") == self._collection_prod:
                    self._commercial_ref = col["name"]
                    logger.info(
                        "Resolved '%s' collection reference: %s",
                        self._collection_commercial,
                        self._commercial_ref,
                    )
                    return self._commercial_ref

        raise ValueError(
            f"Could not find '{self._collection_commercial}' sub-collection "
            f"under '{self._collection_prod}' in Purview account '{self._account_name}'"
        )

    # ── collection metadata ────────────────────────────────────────

    def get_collection_metadata(self) -> Dict[str, Any]:
        ref = self._resolve_commercial()
        url = f"{self._endpoint}/account/collections/{ref}"
        return self._get_json(url, params={"api-version": _COLLECTIONS_API_VERSION})

    # ── streaming search ───────────────────────────────────────────

    def _search(self, entity_type: str) -> Generator[Dict[str, Any], None, None]:
        collection_ref = self._resolve_commercial()
        url = f"{self._endpoint}/catalog/api/search/query"
        params = {"api-version": _SEARCH_API_VERSION}
        offset = 0
        while True:
            body = {
                "filter": {
                    "and": [
                        {"entityType": entity_type},
                        {"collectionId": collection_ref},
                    ]
                },
                "limit": _BATCH_SIZE,
                "offset": offset,
            }
            response = self._post_json(url, body, params=params)
            batch = response.get("value", [])
            yield from batch
            if len(batch) < _BATCH_SIZE:
                break
            offset += _BATCH_SIZE

    # ── JSONL streaming writer (same pattern as SnowflakeClient) ───

    @staticmethod
    def _stream_to_jsonl(
        rows: Generator[Dict[str, Any], None, None],
        out_path: str,
    ) -> Dict[str, Any]:
        dir_name = os.path.dirname(out_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        count = 0
        fields: List[str] = []
        sample: List[Dict[str, Any]] = []

        with open(out_path, "w", encoding="utf-8") as f:
            for row in rows:
                row = dict(row)
                if count == 0:
                    fields = list(row.keys())
                if len(sample) < 5:
                    sample.append(row)
                f.write(json.dumps(row, default=str) + "\n")
                count += 1

        return {"count": count, "fields": fields, "sample": sample, "output": out_path}

    # ── bulk entity fetch (columns live inside table/view entities) ──

    @staticmethod
    def _parse_qualified_name(qn: str) -> Dict[str, str]:
        """Extract database/schema/table/column names from a Snowflake qualifiedName."""
        parts = qn.split("/")
        result: Dict[str, str] = {}
        for key in ("databases", "schemas", "tables", "views", "columns"):
            if key in parts:
                idx = parts.index(key)
                if idx + 1 < len(parts):
                    result[key.rstrip("s")] = parts[idx + 1]
        return result

    def _stream_columns_from_guids(
        self, guids: List[str]
    ) -> Generator[Dict[str, Any], None, None]:
        """Batch-fetch entities by GUID and yield their nested column entities."""
        url = f"{self._endpoint}/catalog/api/atlas/v2/entity/bulk"
        total_batches = (len(guids) + _ENTITY_BULK_SIZE - 1) // _ENTITY_BULK_SIZE
        for batch_num, i in enumerate(range(0, len(guids), _ENTITY_BULK_SIZE), 1):
            batch = guids[i: i + _ENTITY_BULK_SIZE]
            guid_qs = "&".join(f"guid={g}" for g in batch)
            full_url = f"{url}?minExtInfo=true&ignoreRelationships=false&{guid_qs}"
            data = self._get_json(full_url)
            referred = data.get("referredEntities", {})
            for col_entity in referred.values():
                if col_entity.get("typeName") not in _COLUMN_TYPES:
                    continue
                attrs = col_entity.get("attributes", {})
                parsed = self._parse_qualified_name(attrs.get("qualifiedName", ""))
                yield {
                    "guid": col_entity.get("guid"),
                    "typeName": col_entity.get("typeName"),
                    "name": attrs.get("name"),
                    "qualifiedName": attrs.get("qualifiedName"),
                    "dataType": attrs.get("dataType"),
                    "description": attrs.get("description"),
                    "database_name": parsed.get("database"),
                    "schema_name": parsed.get("schema"),
                    "table_name": parsed.get("table") or parsed.get("view"),
                    "collectionId": col_entity.get("collectionId"),
                    "createdBy": col_entity.get("createdBy"),
                    "updatedBy": col_entity.get("updatedBy"),
                    "createTime": col_entity.get("createTime"),
                    "updateTime": col_entity.get("updateTime"),
                }
            if batch_num % 50 == 0:
                logger.info("Column fetch progress: %d/%d batches", batch_num, total_batches)
            time.sleep(_BULK_DELAY_S)

    # ── extraction methods ─────────────────────────────────────────

    def extract_databases(
        self, out_path: str = "data/expected/purview_databases.jsonl"
    ) -> Dict[str, Any]:
        return self._stream_to_jsonl(self._search(_ENTITY_TYPES["databases"]), out_path)

    def extract_schemas(
        self, out_path: str = "data/expected/purview_schemas.jsonl"
    ) -> Dict[str, Any]:
        return self._stream_to_jsonl(self._search(_ENTITY_TYPES["schemas"]), out_path)

    def extract_tables(
        self, out_path: str = "data/expected/purview_tables.jsonl"
    ) -> Dict[str, Any]:
        return self._stream_to_jsonl(self._search(_ENTITY_TYPES["tables"]), out_path)

    def extract_views(
        self, out_path: str = "data/expected/purview_views.jsonl"
    ) -> Dict[str, Any]:
        return self._stream_to_jsonl(self._search(_ENTITY_TYPES["views"]), out_path)

    def extract_columns(
        self, out_dir: str = "data/expected/columns"
    ) -> Dict[str, Any]:
        # Columns are not indexed for search; they live inside table/view entities.
        # Collect all table + view GUIDs, then bulk-fetch their referredEntities.
        table_guids = [r["id"] for r in self._search(_ENTITY_TYPES["tables"])]
        view_guids  = [r["id"] for r in self._search(_ENTITY_TYPES["views"])]
        all_guids   = table_guids + view_guids
        logger.info(
            "Fetching columns from %d tables + %d views (%d total parent entities)",
            len(table_guids), len(view_guids), len(all_guids),
        )
        return self._stream_to_jsonl_by_schema(
            self._stream_columns_from_guids(all_guids),
            out_dir,
            db_key="database_name",
            schema_key="schema_name",
        )

    @staticmethod
    def _stream_to_jsonl_by_schema(
        rows: Generator[Dict[str, Any], None, None],
        out_dir: str,
        db_key: str,
        schema_key: str,
    ) -> Dict[str, Any]:
        os.makedirs(out_dir, exist_ok=True)
        handles: Dict[str, Any] = {}
        count = 0
        fields: List[str] = []
        sample: List[Dict[str, Any]] = []

        try:
            for row in rows:
                row = dict(row)
                if count == 0:
                    fields = list(row.keys())
                if len(sample) < 5:
                    sample.append(row)

                db = str(row.get(db_key) or "UNKNOWN")
                sc = str(row.get(schema_key) or "UNKNOWN")
                key = f"{db}/{sc}"
                if key not in handles:
                    db_dir = os.path.join(out_dir, db)
                    os.makedirs(db_dir, exist_ok=True)
                    handles[key] = open(os.path.join(db_dir, f"{sc}.jsonl"), "w", encoding="utf-8")

                handles[key].write(json.dumps(row, default=str) + "\n")
                count += 1
        finally:
            for fh in handles.values():
                fh.close()

        return {"count": count, "fields": fields, "sample": sample, "output_dir": out_dir}

    def extract_stored_procedures(
        self, out_path: str = "data/expected/purview_stored_procedures.jsonl"
    ) -> Dict[str, Any]:
        return self._stream_to_jsonl(
            self._search(_ENTITY_TYPES["stored_procedures"]), out_path
        )

    def extract_collection_metadata(
        self, out_path: str = "data/expected/purview_collection_commercial.jsonl"
    ) -> Dict[str, Any]:
        metadata = self.get_collection_metadata()
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(metadata, default=str) + "\n")
        return {
            "count": 1,
            "fields": list(metadata.keys()),
            "sample": [metadata],
            "output": out_path,
        }
