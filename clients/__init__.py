"""
clients
=======
Cloud-source API clients for the purview-snowflake-validator framework.

Exports
-------
SnowflakeClient
    Streams metadata from Snowflake ``ACCOUNT_USAGE`` views and writes
    the results as JSONL files under ``data/snowflake_connector_validation/snowflake_results/``.

PurviewClient
    Fetches metadata from the MS Purview Catalog Search API and Atlas
    Entity API, writing results as JSONL files under ``data/snowflake_connector_validation/purview_results/``.
"""

from .purview_client import PurviewClient
from .snowflake_client import SnowflakeClient

__all__ = ["SnowflakeClient", "PurviewClient"]
