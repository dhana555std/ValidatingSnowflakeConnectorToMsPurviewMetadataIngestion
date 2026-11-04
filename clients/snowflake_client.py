"""
snowflake_client
================
 
Extraction client for Snowflake metadata via SNOWFLAKE.ACCOUNT_USAGE views.
 
All SQL queries mirror query.sql exactly.
 
Exclusions applied to every query
---------------------------------
* Fixed system databases:
    SNOWFLAKE
    SNOWFLAKE_SAMPLE_DATA
    SNOWFLAKE_LEARNING_DB
 
* Databases starting with USER$
* Additional exclusions supplied externally
 
Streaming
---------
Rows are fetched in batches using fetchmany().
 
Column files
------------
Column extraction writes one JSONL file per
DATABASE/SCHEMA combination.
 
Connection notes
----------------
* ``account`` must be the short account identifier
  (e.g. ``vantive-eu-nonprod`` or ``orgname-acctname``), NOT the full URL.
* For PrivateLink, pass the full ``...privatelink.snowflakecomputing.com``
  hostname via ``host``.
"""
 
import json
import logging
import os
 
from typing import Any
from typing import Dict
from typing import Generator
from typing import List
from typing import Optional
 
import snowflake.connector
from snowflake.connector import DictCursor
 
from cryptography.hazmat.primitives import serialization
 
logger = logging.getLogger(__name__)
 
_BATCH_SIZE = 5000
 
_SYSTEM_EXCLUDE = frozenset(
    {
        "SNOWFLAKE",
        "SNOWFLAKE_SAMPLE_DATA",
        "SNOWFLAKE_LEARNING_DB",
    }
)
 
 
class SnowflakeClient:
    """
    Snowflake metadata extractor using ACCOUNT_USAGE views.
    """
 
    @staticmethod
    def _load_private_key(
        private_key_path: str,
        passphrase: Optional[str] = None,
    ) -> bytes:
 
        with open(private_key_path, "rb") as key_file:
            private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=passphrase.encode() if passphrase else None,
            )
 
        return private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
 
    def __init__(
        self,
        account: str,
        user: str,
        private_key_path: str,
        private_key_passphrase: Optional[str] = None,
        warehouse: Optional[str] = None,
        database: Optional[str] = None,
        role: Optional[str] = None,
        exclude_databases: Optional[List[str]] = None,
        host: Optional[str] = None,
    ):
 
        private_key = self._load_private_key(
            private_key_path,
            private_key_passphrase,
        )
 
        params: Dict[str, Any] = {
            "account": account,
            "user": user,
            "private_key": private_key,
            "authenticator": "SNOWFLAKE_JWT",
        }
 
        # PrivateLink / custom host (kept separate from the account identifier)
        if host:
            params["host"] = host
 
        if warehouse:
            params["warehouse"] = warehouse
 
        if database:
            params["database"] = database
 
        if role:
            params["role"] = role
 
        logger.info(
            "Connecting to Snowflake: account=%s user=%s host=%s role=%s",
            account,
            user,
            host or "(default)",
            role or "(default)",
        )
 
        self._conn = snowflake.connector.connect(**params)
 
        extra = {
            db.strip().upper()
            for db in (exclude_databases or [])
            if db.strip()
        }
 
        self._exclude: frozenset = _SYSTEM_EXCLUDE | extra
 
    ###########################################################################
    # Helpers
    ###########################################################################
 
    def _stream(
        self,
        query: str,
    ) -> Generator[Dict[str, Any], None, None]:
 
        with self._conn.cursor(DictCursor) as cur:
 
            cur.execute(query)
 
            while True:
 
                batch = cur.fetchmany(_BATCH_SIZE)
 
                if not batch:
                    break
 
                yield from batch
 
    def _execute(
        self,
        query: str,
    ) -> List[Dict[str, Any]]:
 
        with self._conn.cursor(DictCursor) as cur:
 
            cur.execute(query)
 
            return cur.fetchall()
 
    def _not_in(self, col: str) -> str:
 
        names = ", ".join(
            f"'{db}'"
            for db in sorted(self._exclude)
        )
 
        return (
            f"AND {col} NOT IN ({names})\n"
            f"AND {col} NOT LIKE 'USER$%'"
        )
 
    ###########################################################################
    # JSONL Writers
    ###########################################################################
 
    @staticmethod
    def _stream_to_jsonl(
        rows: Generator[Dict[str, Any], None, None],
        out_path: str,
    ) -> Dict[str, Any]:
 
        directory = os.path.dirname(out_path)
 
        if directory:
            os.makedirs(directory, exist_ok=True)
 
        count = 0
        fields = []
        sample = []
 
        with open(
            out_path,
            "w",
            encoding="utf-8",
        ) as f:
 
            for row in rows:
 
                row = dict(row)
 
                if count == 0:
                    fields = list(row.keys())
 
                if len(sample) < 5:
                    sample.append(row)
 
                f.write(
                    json.dumps(row, default=str) + "\n"
                )
 
                count += 1
 
        return {
            "count": count,
            "fields": fields,
            "sample": sample,
            "output": out_path,
        }
 
    @staticmethod
    def _stream_to_jsonl_by_schema(
        rows: Generator[Dict[str, Any], None, None],
        out_dir: str,
        db_key: str,
        schema_key: str,
    ) -> Dict[str, Any]:
 
        os.makedirs(out_dir, exist_ok=True)
 
        handles = {}
 
        count = 0
        fields = []
        sample = []
 
        try:
 
            for row in rows:
 
                row = dict(row)
 
                if count == 0:
                    fields = list(row.keys())
 
                if len(sample) < 5:
                    sample.append(row)
 
                db = str(
                    row.get(db_key)
                    or "UNKNOWN"
                )
 
                schema = str(
                    row.get(schema_key)
                    or "UNKNOWN"
                )
 
                key = f"{db}/{schema}"
 
                if key not in handles:
 
                    db_dir = os.path.join(
                        out_dir,
                        db,
                    )
 
                    os.makedirs(
                        db_dir,
                        exist_ok=True,
                    )
 
                    handles[key] = open(
                        os.path.join(
                            db_dir,
                            f"{schema}.jsonl",
                        ),
                        "w",
                        encoding="utf-8",
                    )
 
                handles[key].write(
                    json.dumps(
                        row,
                        default=str,
                    )
                    + "\n"
                )
 
                count += 1
 
        finally:
 
            for fh in handles.values():
                fh.close()
 
        return {
            "count": count,
            "fields": fields,
            "sample": sample,
            "output_dir": out_dir,
        }
 
    ###########################################################################
    # Smoke Test
    ###########################################################################
 
    def get_databases(self):
 
        rows = self._execute(
            "SHOW DATABASES"
        )
 
        return [
            row
            for row in rows
            if row.get("name", "").upper()
            not in self._exclude
            and not row.get("name", "")
            .upper()
            .startswith("USER$")
        ]
 
    ###########################################################################
    # Extraction Methods
    ###########################################################################
 
    def extract_all_databases(
        self,
        out_path="data/actual/databases.jsonl",
    ):
        return self._stream_to_jsonl(
            self._stream(
                f"""
                SELECT
                    DATABASE_NAME,
                    DATABASE_OWNER,
                    COMMENT,
                    CREATED,
                    LAST_ALTERED
                FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES
                WHERE DELETED IS NULL
                {self._not_in('DATABASE_NAME')}
                ORDER BY DATABASE_NAME
                """
            ),
            out_path,
        )
 
    def extract_all_schemas(
        self,
        out_path="data/actual/schemas.jsonl",
    ):
        return self._stream_to_jsonl(
            self._stream(
                f"""
                SELECT
                    CATALOG_NAME AS DATABASE_NAME,
                    SCHEMA_NAME,
                    SCHEMA_OWNER,
                    COMMENT,
                    CREATED,
                    LAST_ALTERED
                FROM SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA
                WHERE DELETED IS NULL
                AND SCHEMA_NAME != 'INFORMATION_SCHEMA'
                {self._not_in('CATALOG_NAME')}
                ORDER BY
                    CATALOG_NAME,
                    SCHEMA_NAME
                """
            ),
            out_path,
        )
 
    def extract_all_tables(
        self,
        out_path="data/actual/tables.jsonl",
    ):
        return self._stream_to_jsonl(
            self._stream(
                f"""
                SELECT
                    TABLE_CATALOG,
                    TABLE_SCHEMA,
                    TABLE_NAME,
                    TABLE_TYPE,
                    ROW_COUNT,
                    BYTES,
                    COMMENT,
                    CREATED,
                    LAST_ALTERED
                FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
                WHERE DELETED IS NULL
                AND TABLE_TYPE='BASE TABLE'
                AND TABLE_SCHEMA!='INFORMATION_SCHEMA'
                {self._not_in('TABLE_CATALOG')}
                ORDER BY
                    TABLE_CATALOG,
                    TABLE_SCHEMA,
                    TABLE_NAME
                """
            ),
            out_path,
        )
 
    def extract_all_views(
        self,
        out_path="data/actual/views.jsonl",
    ):
        return self._stream_to_jsonl(
            self._stream(
                f"""
                SELECT
                    TABLE_CATALOG,
                    TABLE_SCHEMA,
                    TABLE_NAME,
                    VIEW_DEFINITION,
                    COMMENT,
                    CREATED,
                    LAST_ALTERED
                FROM SNOWFLAKE.ACCOUNT_USAGE.VIEWS
                WHERE DELETED IS NULL
                AND TABLE_SCHEMA!='INFORMATION_SCHEMA'
                {self._not_in('TABLE_CATALOG')}
                ORDER BY
                    TABLE_CATALOG,
                    TABLE_SCHEMA,
                    TABLE_NAME
                """
            ),
            out_path,
        )
 
    def extract_all_columns(
        self,
        out_dir="data/actual/columns",
    ):
        return self._stream_to_jsonl_by_schema(
            self._stream(
                f"""
                SELECT
                    TABLE_CATALOG,
                    TABLE_SCHEMA,
                    TABLE_NAME,
                    COLUMN_NAME,
                    ORDINAL_POSITION,
                    DATA_TYPE,
                    CHARACTER_MAXIMUM_LENGTH,
                    NUMERIC_PRECISION,
                    NUMERIC_SCALE,
                    IS_NULLABLE,
                    COLUMN_DEFAULT,
                    COMMENT
                FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS
                WHERE DELETED IS NULL
                AND TABLE_SCHEMA!='INFORMATION_SCHEMA'
                {self._not_in('TABLE_CATALOG')}
                ORDER BY
                    TABLE_CATALOG,
                    TABLE_SCHEMA,
                    TABLE_NAME,
                    ORDINAL_POSITION
                """
            ),
            out_dir,
            db_key="TABLE_CATALOG",
            schema_key="TABLE_SCHEMA",
        )
 
    def extract_all_stored_procedures(
        self,
        out_path="data/actual/stored_procedures.jsonl",
    ):
        return self._stream_to_jsonl(
            self._stream(
                f"""
                SELECT
                    PROCEDURE_CATALOG,
                    PROCEDURE_SCHEMA,
                    PROCEDURE_NAME,
                    PROCEDURE_LANGUAGE,
                    ARGUMENT_SIGNATURE,
                    PROCEDURE_DEFINITION,
                    COMMENT,
                    CREATED,
                    LAST_ALTERED
                FROM SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES
                WHERE DELETED IS NULL
                AND PROCEDURE_SCHEMA!='INFORMATION_SCHEMA'
                {self._not_in('PROCEDURE_CATALOG')}
                ORDER BY
                    PROCEDURE_CATALOG,
                    PROCEDURE_SCHEMA,
                    PROCEDURE_NAME
                """
            ),
            out_path,
        )
 
    ###########################################################################
    # Cleanup
    ###########################################################################
 
    def close(self):
        self._conn.close()