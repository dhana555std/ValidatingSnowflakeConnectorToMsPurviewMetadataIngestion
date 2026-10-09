"""
purview_scan_client
===================
Read-only client for the MS Purview Scanning data-plane API, used by the
Purview scan trigger validation.
 
Endpoints (GET only)
    /scan/datasources                                       data sources
    /scan/datasources/{ds}/scans                            scans
    /scan/datasources/{ds}/scans/{scan}/triggers/default    schedule (404 = none)
    /scan/datasources/{ds}/scans/{scan}/runs                run history
    /account/collections                                    collection names
 
Auth is a service principal (ClientSecretCredential). Every list call follows
``nextLink`` paging, and connection errors / HTTP 429 are retried.
"""
 
import time
from typing import Any, Dict, Iterator, Optional
 
import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import ClientSecretCredential
 
SCAN_API = "2023-09-01"
COLLECTIONS_API = "2019-11-01-preview"
SCOPE = "https://purview.azure.net/.default"
TIMEOUT = 120
MAX_RETRIES = 3
 
 
class PurviewScanClient:
    """Fetches scan configuration, schedules and run history from Purview."""
 
    def __init__(self, account_name: str, tenant_id: str, client_id: str, client_secret: str):
        self.account_name = account_name
        self.endpoint = f"https://{account_name}.purview.azure.com"
        self._cred = ClientSecretCredential(tenant_id, client_id, client_secret)
        self._session = requests.Session()
        self._token: Optional[str] = None
        self._token_exp = 0.0
 
    # ── auth / http ────────────────────────────────────────────────
 
    def _headers(self) -> Dict[str, str]:
        """Bearer token, cached and refreshed 5 minutes before it expires."""
        if not self._token or time.time() > self._token_exp - 300:
            tok = self._cred.get_token(SCOPE)
            self._token, self._token_exp = tok.token, tok.expires_on
        return {"Authorization": f"Bearer {self._token}"}
 
    def get(self, url: str, params: Optional[Dict] = None, allow_404: bool = False) -> Optional[Dict]:
        for attempt in range(MAX_RETRIES):
            try:
                r = self._session.get(url, headers=self._headers(), params=params, timeout=TIMEOUT)
                if allow_404 and r.status_code == 404:
                    return None
                if r.status_code == 429 and attempt < MAX_RETRIES - 1:
                    time.sleep(int(r.headers.get("Retry-After", 2 ** (attempt + 1))))
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout):
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2 ** (attempt + 1))
                self._session = requests.Session()
        return None
 
    def paged(self, url: str) -> Iterator[Dict[str, Any]]:
        """Yield every item of a list endpoint, following nextLink pages."""
        params: Optional[Dict] = {"api-version": SCAN_API}
        while url:
            data = self.get(url, params) or {}
            yield from data.get("value", [])
            url = data.get("nextLink")
            params = None          # nextLink already carries the query string
 
    def check_connection(self) -> None:
        """Log in and make one real call. Raises ConnectionError with a clear reason."""
        try:
            self._headers()
        except ClientAuthenticationError as exc:
            raise ConnectionError(
                "could not log in with the service principal "
                f"(check AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET): {exc}") from exc
        try:
            r = self._session.get(f"{self.endpoint}/scan/datasources", headers=self._headers(),
                                  params={"api-version": SCAN_API}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ConnectionError(f"could not reach {self.endpoint} (check PURVIEW_ACCOUNT_NAME): {exc}") from exc
        if r.status_code in (401, 403):
            raise ConnectionError(f"logged in, but access denied (HTTP {r.status_code}); "
                                  "give the service principal read access on the collection")
        if not r.ok:
            raise ConnectionError(f"Purview returned HTTP {r.status_code}: {r.text[:200]}")
 
    def close(self) -> None:
        self._session.close()
 
    # ── endpoints ──────────────────────────────────────────────────
 
    def collections(self) -> Dict[str, Dict[str, str]]:
        """Collection referenceName -> {name: friendlyName, parent: parent referenceName}."""
        data = self.get(f"{self.endpoint}/account/collections", {"api-version": COLLECTIONS_API}) or {}
        return {c["name"]: {"name": c.get("friendlyName", c["name"]),
                            "parent": (c.get("parentCollection") or {}).get("referenceName", "")}
                for c in data.get("value", [])}
 
    def datasources(self) -> Iterator[Dict[str, Any]]:
        yield from self.paged(f"{self.endpoint}/scan/datasources")
 
    def scans(self, ds: str) -> Iterator[Dict[str, Any]]:
        yield from self.paged(f"{self.endpoint}/scan/datasources/{ds}/scans")
 
    def trigger(self, ds: str, scan: str) -> Optional[Dict[str, Any]]:
        return self.get(f"{self.endpoint}/scan/datasources/{ds}/scans/{scan}/triggers/default",
                        {"api-version": SCAN_API}, allow_404=True)
 
    def runs(self, ds: str, scan: str) -> Iterator[Dict[str, Any]]:
        yield from self.paged(f"{self.endpoint}/scan/datasources/{ds}/scans/{scan}/runs")
 