"""
snowflake_client
================
Extraction client for Snowflake metadata via SNOWFLAKE.ACCOUNT_USAGE views.

All SQL queries mirror query.sql exactly.

Exclusions applied to every query
----------------------------------
* Fixed system databases: SNOWFLAKE, SNOWFLAKE_SAMPLE_DATA, SNOWFLAKE_LEARNING_DB
* Pattern: any database whose name starts with USER$
* Additional: comma-separated list from SNOWFLAKE_EXCLUDE_DATABASES env var

Streaming
---------
Rows are fetched in batches of _BATCH_SIZE via fetchmany() so that even
result sets with 100 000+ rows never fully reside in memory.

Column files
------------
Column extraction writes one JSONL file per <DATABASE>/<SCHEMA> combination
under out_dir/ to keep individual files to a manageable size.
"""

import json
import logging
import os
from typing import Any, Dict, Generator, List, Optional

import snowflake.connector
from snowflake.connector import DictCursor

from utils.paths import (
    SF_DATABASES_PATH, SF_SCHEMAS_PATH, SF_TABLES_PATH,
    SF_VIEWS_PATH, SF_COLUMNS_DIR, SF_STORED_PROCEDURES_PATH,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = 5_000

# ── hardcoded exclusions (always applied, regardless of env var) ──────────────
_SYSTEM_EXCLUDE = frozenset({"SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA", "SNOWFLAKE_LEARNING_DB"})


class SnowflakeClient:
    """Snowflake metadata extractor using ACCOUNT_USAGE views."""

    def __init__(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        role: Optional[str] = None,
        exclude_databases: Optional[List[str]] = None,
    ):
        params: Dict[str, Any] = dict(
            account=account,
            user=user,
            password=password,
            warehouse=warehouse,
        )
        if role:
            params["role"] = role
        self._conn = snowflake.connector.connect(**params)

        # Merge hardcoded system exclusions with caller-supplied ones
        extra = {db.strip().upper() for db in (exclude_databases or []) if db.strip()}
        self._exclude: frozenset = _SYSTEM_EXCLUDE | extra

    # ── internal helpers ──────────────────────────────────────────────────────

    def _stream(self, query: str) -> Generator[Dict[str, Any], None, None]:
        with self._conn.cursor(DictCursor) as cur:
            cur.execute(query)
            while True:
                batch = cur.fetchmany(_BATCH_SIZE)
                if not batch:
                    break
                yield from batch

    def _execute(self, query: str) -> List[Dict[str, Any]]:
        with self._conn.cursor(DictCursor) as cur:
            cur.execute(query)
            return cur.fetchall()

    # ── SQL fragment builders ─────────────────────────────────────────────────

    def _not_in(self, col: str) -> str:
        """Returns a SQL fragment: AND <col> NOT IN ('A','B',...) AND <col> NOT LIKE 'USER$%'"""
        names = ", ".join(f"'{db}'" for db in sorted(self._exclude))
        return f"AND {col} NOT IN ({names})\n  AND {col} NOT LIKE 'USER$%'"

    # ── JSONL writers ─────────────────────────────────────────────────────────

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

    @staticmethod
    def _stream_to_jsonl_by_schema(
        rows: Generator[Dict[str, Any], None, None],
        out_dir: str,
        db_key: str,
        schema_key: str,
    ) -> Dict[str, Any]:
        """Write rows to per-schema JSONL files under out_dir/<DB>/<SCHEMA>.jsonl."""
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

                db  = str(row.get(db_key)     or "UNKNOWN")
                sc  = str(row.get(schema_key) or "UNKNOWN")
                key = f"{db}/{sc}"
                if key not in handles:
                    db_dir = os.path.join(out_dir, db)
                    os.makedirs(db_dir, exist_ok=True)
                    handles[key] = open(
                        os.path.join(db_dir, f"{sc}.jsonl"), "w", encoding="utf-8"
                    )
                handles[key].write(json.dumps(row, default=str) + "\n")
                count += 1
        finally:
            for fh in handles.values():
                fh.close()

        return {"count": count, "fields": fields, "sample": sample, "output_dir": out_dir}

    # ── smoke-test helper ─────────────────────────────────────────────────────

    def get_databases(self) -> List[Dict[str, Any]]:
        """Lightweight smoke-test — returns database names via SHOW DATABASES."""
        rows = self._execute("SHOW DATABASES")
        return [
            r for r in rows
            if r.get("name", "").upper() not in self._exclude
            and not r.get("name", "").upper().startswith("USER$")
        ]

    # ── bulk extraction (matches query.sql exactly) ───────────────────────────

    def extract_all_databases(
        self, out_path: str = SF_DATABASES_PATH
    ) -> Dict[str, Any]:
        """
        -- query.sql section 1: DATABASES
        SELECT DATABASE_NAME, DATABASE_OWNER, COMMENT, CREATED, LAST_ALTERED
        FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES
        WHERE DELETED IS NULL
          AND DATABASE_NAME NOT IN (...)
          AND DATABASE_NAME NOT LIKE 'USER$%'
        ORDER BY DATABASE_NAME;
        """
        return self._stream_to_jsonl(
            self._stream(f"""
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
            """),
            out_path,
        )

    def extract_all_schemas(
        self, out_path: str = SF_SCHEMAS_PATH
    ) -> Dict[str, Any]:
        """
        -- query.sql section 2: SCHEMAS
        SELECT CATALOG_NAME AS DATABASE_NAME, SCHEMA_NAME, ...
        FROM SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA
        WHERE DELETED IS NULL AND SCHEMA_NAME != 'INFORMATION_SCHEMA'
          AND CATALOG_NAME NOT IN (...)
        """
        return self._stream_to_jsonl(
            self._stream(f"""
                SELECT
                    CATALOG_NAME     AS DATABASE_NAME,
                    SCHEMA_NAME,
                    SCHEMA_OWNER,
                    COMMENT,
                    CREATED,
                    LAST_ALTERED
                FROM SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA
                WHERE DELETED IS NULL
                  AND SCHEMA_NAME != 'INFORMATION_SCHEMA'
                  {self._not_in('CATALOG_NAME')}
                ORDER BY CATALOG_NAME, SCHEMA_NAME
            """),
            out_path,
        )

    def extract_all_tables(
        self, out_path: str = SF_TABLES_PATH
    ) -> Dict[str, Any]:
        """
        -- query.sql section 3: TABLES
        SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ...
        FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
        WHERE DELETED IS NULL AND TABLE_TYPE = 'BASE TABLE'
          AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
          AND TABLE_CATALOG NOT IN (...)
        """
        return self._stream_to_jsonl(
            self._stream(f"""
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
                  AND TABLE_TYPE = 'BASE TABLE'
                  AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
                  {self._not_in('TABLE_CATALOG')}
                ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME
            """),
            out_path,
        )

    def extract_all_views(
        self, out_path: str = SF_VIEWS_PATH
    ) -> Dict[str, Any]:
        """
        -- query.sql section 4: VIEWS
        SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION, ...
        FROM SNOWFLAKE.ACCOUNT_USAGE.VIEWS
        WHERE DELETED IS NULL AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
          AND TABLE_CATALOG NOT IN (...)
        """
        return self._stream_to_jsonl(
            self._stream(f"""
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
                  AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
                  {self._not_in('TABLE_CATALOG')}
                ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME
            """),
            out_path,
        )

    def extract_all_columns(
        self, out_dir: str = SF_COLUMNS_DIR
    ) -> Dict[str, Any]:
        """
        -- query.sql section 5: COLUMNS
        SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ...
        FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS
        WHERE DELETED IS NULL AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
          AND TABLE_CATALOG NOT IN (...)
        ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;
        Written as per-schema JSONL files under out_dir/<DB>/<SCHEMA>.jsonl.
        """
        return self._stream_to_jsonl_by_schema(
            self._stream(f"""
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
                  AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
                  {self._not_in('TABLE_CATALOG')}
                ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
            """),
            out_dir,
            db_key="TABLE_CATALOG",
            schema_key="TABLE_SCHEMA",
        )

    def extract_all_stored_procedures(
        self, out_path: str = SF_STORED_PROCEDURES_PATH
    ) -> Dict[str, Any]:
        """
        -- query.sql section 6: STORED PROCEDURES
        SELECT PROCEDURE_CATALOG, PROCEDURE_SCHEMA, PROCEDURE_NAME, ...
        FROM SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES
        WHERE DELETED IS NULL AND PROCEDURE_SCHEMA != 'INFORMATION_SCHEMA'
          AND PROCEDURE_CATALOG NOT IN (...)
        """
        return self._stream_to_jsonl(
            self._stream(f"""
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
                  AND PROCEDURE_SCHEMA != 'INFORMATION_SCHEMA'
                  {self._not_in('PROCEDURE_CATALOG')}
                ORDER BY PROCEDURE_CATALOG, PROCEDURE_SCHEMA, PROCEDURE_NAME
            """),
            out_path,
        )

    def close(self) -> None:
        self._conn.close()
