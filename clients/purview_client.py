"""
purview_client
==============
Extraction client for MS Purview metadata via the Catalog Search API and the
Atlas Entity REST API.
 
Design notes
------------
Searchable entities (databases, schemas, tables, views, stored procedures)
    Retrieved page-by-page using ``POST /catalog/api/search/query`` filtered
    to the target collection's machine reference name, which is resolved
    once and cached for the session.
 
Columns (``snowflake_table_column``, ``snowflake_view_column``)
    Purview does **not** index column entities for search (``isIndexed: false``
    in the data model).  Columns are embedded as ``referredEntities`` inside
    their parent table or view entity.  The client collects all table/view
    GUIDs, then batch-fetches them 20 at a time via the Atlas
    ``/entity/bulk?minExtInfo=true`` endpoint, yielding column entities from
    the ``referredEntities`` map.
 
Collection metadata (Prod AND NonProd)
    ``extract_collection_report()`` resolves the Commercial collection under
    every requested parent (e.g. Prod and NonProd) in the same Purview
    account, reads its metadata from the Collections API and counts its
    Snowflake assets using ``@search.count`` (one cheap call per entity type,
    no paging).  The result is written to a single JSON file that the report
    builder reads.
 
Resilience
    Each HTTP request (GET and POST) is made through a shared
    ``requests.Session`` with a 120-second timeout.  ``ConnectionError``,
    ``ChunkedEncodingError``, and ``ReadTimeout`` are retried up to three
    times using exponential back-off (2 s, 4 s).  A fresh ``Session`` is
    created after each connection-level error.
"""
 
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Generator, Iterable, List, Optional
 
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
 
_RETRYABLE = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ReadTimeout,
)
 
 
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
        Friendly name of the parent collection used for entity extraction
        in this run (case-sensitive), e.g. ``"Prod"`` or ``"NonProd"``.
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
        self._collections_cache: Optional[List[Dict[str, Any]]] = None
        self._ref_cache: Dict[str, str] = {}     # parent friendly name -> reference name
        self._session = requests.Session()
 
    # ── auth + HTTP helpers ────────────────────────────────────────
 
    def _token(self) -> str:
        return self._credential.get_token("https://purview.azure.net/.default").token
 
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
 
    def _request_json(
        self, method: str, url: str, params: Optional[Dict] = None, body: Optional[Dict] = None
    ) -> Any:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params or {},
                    json=body,
                    timeout=_REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                return resp.json()
            except _RETRYABLE as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "%s failed (attempt %d/%d), retrying in %ds: %s",
                    method, attempt + 1, _MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)
                self._session = requests.Session()
 
    def _get_json(self, url: str, params: Optional[Dict] = None) -> Any:
        return self._request_json("GET", url, params=params)
 
    def _post_json(self, url: str, body: Dict, params: Optional[Dict] = None) -> Any:
        return self._request_json("POST", url, params=params, body=body)
 
    # ── collection resolution ──────────────────────────────────────
 
    def _list_collections(self) -> List[Dict[str, Any]]:
        """All collections in the account (fetched once per session)."""
        if self._collections_cache is None:
            url = f"{self._endpoint}/account/collections"
            data = self._get_json(url, params={"api-version": _COLLECTIONS_API_VERSION})
            self._collections_cache = data.get("value", [])
        return self._collections_cache
 
    def _resolve_collection(self, parent_friendly_name: str) -> str:
        """Reference name of the Commercial collection under the given parent.
 
        Both Prod and NonProd contain a collection called "Commercial", so the
        parent's friendly name is what tells them apart.
        """
        if parent_friendly_name in self._ref_cache:
            return self._ref_cache[parent_friendly_name]
 
        collections = self._list_collections()
        col_by_name = {c["name"].lower(): c for c in collections}   # case-insensitive refs
 
        for col in collections:
            if col.get("friendlyName") != self._collection_commercial:
                continue
            parent_ref = col.get("parentCollection", {}).get("referenceName", "")
            if col_by_name.get(parent_ref.lower(), {}).get("friendlyName") == parent_friendly_name:
                self._ref_cache[parent_friendly_name] = col["name"]
                logger.info(
                    "Resolved '%s/%s' collection reference: %s",
                    parent_friendly_name, self._collection_commercial, col["name"],
                )
                return col["name"]
 
        raise ValueError(
            f"Could not find '{self._collection_commercial}' sub-collection "
            f"under '{parent_friendly_name}' in Purview account '{self._account_name}'"
        )
 
    def _resolve_commercial(self) -> str:
        """Commercial collection for this run's environment (used by extraction)."""
        return self._resolve_collection(self._collection_prod)
 
    def _collection_path(self, reference_name: str) -> str:
        """Full friendly path from the root, e.g. 'Vantive US/Prod/Commercial'.
 
        Matches the "Collection path" panel in the Purview UI.  The Collections
        API only returns the parent's *reference* name, so the chain is walked
        through the cached collection list to get friendly names.
        """
        # Reference names are matched case-insensitively: Purview returns the root
        # as "Vantive" but children point to it as "vantive".
        col_by_name = {c["name"].lower(): c for c in self._list_collections()}
        parts: List[str] = []
        current = col_by_name.get(str(reference_name).lower())
        seen = set()
        while current is not None and current["name"].lower() not in seen:   # guard against loops
            seen.add(current["name"].lower())
            parts.append(current.get("friendlyName") or current["name"])
            parent_ref = current.get("parentCollection", {}).get("referenceName")
            current = col_by_name.get(parent_ref.lower()) if parent_ref else None
        return "/".join(reversed(parts))
 
    # ── collection metadata ────────────────────────────────────────
 
    def get_collection_metadata(self, parent_friendly_name: Optional[str] = None) -> Dict[str, Any]:
        """Raw Collections API response for Commercial under the given parent."""
        ref = self._resolve_collection(parent_friendly_name or self._collection_prod)
        url = f"{self._endpoint}/account/collections/{ref}"
        return self._get_json(url, params={"api-version": _COLLECTIONS_API_VERSION})
 
    def get_collection_details(self, environment: str) -> Dict[str, Any]:
        """Report-ready metadata for Commercial under ``environment``."""
        metadata = self.get_collection_metadata(environment)
        system_data = metadata.get("systemData", {})
        return {
            "environment": environment,
            "collection_name": metadata.get("friendlyName"),
            "collection_id": metadata.get("name"),
            "collection_path": self._collection_path(metadata.get("name")),
            "description": metadata.get("description"),
            "provisioning_state": metadata.get("collectionProvisioningState"),
            # Entra object IDs, not display names (the Collections API doesn't return names)
            "created_by": system_data.get("createdBy"),
            "created_by_type": system_data.get("createdByType"),
            "created_at": system_data.get("createdAt"),
            "updated_by": system_data.get("lastModifiedBy"),
            "updated_by_type": system_data.get("lastModifiedByType"),
            "updated_at": system_data.get("lastModifiedAt"),
        }
 
    # ── counting (no paging) ───────────────────────────────────────
 
    def _count(self, collection_ref: str, entity_type: Optional[str] = None) -> int:
        """Number of search hits, read from ``@search.count`` (limit 1 → one call)."""
        filters: List[Dict[str, str]] = [{"collectionId": collection_ref}]
        if entity_type:
            filters.append({"entityType": entity_type})
        url = f"{self._endpoint}/catalog/api/search/query"
        response = self._post_json(
            url,
            {"filter": {"and": filters}, "limit": 1},
            params={"api-version": _SEARCH_API_VERSION},
        )
        if "@search.count" in response:
            return int(response["@search.count"])
 
        # Fallback if the API version ever stops returning @search.count
        logger.warning("@search.count missing — counting %s by paging", entity_type or "all assets")
        return sum(1 for _ in self._search_paged(filters))
 
    def get_collection_asset_counts(self, environment: Optional[str] = None) -> Dict[str, int]:
        ref = self._resolve_collection(environment or self._collection_prod)
        counts = {key: self._count(ref, etype) for key, etype in _ENTITY_TYPES.items()}
        counts["total_snowflake_assets"] = sum(counts.values())
        # Every asset type in the collection — this is what the Purview UI tile shows
        counts["total_all_assets"] = self._count(ref)
        return counts
 
    def extract_collection_report(
        self,
        environments: Iterable[str] = ("Prod", "NonProd"),
        out_path: str = "data/expected/purview_collection_commercial.json",
    ) -> Dict[str, Any]:
        """Metadata + asset counts for Commercial under each environment, in one file.
 
        A failure for one environment (e.g. no access to NonProd) is recorded in
        the output instead of stopping the whole run.
        """
        results: List[Dict[str, Any]] = []
        for env in environments:
            try:
                details = self.get_collection_details(env)
                details["asset_counts"] = self.get_collection_asset_counts(env)
                if details["asset_counts"]["total_all_assets"] == 0:
                    details["warning"] = (
                        "0 assets found — check that the service principal has "
                        f"Data Reader on {env}/{self._collection_commercial}"
                    )
            except Exception as exc:  # keep going; report shows the error
                logger.error("Collection metadata for %s failed: %s", env, exc)
                details = {"environment": env, "error": str(exc)}
            details["validated_in_this_run"] = (env == self._collection_prod)
            results.append(details)
 
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "purview_account": self._account_name,
            "validated_environment": self._collection_prod,
            "collections": results,
        }
        dir_name = os.path.dirname(out_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, default=str)
 
        return {
            "count": len(results),
            "fields": list(payload.keys()),
            "sample": results,
            "output": out_path,
        }
 
    # ── streaming search ───────────────────────────────────────────
 
    def _search_paged(self, filters: List[Dict[str, str]]) -> Generator[Dict[str, Any], None, None]:
        url = f"{self._endpoint}/catalog/api/search/query"
        params = {"api-version": _SEARCH_API_VERSION}
        offset = 0
        while True:
            body = {"filter": {"and": filters}, "limit": _BATCH_SIZE, "offset": offset}
            response = self._post_json(url, body, params=params)
            batch = response.get("value", [])
            yield from batch
            if len(batch) < _BATCH_SIZE:
                break
            offset += _BATCH_SIZE
 
    def _search(self, entity_type: str) -> Generator[Dict[str, Any], None, None]:
        yield from self._search_paged(
            [{"entityType": entity_type}, {"collectionId": self._resolve_commercial()}]
        )
 
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