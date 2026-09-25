-- Databases excluded from all queries.
-- Fixed system databases are always excluded; USER$% covers all personal databases.
-- Add additional databases to excluded_dbs if needed for manual ad-hoc runs.
SET excluded_dbs = ('SNOWFLAKE', 'SNOWFLAKE_SAMPLE_DATA', 'SNOWFLAKE_LEARNING_DB');
-- Note: In the Python client, AND <col> NOT LIKE 'USER$%' is also applied to every query.


-- ─────────────────────────────────────────────────────────────
-- 0. COUNTS SUMMARY
-- ─────────────────────────────────────────────────────────────
SELECT
    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES
     WHERE DELETED IS NULL
       AND DATABASE_NAME NOT IN ($excluded_dbs))                                     AS DATABASES,

    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA
     WHERE DELETED IS NULL AND SCHEMA_NAME != 'INFORMATION_SCHEMA'
       AND CATALOG_NAME NOT IN ($excluded_dbs))                                      AS SCHEMAS,

    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
     WHERE DELETED IS NULL AND TABLE_TYPE = 'BASE TABLE'
       AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
       AND TABLE_CATALOG NOT IN ($excluded_dbs))                                     AS TABLES,

    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.VIEWS
     WHERE DELETED IS NULL AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
       AND TABLE_CATALOG NOT IN ($excluded_dbs))                                     AS VIEWS,

    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS
     WHERE DELETED IS NULL AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
       AND TABLE_CATALOG NOT IN ($excluded_dbs))                                     AS COLUMNS,

    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES
     WHERE DELETED IS NULL AND PROCEDURE_SCHEMA != 'INFORMATION_SCHEMA'
       AND PROCEDURE_CATALOG NOT IN ($excluded_dbs))                                 AS STORED_PROCEDURES;


-- ─────────────────────────────────────────────────────────────
-- 0b. TABLE COUNT BY DATABASE
-- ─────────────────────────────────────────────────────────────
SELECT
    TABLE_CATALOG   AS DATABASE_NAME,
    COUNT(*)        AS TABLE_COUNT
FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
WHERE DELETED IS NULL
  AND TABLE_TYPE = 'BASE TABLE'
  AND TABLE_SCHEMA != 'INFORMATION_SCHEMA'
  AND TABLE_CATALOG NOT IN ($excluded_dbs)
GROUP BY TABLE_CATALOG
ORDER BY TABLE_CATALOG;


-- ─────────────────────────────────────────────────────────────
-- 1. DATABASES
-- ─────────────────────────────────────────────────────────────
SELECT
    DATABASE_NAME,
    DATABASE_OWNER,
    COMMENT,
    CREATED,
    LAST_ALTERED
FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES
WHERE DELETED IS NULL
  AND DATABASE_NAME NOT IN ($excluded_dbs)
ORDER BY DATABASE_NAME;


-- ─────────────────────────────────────────────────────────────
-- 2. SCHEMAS
-- ─────────────────────────────────────────────────────────────
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
  AND CATALOG_NAME NOT IN ($excluded_dbs)
ORDER BY CATALOG_NAME, SCHEMA_NAME;


-- ─────────────────────────────────────────────────────────────
-- 3. TABLES
-- ─────────────────────────────────────────────────────────────
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
  AND TABLE_CATALOG NOT IN ($excluded_dbs)
ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME;


-- ─────────────────────────────────────────────────────────────
-- 4. VIEWS
-- ─────────────────────────────────────────────────────────────
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
  AND TABLE_CATALOG NOT IN ($excluded_dbs)
ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME;


-- ─────────────────────────────────────────────────────────────
-- 5. COLUMNS
-- ─────────────────────────────────────────────────────────────
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
  AND TABLE_CATALOG NOT IN ($excluded_dbs)
ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;


-- ─────────────────────────────────────────────────────────────
-- 6. STORED PROCEDURES
-- ─────────────────────────────────────────────────────────────
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
  AND PROCEDURE_CATALOG NOT IN ($excluded_dbs)
ORDER BY PROCEDURE_CATALOG, PROCEDURE_SCHEMA, PROCEDURE_NAME;
