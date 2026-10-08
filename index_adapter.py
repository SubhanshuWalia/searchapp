from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

from onboarding.registry import (
    get_source,
    get_mappings,
    UNIFIED_INDEX_PATH,
)

from search_index import (
    normalized_identifier_values,
    init_index,
    digits,
    qident,
)
import logging

logger = logging.getLogger(__name__)

def open_source(db_path):
    c = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=30,
    )

    c.row_factory = sqlite3.Row

    c.execute("PRAGMA query_only=ON")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA cache_size=-131072")
    c.execute("PRAGMA temp_store=MEMORY")
    c.execute("PRAGMA mmap_size=268435456")

    return c


def get_columns(c, table_name):
    return [
        row["name"]
        for row in c.execute(
            f"PRAGMA table_info({qident(table_name)})"
        )
    ]


def get_index_columns(source_key, table_name):
    mappings = get_mappings(
        source_key,
        table_name,
    )

    cols = []

    for m in mappings:

        if not m["confirmed"]:
            continue

        if m["logical_field"] not in {
            "phone",
            "id",
        }:
            continue

        column = m["column_name"]

        if not any(
            x["column_name"] == column
            for x in cols
        ):
            cols.append({
                "logical_field": m["logical_field"],
                "column_name": column,
            })

    return cols


def build_index_plan(source_key, table_name):

    source = get_source(source_key)

    if source is None:
        raise ValueError(
            f"source not found: {source_key}"
        )

    tables = {
        t["name"]: t
        for t in source["tables"]
    }

    if table_name not in tables:
        raise ValueError(
            f"table not found: {source_key}.{table_name}"
        )

    table = tables[table_name]

    mappings = get_mappings(
        source_key,
        table_name,
    )

    confirmed = [
        m for m in mappings
        if m["confirmed"]
    ]

    index_columns = get_index_columns(
        source_key,
        table_name,
    )

    source_path = Path(
        source["database"]
    )

    if not source_path.exists():
        raise ValueError(
            f"source database not found: {source_path}"
        )

    with open_source(source_path) as src:

        columns = get_columns(
            src,
            table_name,
        )

        missing = [
            m["column_name"]
            for m in index_columns
            if m["column_name"] not in columns
        ]

        row_count = src.execute(
            f"SELECT COUNT(*) FROM {qident(table_name)}"
        ).fetchone()[0]

        max_rowid = src.execute(
            f"SELECT COALESCE(MAX(rowid), 0) "
            f"FROM {qident(table_name)}"
        ).fetchone()[0]

    return {
        "source_key": source_key,
        "table_name": table_name,
        "database": str(source_path),
        "row_count": row_count,
        "max_rowid": max_rowid,
        "confirmed_mappings": confirmed,
        "index_columns": index_columns,
        "missing_columns": missing,
        "index_path": str(UNIFIED_INDEX_PATH),
    }


def print_plan(plan):

    print()
    print("INDEX PLAN")
    print("-" * 60)
    print("Source:       ", plan["source_key"])
    print("Table:        ", plan["table_name"])
    print("Database:     ", plan["database"])
    print("Rows:         ", f'{plan["row_count"]:,}')
    print("Max rowid:    ", f'{plan["max_rowid"]:,}')
    print("Index:        ", plan["index_path"])
    print()

    print("Confirmed mappings:")

    for m in plan["confirmed_mappings"]:
        print(
            f'  {m["logical_field"]:<15} '
            f'-> {m["column_name"]}'
        )

    print()

    print("Identifier columns:")

    for m in plan["index_columns"]:
        print(
            f'  {m["logical_field"]:<15} '
            f'-> {m["column_name"]}'
        )
    if plan["missing_columns"]:
        print()
        print("MISSING COLUMNS:")

        for col in plan["missing_columns"]:
            print(" ", col)

    print("-" * 60)
    print()


def ensure_index_record(
    source_key,
    table_name,
    status="pending",
):
    from onboarding.registry import connect, init_registry

    c = connect()

    try:

        init_registry(c)

        ts = int(time.time())

        c.execute(
            """
            INSERT INTO indexes(
                source_key,
                table_name,
                index_type,
                index_path,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'unified', ?, ?, ?, ?)

            ON CONFLICT(
                source_key,
                table_name,
                index_type
            )
            DO UPDATE SET
                index_path=excluded.index_path,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                source_key,
                table_name,
                str(UNIFIED_INDEX_PATH),
                status,
                ts,
                ts,
            ),
        )

        c.commit()

    finally:
        c.close()

def delete_source_index(source_key):
    source = get_source(source_key)

    if not source:
        raise ValueError(f"source not found: {source_key}")

    db_path = source["database"]

    tables = [
        t["name"]
        for t in source.get("tables", [])
        if t.get("searchable")
    ]

    if not tables:
        return {
            "source_key": source_key,
            "rows_processed": 0,
            "identifiers_deleted": 0,
        }

    if len(tables) != 1:
        raise ValueError(
            f"source has {len(tables)} searchable tables; "
            "delete_source_index currently expects one table"
        )

    table_name = tables[0]

    mappings = get_mappings(source_key, table_name)

    index_columns = []
    for m in mappings:
        if not m["confirmed"]:
            continue

        if m["logical_field"] not in {"phone", "id"}:
            continue

        index_columns.append({
            "logical_field": m["logical_field"],
            "column_name": m["column_name"],
        })

    if not index_columns:
        return {
            "source_key": source_key,
            "table_name": table_name,
            "rows_processed": 0,
            "identifiers_deleted": 0,
        }

    db = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=30,
    )
    db.row_factory = sqlite3.Row

    idx = sqlite3.connect(
        UNIFIED_INDEX_PATH,
        timeout=30,
    )

    try:
        idx.execute("PRAGMA busy_timeout=30000")

        cols = ", ".join(
            qident(m["column_name"])
            for m in index_columns
        )

        cur = db.execute(
            f"""
            SELECT rowid, {cols}
            FROM {qident(table_name)}
            ORDER BY rowid
            """
        )

        rows_processed = 0
        identifiers_deleted = 0

        while True:
            rows = cur.fetchmany(10000)

            if not rows:
                break

            delete_rows = []

            for row in rows:
                rowid = int(row[0])

                values = set()

                for i, value in enumerate(row[1:]):
                    logical_field = index_columns[i]["logical_field"]

                    if logical_field == "id":
                        s = str(value or "").strip()

                        if s:
                            d = digits(s)

                            if d:
                                values.add("n:" + d)

                            compact = re.sub(
                                r"[^0-9A-Za-z]+",
                                "",
                                s,
                            ).upper()

                            if (
                                1 <= len(compact) <= 40
                                and any(ch.isdigit() for ch in compact)
                            ):
                                values.add("a:" + compact)

                    elif logical_field == "phone":
                        values.update(
                            normalized_identifier_values(value)
                        )

                for key in values:
                    delete_rows.append(
                        (
                            key,
                            source_key,
                            table_name,
                            rowid,
                        )
                    )

            if delete_rows:
                before = idx.total_changes

                idx.executemany(
                    """
                    DELETE FROM identifiers
                    WHERE key=?
                      AND source_key=?
                      AND table_name=?
                      AND source_rowid=?
                    """,
                    delete_rows,
                )

                identifiers_deleted += (
                    idx.total_changes - before
                )

            rows_processed += len(rows)

            idx.commit()

        idx.execute(
            """
            DELETE FROM checkpoints
            WHERE source_key=?
              AND table_name=?
            """,
            (source_key, table_name),
        )

        idx.commit()

        return {
            "source_key": source_key,
            "table_name": table_name,
            "rows_processed": rows_processed,
            "identifiers_deleted": identifiers_deleted,
        }

    finally:
        db.close()
        idx.close()

def build_source_table(
    source_key,
    table_name,
    batch=10000,
):

    plan = build_index_plan(
        source_key,
        table_name,
    )

    if plan["missing_columns"]:
        raise ValueError(
            "mapped columns no longer exist: "
            + ", ".join(plan["missing_columns"])
        )

    if not plan["index_columns"]:
        raise ValueError(
            "no confirmed phone/id mappings available"
        )

    Path(
        UNIFIED_INDEX_PATH
    ).parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    ensure_index_record(
        source_key,
        table_name,
        "running",
    )

    idx = sqlite3.connect(
        UNIFIED_INDEX_PATH
    )

    idx.execute(
        "PRAGMA busy_timeout=30000"
    )

    init_index(idx)
    logger.debug("init_index completed") # added debug statement
    src = None

    try:

        src = open_source(
            plan["database"]
        )
        logger.debug("source opened")
        source_max = plan["max_rowid"]
        source_count = plan["row_count"]

        cp = idx.execute(
            """
            SELECT
                last_rowid,
                rows_processed,
                identifiers_written,
                status
            FROM checkpoints
            WHERE source_key=?
              AND table_name=?
            """,
            (
                source_key,
                table_name,
            ),
        ).fetchone()
        logger.debug("checkpoint SELECT completed")
        if cp:

            last_rowid = int(
                cp[0] or 0
            )

            rows_processed = int(
                cp[1] or 0
            )

            identifiers_written = int(
                cp[2] or 0
            )

            if (
                cp[3] == "done"
                and last_rowid >= source_max
            ):
                ensure_index_record(
                    source_key,
                    table_name,
                    "ready",
                )

                return {
                    "status": "already_complete",
                    "rows_processed": rows_processed,
                    "identifiers_written":
                        identifiers_written,
                }

        else:
            last_rowid = 0
            rows_processed = 0
            identifiers_written = 0
            
        idx.execute(
            """
            INSERT INTO checkpoints(
                source_key,
                db_key,
                table_name,
                last_rowid,
                source_max_rowid,
                source_row_count,
                rows_processed,
                identifiers_written,
                status,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)

            ON CONFLICT(
                source_key,
                table_name
            )
            DO UPDATE SET
                db_key=excluded.db_key,
                last_rowid=excluded.last_rowid,
                source_max_rowid=excluded.source_max_rowid,
                source_row_count=excluded.source_row_count,
                rows_processed=excluded.rows_processed,
                identifiers_written=excluded.identifiers_written,
                status='running',
                updated_at=excluded.updated_at
            """,
            (
                source_key,
                plan["database"],
                table_name,
                last_rowid,
                source_max,
                source_count,
                rows_processed,
                identifiers_written,
                int(time.time()),
            ),
        )

        idx.commit()
        logger.debug("checkpoint committed")
        col_sql = ", ".join(
            qident(m["column_name"])
            for m in plan["index_columns"]
        )
        

        cur = src.execute(
            f"""
            SELECT
                rowid,
                {col_sql}
            FROM {qident(table_name)}
            WHERE rowid > ?
            ORDER BY rowid
            """,
            (last_rowid,),
        )
        logger.debug("source SELECT executed")
        started = time.time()

        while True:

            rows = cur.fetchmany(batch)
            logger.debug(f"fetchmany starting, fetched {len(rows)} rows")
            if not rows:
                break
            logger.debug(f"fetched {len(rows)} rows")
            id_rows = []

            batch_last_rowid = last_rowid

            for row in rows:

                rid = int(row[0])

                batch_last_rowid = rid

                values = set()

                for i, value in enumerate(row[1:]):

                    logical_field = plan["index_columns"][i]["logical_field"]

                    if logical_field == "id":
                        s = str(value or "").strip()

                        if s:
                            d = digits(s)

                            if d:
                                values.add("n:" + d)

                            compact = re.sub(
                                r"[^0-9A-Za-z]+",
                                "",
                                s,
                            ).upper()

                            if (
                                1 <= len(compact) <= 40
                                and any(ch.isdigit() for ch in compact)
                            ):
                                values.add("a:" + compact)

                    elif logical_field == "phone":
                        values.update(
                            normalized_identifier_values(value)
                        )

                for key in values:

                    id_rows.append(
                        (
                            key,
                            source_key,
                            table_name,
                            rid,
                        )
                    )
            logger.debug(f"inserting {len(id_rows)} identifiers")
            idx.executemany(
                """
                INSERT OR IGNORE INTO identifiers(
                    key,
                    source_key,
                    table_name,
                    source_rowid
                )
                VALUES (?, ?, ?, ?)
                """,
                id_rows,
            )
            logger.debug("executemany completed")

            rows_processed += len(rows)

            identifiers_written += len(
                id_rows
            )

            last_rowid = batch_last_rowid

            idx.execute(
                """
                UPDATE checkpoints
                SET
                    last_rowid=?,
                    source_max_rowid=?,
                    source_row_count=?,
                    rows_processed=?,
                    identifiers_written=?,
                    status='running',
                    updated_at=?
                WHERE source_key=?
                  AND table_name=?
                """,
                (
                    last_rowid,
                    source_max,
                    source_count,
                    rows_processed,
                    identifiers_written,
                    int(time.time()),
                    source_key,
                    table_name,
                ),
            )

            idx.commit()

            rate = (
                rows_processed /
                max(time.time() - started, 0.001)
            )

            logger.info(
                f"{source_key}.{table_name}: "
                f"{rows_processed:,}/{source_count:,} "
                f"rows; "
                f"{rate:,.0f} rows/s"
            )

        idx.execute(
            """
            UPDATE checkpoints
            SET
                last_rowid=?,
                source_max_rowid=?,
                source_row_count=?,
                rows_processed=?,
                identifiers_written=?,
                status='done',
                updated_at=?
            WHERE source_key=?
              AND table_name=?
            """,
            (
                source_max,
                source_max,
                source_count,
                rows_processed,
                identifiers_written,
                int(time.time()),
                source_key,
                table_name,
            ),
        )

        idx.commit()

        ensure_index_record(
            source_key,
            table_name,
            "ready",
        )

        return {
            "status": "complete",
            "rows_processed": rows_processed,
            "identifiers_written":
                identifiers_written,
        }

    except Exception as e:

        logger.error(f"Error occurred while building index for {source_key}.{table_name}: {e}")
        ensure_index_record(
            source_key,
            table_name,
            "error",
        )

        raise

    finally:

        if src:
            src.close()

        idx.close()


if __name__ == "__main__":

    import argparse

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--source",
        required=True,
    )

    ap.add_argument(
        "--table",
        required=True,
    )

    ap.add_argument(
        "--batch",
        type=int,
        default=10000,
    )

    ap.add_argument(
        "--plan",
        action="store_true",
    )

    args = ap.parse_args()

    plan = build_index_plan(
        args.source,
        args.table,
    )

    print_plan(plan)

    if not args.plan:
        result = build_source_table(
            args.source,
            args.table,
            batch=args.batch,
        )

        print()
        print(result)