"""
Phase 1: Search_app registry.

This module creates and manages registry.sqlite without modifying
any existing source SQLite databases or the existing unified search index.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable
import re



def get_base_dir() -> Path:
    env_dir = os.environ.get("SEARCH_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    guessed = Path(__file__).resolve().parent.parent / "sqlite"
    if guessed.is_dir():
        return guessed

    return Path("./sqlite").resolve()


BASE = get_base_dir()

UPLOAD_DIR = BASE / "uploads"

REGISTRY_PATH = Path(
    os.environ.get("SEARCH_REGISTRY_DB", str(BASE / "registry.sqlite"))
).expanduser().resolve()

UNIFIED_INDEX_PATH = Path(
    os.environ.get(
        "SEARCH_INDEX_DB",
        str(BASE / "unified_search_index.sqlite"),
    )
).expanduser().resolve()


EXISTING_SOURCES = {
    "ghmc": {
        "label": "GHMC",
        "db": "ghmc",
        "table": "ghmc",
    },
    "aarogyasri": {
        "label": "Aarogyasri (XLSX)",
        "db": "excel",
        "table": "aarogyasri",
    },
    "mdb": {
        "label": "Aarogyasri (MDB)",
        "db": "mdb",
        "table": "mdb_data",
    },
    "teachers": {
        "label": "Teachers / Schools",
        "db": "excel",
        "table": "teachers",
    },
    "pdf": {
        "label": "PDF documents",
        "db": "excel",
        "table": "pdf_records",
    },
    "zdata": {
        "label": "Zdata",
        "db": "zdata",
        "table": "zdata",
    },
}

DB_FILES = {
    "ghmc": BASE / "ghmc.sqlite",
    "excel": BASE / "excel_data.sqlite",
    "mdb": BASE / "mdb_data.sqlite",
    "zdata": BASE / "zdata.sqlite",
}


def now() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)

    c = sqlite3.connect(REGISTRY_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_registry(c: sqlite3.Connection | None = None) -> None:
    own = c is None
    c = c or connect()

    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            source_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            db_key TEXT NOT NULL,
            db_path TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'registered',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_tables (
            source_key TEXT NOT NULL,
            table_name TEXT NOT NULL,
            searchable INTEGER NOT NULL DEFAULT 1,
            row_count INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (source_key, table_name),
            FOREIGN KEY (source_key)
                REFERENCES sources(source_key)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS source_columns (
            source_key TEXT NOT NULL,
            table_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            column_type TEXT,
            ordinal INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (source_key, table_name, column_name),
            FOREIGN KEY (source_key, table_name)
                REFERENCES source_tables(source_key, table_name)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS field_mappings (
            source_key TEXT NOT NULL,
            table_name TEXT NOT NULL,
            logical_field TEXT NOT NULL,
            column_name TEXT NOT NULL,
            confidence REAL,
            confirmed INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (
                source_key,
                table_name,
                logical_field,
                column_name
            ),
            FOREIGN KEY (source_key, table_name)
                REFERENCES source_tables(source_key, table_name)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS indexes (
            index_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL,
            table_name TEXT NOT NULL,
            index_type TEXT NOT NULL,
            index_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            index_size_bytes INTEGER,
            indexed_rows INTEGER,
            started_at INTEGER,
            completed_at INTEGER,
            error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(source_key, table_name, index_type),
            FOREIGN KEY (source_key, table_name)
                REFERENCES source_tables(source_key, table_name)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sources_enabled
            ON sources(enabled, status);

        CREATE INDEX IF NOT EXISTS idx_tables_searchable
            ON source_tables(searchable);

        CREATE INDEX IF NOT EXISTS idx_mappings_logical
            ON field_mappings(logical_field);
        """
    )

    c.commit()

    if own:
        c.close()


def table_info(db_path: Path, table_name: str) -> tuple[list[dict], int]:
    if not db_path.exists():
        raise FileNotFoundError(str(db_path))

    c = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=10,
    )
    try:
        c.execute("PRAGMA query_only=ON")

        exists = c.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type IN ('table', 'view') AND name=?
            """,
            (table_name,),
        ).fetchone()

        if not exists:
            raise ValueError(
                f"Table {table_name!r} does not exist in {db_path}"
            )

        rows = c.execute(
            f'PRAGMA table_info("{table_name.replace(chr(34), chr(34) * 2)}")'
        ).fetchall()

        count = c.execute(
            f'SELECT COUNT(*) FROM "{table_name.replace(chr(34), chr(34) * 2)}"'
        ).fetchone()[0]

        return [
            {
                "name": r[1],
                "type": r[2],
                "ordinal": r[0],
            }
            for r in rows
        ], int(count)
    finally:
        c.close()

def inspect_sqlite(db_path: Path) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(str(db_path))

    c = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=10,
    )

    try:
        c.execute("PRAGMA query_only=ON")

        tables = c.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        result = []

        for row in tables:
            table_name = row[0]

            columns = c.execute(
                f'PRAGMA table_info("{table_name.replace(chr(34), chr(34) * 2)}")'
            ).fetchall()

            count = c.execute(
                f'SELECT COUNT(*) FROM "{table_name.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0]

            result.append({
                "name": table_name,
                "row_count": int(count),
                "columns": [
                    {
                        "name": col[1],
                        "type": col[2],
                        "ordinal": col[0],
                    }
                    for col in columns
                ],
            })

        return {
            "database": str(db_path),
            "tables": result,
        }

    finally:
        c.close()

def register_source(
    source_key: str,
    label: str,
    db_key: str,
    db_path: Path,
    table_name: str,
    *,
    enabled: bool = True,
    status: str = "ready",
    index_type: str = "unified",
    index_path: Path = UNIFIED_INDEX_PATH,
) -> None:
    columns, row_count = table_info(db_path, table_name)

    ts = now()

    c = connect()
    try:
        init_registry(c)

        c.execute(
            """
            INSERT INTO sources(
                source_key, label, db_key, db_path,
                enabled, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                label=excluded.label,
                db_key=excluded.db_key,
                db_path=excluded.db_path,
                enabled=excluded.enabled,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                source_key,
                label,
                db_key,
                str(db_path),
                int(enabled),
                status,
                ts,
                ts,
            ),
        )

        c.execute(
            """
            INSERT INTO source_tables(
                source_key, table_name, searchable,
                row_count, created_at, updated_at
            )
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(source_key, table_name) DO UPDATE SET
                row_count=excluded.row_count,
                updated_at=excluded.updated_at
            """,
            (source_key, table_name, row_count, ts, ts),
        )

        for col in columns:
            c.execute(
                """
                INSERT INTO source_columns(
                    source_key, table_name, column_name,
                    column_type, ordinal, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    source_key, table_name, column_name
                ) DO UPDATE SET
                    column_type=excluded.column_type,
                    ordinal=excluded.ordinal,
                    updated_at=excluded.updated_at
                """,
                (
                    source_key,
                    table_name,
                    col["name"],
                    col["type"],
                    col["ordinal"],
                    ts,
                    ts,
                ),
            )

        c.execute(
            """
            INSERT INTO indexes(
                source_key, table_name, index_type, index_path,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'existing', ?, ?)
            ON CONFLICT(source_key, table_name, index_type)
            DO UPDATE SET
                index_path=excluded.index_path,
                updated_at=excluded.updated_at
            """,
            (
                source_key,
                table_name,
                index_type,
                str(index_path),
                ts,
                ts,
            ),
        )

        c.commit()
    finally:
        c.close()

def register_uploaded_source(db_path, label=None):
    db_path = Path(db_path).resolve()

    if not db_path.exists():
        raise ValueError(f"database file not found: {db_path}")

    inspection = inspect_sqlite(db_path)
    tables = inspection["tables"]

    if not tables:
        raise ValueError("database contains no user tables")

    conn = connect()
    init_registry(conn)

    existing = conn.execute(
        """
        SELECT source_key
        FROM sources
        WHERE db_path=?
        """,
        (str(db_path),)
    ).fetchone()

    if existing:
        conn.close()
        raise ValueError(
            f"database already registered as: {existing['source_key']}"
        )

    stem = re.sub(r"[^a-z0-9]+", "_", db_path.stem.lower()).strip("_")

    base_key = f"upload_{stem}"
    source_key = base_key
    n = 2

    while conn.execute(
        "SELECT 1 FROM sources WHERE source_key=?",
        (source_key,)
    ).fetchone():
        source_key = f"{base_key}_{n}"
        n += 1

    label = label or db_path.stem

    ts = now()

    conn.execute(
        """
        INSERT INTO sources (
            source_key,
            label,
            db_key,
            db_path,
            enabled,
            status,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, 1, 'ready_for_mapping', ?, ?)
        """,
        (
            source_key,
            label,
            source_key,
            str(db_path),
            ts,
            ts
        )
    )

    for table in tables:
        table_name = table["name"]
        row_count = table["row_count"]

        conn.execute(
            """
            INSERT INTO source_tables (
                source_key,
                table_name,
                searchable,
                row_count,
                created_at,
                updated_at
            )
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                source_key,
                table_name,
                row_count,
                ts,
                ts
            )
        )

        for col in table["columns"]:
            conn.execute(
                """
                INSERT INTO source_columns (
                    source_key,
                    table_name,
                    column_name,
                    column_type,
                    ordinal,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    table_name,
                    col["name"],
                    col["type"],
                    col["ordinal"],
                    ts,
                    ts
                )
            )

    conn.commit()
    conn.close()

    return get_source(source_key)


def register_existing_sources() -> None:
    """
    Register the six logical sources already used by Search_app.

    This only reads the existing source databases and writes registry.sqlite.
    It does not rebuild or modify unified_search_index.sqlite.
    """
    for source_key, cfg in EXISTING_SOURCES.items():
        db_path = DB_FILES[cfg["db"]]

        if not db_path.exists():
            print(f"SKIP {source_key}: missing {db_path}")
            continue

        register_source(
            source_key=source_key,
            label=cfg["label"],
            db_key=cfg["db"],
            db_path=db_path,
            table_name=cfg["table"],
        )

        print(
            f"REGISTERED {source_key}: "
            f"{cfg['label']} -> {db_path.name}:{cfg['table']}"
        )


def list_sources() -> list[dict]:
    c = connect()
    try:
        init_registry(c)
        rows = c.execute(
            """
            SELECT
                s.source_key,
                s.label,
                s.db_key,
                s.db_path,
                s.enabled,
                s.status,
                t.table_name,
                t.row_count,
                i.index_type,
                i.index_path,
                i.status AS index_status
            FROM sources AS s
            LEFT JOIN source_tables AS t
                ON t.source_key = s.source_key
            LEFT JOIN indexes AS i
                ON i.source_key = t.source_key
               AND i.table_name = t.table_name
            ORDER BY s.source_key, t.table_name
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()

def get_source(source_key: str) -> dict | None:
    c = connect()
    try:
        init_registry(c)

        src = c.execute(
            """
            SELECT
                source_key,
                label,
                db_key,
                db_path,
                enabled,
                status,
                created_at,
                updated_at
            FROM sources
            WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()

        if src is None:
            return None

        tables = []

        table_rows = c.execute(
            """
            SELECT
                table_name,
                searchable,
                row_count
            FROM source_tables
            WHERE source_key = ?
            ORDER BY table_name
            """,
            (source_key,),
        ).fetchall()

        for table in table_rows:
            table_name = table["table_name"]

            columns = c.execute(
                """
                SELECT
                    column_name,
                    column_type,
                    ordinal
                FROM source_columns
                WHERE source_key = ?
                  AND table_name = ?
                ORDER BY ordinal
                """,
                (source_key, table_name),
            ).fetchall()

            indexes = c.execute(
                """
                SELECT
                    index_type,
                    index_path,
                    status,
                    index_size_bytes,
                    indexed_rows,
                    started_at,
                    completed_at,
                    error
                FROM indexes
                WHERE source_key = ?
                  AND table_name = ?
                ORDER BY index_type
                """,
                (source_key, table_name),
            ).fetchall()

            tables.append({
                "name": table_name,
                "searchable": bool(table["searchable"]),
                "row_count": table["row_count"],
                "columns": [
                    {
                        "name": col["column_name"],
                        "type": col["column_type"],
                        "ordinal": col["ordinal"],
                    }
                    for col in columns
                ],
                "indexes": [
                    {
                        "type": idx["index_type"],
                        "path": idx["index_path"],
                        "status": idx["status"],
                        "size_bytes": idx["index_size_bytes"],
                        "indexed_rows": idx["indexed_rows"],
                        "started_at": idx["started_at"],
                        "completed_at": idx["completed_at"],
                        "error": idx["error"],
                    }
                    for idx in indexes
                ],
            })

        return {
            "source_key": src["source_key"],
            "label": src["label"],
            "db_key": src["db_key"],
            "database": src["db_path"],
            "enabled": bool(src["enabled"]),
            "status": src["status"],
            "created_at": src["created_at"],
            "updated_at": src["updated_at"],
            "tables": tables,
        }

    finally:
        c.close()

def get_mappings(source_key: str, table_name: str | None = None) -> list[dict]:
    c = connect()
    try:
        init_registry(c)

        if table_name:
            rows = c.execute(
                """
                SELECT
                    source_key,
                    table_name,
                    logical_field,
                    column_name,
                    confidence,
                    confirmed,
                    created_at,
                    updated_at
                FROM field_mappings
                WHERE source_key = ?
                  AND table_name = ?
                ORDER BY table_name, logical_field, column_name
                """,
                (source_key, table_name),
            ).fetchall()
        else:
            rows = c.execute(
                """
                SELECT
                    source_key,
                    table_name,
                    logical_field,
                    column_name,
                    confidence,
                    confirmed,
                    created_at,
                    updated_at
                FROM field_mappings
                WHERE source_key = ?
                ORDER BY table_name, logical_field, column_name
                """,
                (source_key,),
            ).fetchall()

        return [
            {
                "source_key": row["source_key"],
                "table_name": row["table_name"],
                "logical_field": row["logical_field"],
                "column_name": row["column_name"],
                "confidence": row["confidence"],
                "confirmed": bool(row["confirmed"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    finally:
        c.close()

def delete_source(source_key):
    if source_key in EXISTING_SOURCES:
        raise ValueError(f"existing source cannot be deleted: {source_key}")

    conn = connect()

    row = conn.execute(
        "SELECT db_path FROM sources WHERE source_key=?",
        (source_key,)
    ).fetchone()

    if not row:
        conn.close()
        raise ValueError(f"source not found: {source_key}")

    db_path = row["db_path"]

    indexes = conn.execute(
        """
        SELECT index_type, index_path
        FROM indexes
        WHERE source_key=?
        """,
        (source_key,)
    ).fetchall()

    conn.close()

    # Clean unified index first.
    from index_adapter import delete_source_index

    delete_source_index(source_key)

    # Only mutate registry after index cleanup succeeds.
    conn = connect()

    conn.execute(
        "DELETE FROM source_columns WHERE source_key=?",
        (source_key,)
    )

    conn.execute(
        "DELETE FROM source_tables WHERE source_key=?",
        (source_key,)
    )

    conn.execute(
        "DELETE FROM field_mappings WHERE source_key=?",
        (source_key,)
    )

    conn.execute(
        "DELETE FROM indexes WHERE source_key=?",
        (source_key,)
    )

    conn.execute(
        "DELETE FROM sources WHERE source_key=?",
        (source_key,)
    )

    conn.commit()
    conn.close()

    # Delete uploaded DB only after registry + index cleanup succeeded.
    if db_path:
        path = Path(db_path)
        if path.exists():
            path.unlink()

    # Delete dedicated indexes only.
    # NEVER delete the unified index.
    for idx in indexes:
        if idx["index_type"] == "unified":
            continue

        if idx["index_path"]:
            path = Path(idx["index_path"])
            if path.exists():
                path.unlink()

    return {
        "source_key": source_key,
        "deleted": True,
    }

LOGICAL_FIELD_ALIASES = {
    "phone": [
        "phone",
        "mobile",
        "mobile_no",
        "mobile_number",
        "phone_no",
        "phone_number",
        "contact",
        "contact_no",
        "contact_number",
        "telephone",
        "tel",
    ],
    "id": [
        "id",
        "id_no",
        "id_number",
        "identifier",
        "uid",
        "unique_id",
        "card_no",
        "card_number",
        "idcard",
        "id_card",
        "aadhaar",
        "aadhaar_no",
        "aadhar",
        "aadhar_no",
    ],
    "address": [
        "address",
        "addr",
        "address1",
        "address2",
        "address_line1",
        "address_line2",
        "full_address",
        "residential_address",
        "residence",
        "location",
        "locality",
        "street",
    ],
    "city": [
        "city",
        "town",
        "village",
        "district",
        "municipality",
        "mandal",
        "area",
    ],
    "pincode": [
        "pincode",
        "pin",
        "pin_code",
        "postal_code",
        "postcode",
        "zip",
        "zip_code",
    ],
    "father_name": [
        "father",
        "father_name",
        "fathername",
        "father_nm",
        "fathers_name",
        "parent_name",
        "guardian",
        "guardian_name",
    ],
}


def normalize_column_name(name: str) -> str:
    name = str(name).strip().lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def mapping_score(column_name: str, logical_field: str) -> float:
    col = normalize_column_name(column_name)
    aliases = LOGICAL_FIELD_ALIASES.get(logical_field, [])

    if col in aliases:
        return 1.0

    for alias in aliases:
        if col == alias.replace("_", ""):
            return 0.95

    for alias in aliases:
        if alias in col:
            return 0.80

    return 0.0


def suggest_mappings(source_key: str, table_name: str) -> list[dict]:
    c = connect()

    try:
        init_registry(c)

        source = c.execute(
            """
            SELECT source_key
            FROM sources
            WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()

        if source is None:
            return []

        table = c.execute(
            """
            SELECT table_name
            FROM source_tables
            WHERE source_key = ?
              AND table_name = ?
            """,
            (source_key, table_name),
        ).fetchone()

        if table is None:
            return []

        columns = c.execute(
            """
            SELECT column_name, column_type, ordinal
            FROM source_columns
            WHERE source_key = ?
              AND table_name = ?
            ORDER BY ordinal
            """,
            (source_key, table_name),
        ).fetchall()

        suggestions = []

        for logical_field in LOGICAL_FIELD_ALIASES:
            candidates = []

            for col in columns:
                score = mapping_score(
                    col["column_name"],
                    logical_field,
                )

                if score > 0:
                    candidates.append({
                        "column_name": col["column_name"],
                        "column_type": col["column_type"],
                        "ordinal": col["ordinal"],
                        "confidence": score,
                    })

            candidates.sort(
                key=lambda x: (
                    -x["confidence"],
                    x["ordinal"],
                )
            )

            if candidates:
                best = candidates[0]

                suggestions.append({
                    "logical_field": logical_field,
                    "suggested_column": best["column_name"],
                    "confidence": best["confidence"],
                    "candidates": candidates,
                })

        return suggestions

    finally:
        c.close()

def confirm_mappings(
    source_key: str,
    table_name: str,
    mappings: list[dict],
) -> list[dict]:
    c = connect()

    try:
        init_registry(c)

        source = c.execute(
            """
            SELECT source_key
            FROM sources
            WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()

        if source is None:
            raise ValueError(f"source not found: {source_key}")

        table = c.execute(
            """
            SELECT table_name
            FROM source_tables
            WHERE source_key = ?
              AND table_name = ?
            """,
            (source_key, table_name),
        ).fetchone()

        if table is None:
            raise ValueError(
                f"table not found: {source_key}.{table_name}"
            )

        ts = now()
        saved = []

        for item in mappings:
            logical_field = str(
                item.get("logical_field", "")
            ).strip()

            column_name = str(
                item.get("column_name", "")
            ).strip()

            confidence = item.get("confidence")

            if not logical_field or not column_name:
                continue

            column = c.execute(
                """
                SELECT column_name
                FROM source_columns
                WHERE source_key = ?
                  AND table_name = ?
                  AND column_name = ?
                """,
                (
                    source_key,
                    table_name,
                    column_name,
                ),
            ).fetchone()

            if column is None:
                raise ValueError(
                    f"column not found: {column_name}"
                )

            c.execute(
                """
                INSERT INTO field_mappings(
                    source_key,
                    table_name,
                    logical_field,
                    column_name,
                    confidence,
                    confirmed,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)

                ON CONFLICT(
                    source_key,
                    table_name,
                    logical_field,
                    column_name
                )
                DO UPDATE SET
                    confidence=excluded.confidence,
                    confirmed=1,
                    updated_at=excluded.updated_at
                """,
                (
                    source_key,
                    table_name,
                    logical_field,
                    column_name,
                    confidence,
                    ts,
                    ts,
                ),
            )

            saved.append({
                "logical_field": logical_field,
                "column_name": column_name,
                "confidence": confidence,
                "confirmed": True,
            })

        c.commit()

        return saved

    finally:
        c.close()

def main() -> None:
    init_registry()
    register_existing_sources()

    print()
    print(f"Registry: {REGISTRY_PATH}")
    print()

    for row in list_sources():
        print(
            f"{row['source_key']:<14} "
            f"{row['label']:<24} "
            f"{row['table_name']:<18} "
            f"{row['row_count']:>12,} rows"
        )


if __name__ == "__main__":
    main()
