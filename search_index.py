"""Phase 2 disposable search index with crash-safe, resumable checkpoints.

Source SQLite files are NEVER opened writable by this module. The index stores
provenance + normalized identifier keys. Existing source data is left intact.
"""
from __future__ import annotations
import os, re, sqlite3, time
from pathlib import Path

BASE = os.environ.get("SEARCH_DATA_DIR", "./sqlite")
INDEX_PATH = os.environ.get("SEARCH_INDEX_DB", str(Path(BASE) / "unified_search_index.sqlite"))

DBS = {
    "ghmc": f"{BASE}/ghmc.sqlite",
    "excel": f"{BASE}/excel_data.sqlite",
    "mdb": f"{BASE}/mdb_data.sqlite",
    "zdata": f"{BASE}/zdata.sqlite",
}
TABLES = {
    "ghmc": [("ghmc", "ghmc")],
    "aarogyasri": [("excel", "aarogyasri")],
    "mdb": [("mdb", "mdb_data")],
    "teachers": [("excel", "teachers")],
    "pdf": [("excel", "pdf_records")],
    "zdata": [("zdata", "zdata")],
}
ID_COLUMNS = {
    "ghmc": ["mobile", "uid", "cname", "fname", "ladd", "ward_details", "gender", "relation", "age"],
    "aarogyasri": ["district", "mandal_code", "mandal_name", "secretariat_code", "secretariat_name", "srno", "rice_card_no", "policy_holder_name", "policy_holder_gender", "account_number", "bankname", "branchname", "ifsc", "policy_holder_mobile", "nominee_name", "status", "nominee_relation", "source_file"],
    "mdb": ["DISTRICT_NAME", "mandal_name", "VILLAGE_NAME", "pan_name", "HOUSEHOLD_ID", "UID_NUM", "CITIZEN_NAME", "FATHER_CARE_OF", "DOB_DT", "AGE", "MOBILE_NUMBER", "NOMINEE_NAME", "NOMINEE_AGE", "NOMINEE_RELATION", "policynumber", "rural_urban", "DNO", "STREET", "ward", "claim", "source_file"],
    "teachers": ["district", "teacher_name", "sex", "mobile", "caste", "dob", "year_of_joining", "aadhar", "school_name", "source_file"],
    "pdf": ["source_file", "page", "text"],
    "zdata": ["operator", "mobile", "doa", "ctype", "cname", "fname", "address", "altno"],
}


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def normalized_identifier_values(value):
    s = str(value or "").strip()
    if not s:
        return set()
    d = digits(s)
    out = set()
    if len(d) == 10:
        out.add("p:" + d)
    elif 11 <= len(d) <= 15:
        out.add("p:" + d[-10:])
        out.add("pn:" + d)
    if 8 <= len(d) <= 16:
        out.add("n:" + d)
    compact = re.sub(r"[^0-9A-Za-z]+", "", s).upper()
    if 6 <= len(compact) <= 40 and any(ch.isdigit() for ch in compact):
        out.add("a:" + compact)
    return out


def open_source(dbkey):
    c = sqlite3.connect(f"file:{DBS[dbkey]}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    c.execute("PRAGMA cache_size=-131072")
    c.execute("PRAGMA mmap_size=268435456")
    return c


def init_index(c):
    c.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    PRAGMA temp_store=MEMORY;
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS identifiers (
      key TEXT NOT NULL,
      source_key TEXT NOT NULL,
      table_name TEXT NOT NULL,
      source_rowid INTEGER NOT NULL,
      PRIMARY KEY(key, source_key, table_name, source_rowid)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS idx_identifiers_key
      ON identifiers(key, source_key, table_name, source_rowid);
    CREATE TABLE IF NOT EXISTS checkpoints (
      source_key TEXT NOT NULL,
      db_key TEXT NOT NULL,
      table_name TEXT NOT NULL,
      last_rowid INTEGER NOT NULL DEFAULT 0,
      source_max_rowid INTEGER,
      source_row_count INTEGER,
      rows_processed INTEGER NOT NULL DEFAULT 0,
      identifiers_written INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'pending',
      updated_at INTEGER NOT NULL,
      PRIMARY KEY(source_key, table_name)
    );
    """)


def table_columns(c, table):
    return [r[1] for r in c.execute(f'PRAGMA table_info("{table}")')]


def qident(name):
    return '"' + name.replace('"', '""') + '"'


def get_checkpoint(idx, logical, dbkey, table):
    return idx.execute(
        "SELECT source_key, db_key, table_name, last_rowid, source_max_rowid, source_row_count, rows_processed, identifiers_written, status FROM checkpoints WHERE source_key=? AND table_name=?",
        (logical, table)).fetchone()


def set_checkpoint(idx, logical, dbkey, table, last_rowid, max_rowid, row_count,
                   rows_processed, identifiers_written, status):
    idx.execute("""
      INSERT INTO checkpoints(source_key,db_key,table_name,last_rowid,source_max_rowid,
                              source_row_count,rows_processed,identifiers_written,status,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(source_key,table_name) DO UPDATE SET
        db_key=excluded.db_key, last_rowid=excluded.last_rowid,
        source_max_rowid=excluded.source_max_rowid, source_row_count=excluded.source_row_count,
        rows_processed=excluded.rows_processed, identifiers_written=excluded.identifiers_written,
        status=excluded.status, updated_at=excluded.updated_at
    """, (logical, dbkey, table, int(last_rowid), max_rowid, row_count,
          int(rows_processed), int(identifiers_written), status, int(time.time())))


def build(rebuild=False, batch=10000):
    Path(INDEX_PATH).parent.mkdir(parents=True, exist_ok=True)
    if rebuild and os.path.exists(INDEX_PATH):
        for suffix in ("", "-wal", "-shm"):
            try: os.remove(INDEX_PATH + suffix)
            except FileNotFoundError: pass
    idx = sqlite3.connect(INDEX_PATH)
    idx.execute("PRAGMA busy_timeout=30000")
    init_index(idx)
    started = time.time()
    total_new_rows = 0
    try:
        for logical, entries in TABLES.items():
            for dbkey, table in entries:
                if not os.path.exists(DBS[dbkey]):
                    print(f"SKIP {logical}: missing {DBS[dbkey]}")
                    continue
                src = open_source(dbkey)
                cols = table_columns(src, table)
                scan_cols = [x for x in ID_COLUMNS[logical] if x in cols]
                if not scan_cols:
                    print(f"SKIP {logical}: no indexable columns")
                    src.close(); continue

                source_max = src.execute(f'SELECT COALESCE(MAX(rowid),0) FROM {qident(table)}').fetchone()[0] or 0
                source_count = src.execute(f'SELECT COUNT(*) FROM {qident(table)}').fetchone()[0] or 0
                cp = get_checkpoint(idx, logical, dbkey, table)

                # A previous version of Phase 2 had no checkpoints. Recover safely
                # from its existing index by using the highest indexed source rowid.
                if cp:
                    last_rowid = int(cp[3] or 0)
                    rows_processed = int(cp[6] or 0)
                    identifiers_written = int(cp[7] or 0)
                    if cp[8] == 'done' and last_rowid >= source_max:
                        print(f"RESUME {logical}: already complete ({rows_processed:,} rows)")
                        src.close(); continue
                else:
                    # The original Phase 2 index had no checkpoints. Recover using
                    # MAX(source_rowid), which is cheap compared with COUNT(DISTINCT)
                    # over a potentially 100M+ row identifier table. Rows are scanned
                    # in rowid order, so everything at/below this watermark was already
                    # committed. Any tail rows are reprocessed safely with INSERT OR IGNORE.
                    rec = idx.execute(
                        "SELECT MAX(source_rowid) FROM identifiers WHERE source_key=? AND table_name=?",
                        (logical, table)).fetchone()
                    last_rowid = int(rec[0] or 0)
                    rows_processed = 0
                    identifiers_written = 0
                    print(f"RECOVER {logical}: existing index reaches rowid {last_rowid:,}; resuming after it")

                set_checkpoint(idx, logical, dbkey, table, last_rowid, source_max,
                               source_count, rows_processed, identifiers_written, 'running')
                idx.commit()

                col_sql = ", ".join(qident(x) for x in scan_cols)
                cur = src.execute(
                    f'SELECT rowid, {col_sql} FROM {qident(table)} WHERE rowid > ? ORDER BY rowid',
                    (last_rowid,))
                local_rows = rows_processed
                local_ids = identifiers_written
                t0 = time.time()
                while True:
                    rows = cur.fetchmany(batch)
                    if not rows:
                        break
                    id_rows = []
                    batch_last_rowid = last_rowid
                    for r in rows:
                        rid = int(r[0])
                        batch_last_rowid = rid
                        vals = set()
                        for v in r[1:]:
                            vals.update(normalized_identifier_values(v))
                        for k in vals:
                            id_rows.append((k, logical, table, rid))
                    idx.executemany(
                        'INSERT OR IGNORE INTO identifiers(key,source_key,table_name,source_rowid) VALUES (?,?,?,?)',
                        id_rows)
                    local_rows += len(rows)
                    local_ids += len(id_rows)
                    last_rowid = batch_last_rowid
                    set_checkpoint(idx, logical, dbkey, table, last_rowid, source_max,
                                   source_count, local_rows, local_ids, 'running')
                    idx.commit()
                    total_new_rows += len(rows)
                    if local_rows == len(rows) or local_rows % 100000 < batch:
                        rate = (local_rows / max(time.time() - t0, 0.001))
                        print(f"{logical}: indexed {local_rows:,}/{source_count:,} rows; rowid {last_rowid:,}; {rate:,.0f} rows/s")

                set_checkpoint(idx, logical, dbkey, table, source_max, source_max,
                               source_count, local_rows, local_ids, 'done')
                idx.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                            (f'done:{logical}', str(int(time.time()))))
                idx.commit()
                print(f"DONE {logical}: {local_rows:,} rows, {local_ids:,} identifier entries")
                src.close()

        idx.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', ('built_at', str(int(time.time()))))
        idx.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', ('version','2-resumable'))
        idx.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', ('status','complete'))
        idx.commit()
    finally:
        idx.close()
    print(f"DONE ALL: {INDEX_PATH} — {total_new_rows:,} newly processed source rows in {time.time()-started:.1f}s")


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Build/resume the Phase 2 identifier index.')
    ap.add_argument('--rebuild', action='store_true', help='Delete the index and start over (normally NOT needed).')
    ap.add_argument('--batch', type=int, default=10000)
    args = ap.parse_args()
    build(rebuild=args.rebuild, batch=args.batch)
