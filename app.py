import os, re, sqlite3, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from logging_config import setup_logging

load_dotenv()
setup_logging()

from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from pandas import Timedelta

# Immutable source databases + separate disposable search index
env_dir = os.environ.get("SEARCH_DATA_DIR")
if env_dir:
    BASE = env_dir
else:
    guessed = Path(__file__).resolve().parent / "sqlite"
    BASE = str(guessed) if guessed.is_dir() else "./sqlite"

INDEX_PATH = os.environ.get("SEARCH_INDEX_DB", str(Path(BASE) / "unified_search_index.sqlite"))
DBS = {
    "ghmc": f"{BASE}/ghmc.sqlite",
    "excel": f"{BASE}/excel_data.sqlite",
    "mdb": f"{BASE}/mdb_data.sqlite",
    "zdata": f"{BASE}/zdata.sqlite",
}

SOURCES = {
    "ghmc": {"label": "GHMC", "db": "ghmc", "table": "ghmc", "fts": "ghmc_fts", "fts_cols": ["cname", "fname", "ladd", "ward_details"],
             "field_cols": {"addr": ["ladd", "ward_details"], "city": ["ladd", "ward_details"], "father": ["fname"], "pincode": ["pincode", "pin_code", "pin", "zipcode", "zip_code", "ladd", "ward_details"]}},
    "aarogyasri": {"label": "Aarogyasri (XLSX)", "db": "excel", "table": "aarogyasri", "fts": "aarogyasri_fts", "fts_cols": ["policy_holder_name", "nominee_name", "mandal_name", "secretariat_name"],
                   "field_cols": {"addr": ["mandal_name", "secretariat_name", "district"], "city": ["district", "mandal_name", "secretariat_name"], "father": ["father_name", "fname", "father"], "pincode": ["pincode", "pin_code", "pin", "zipcode", "zip_code", "mandal_name", "secretariat_name", "district"]}},
    "mdb": {"label": "Aarogyasri (MDB)", "db": "mdb", "table": "mdb_data", "fts": "mdb_fts", "fts_cols": ["CITIZEN_NAME", "FATHER_CARE_OF", "NOMINEE_NAME", "VILLAGE_NAME", "STREET"],
            "field_cols": {"addr": ["VILLAGE_NAME", "STREET", "mandal_name", "DISTRICT_NAME"], "city": ["VILLAGE_NAME", "mandal_name", "DISTRICT_NAME"], "father": ["FATHER_CARE_OF", "FATHER_NAME"], "pincode": ["PINCODE", "PIN_CODE", "pincode", "pin", "PIN", "VILLAGE_NAME", "STREET", "mandal_name", "DISTRICT_NAME"]}},
    "teachers": {"label": "Teachers / Schools", "db": "excel", "table": "teachers", "fts": "teachers_fts", "fts_cols": ["teacher_name", "school_name"],
                 "field_cols": {"addr": ["school_name", "district"], "city": ["district"], "father": ["father_name", "fname", "father"], "pincode": ["pincode", "pin_code", "pin", "zipcode", "zip_code", "school_name", "district"]}},
    "pdf": {"label": "PDF documents", "db": "excel", "table": "pdf_records", "fts": "pdf_fts", "fts_cols": ["text"],
            "field_cols": {"addr": ["text"], "city": ["text"], "father": ["text"], "pincode": ["text"]}},
    "zdata": {"label": "Zdata", "db": "zdata", "table": "zdata", "fts": "zdata_fts", "fts_cols": ["cname", "fname", "address"],
              "field_cols": {"addr": ["address"], "city": ["address"], "father": ["fname", "father_name", "father"], "pincode": ["pincode", "pin_code", "pin", "zipcode", "zip_code", "address"]}},
}

STATIC_SOURCES = {
    k: dict(v)
    for k, v in SOURCES.items()
}

STATIC_DBS = dict(DBS)

def registry_field_cols(source_key, table_name):
    field_cols = {}

    for m in get_mappings(source_key, table_name):
        if not m["confirmed"]:
            continue

        logical = m["logical_field"]
        column = m["column_name"]

        if logical == "address":
            logical = "addr"

        if logical == "id":
            continue

        field_cols.setdefault(logical, []).append(column)

    return field_cols

def registry_mapping_meta(source_key, table_name):
    out = {
        "id_cols": [],
        "phone_cols": [],
    }

    for m in get_mappings(source_key, table_name):
        if not m["confirmed"]:
            continue

        logical = m["logical_field"]
        column = m["column_name"]

        if logical == "id":
            out["id_cols"].append(column)

        elif logical == "phone":
            out["phone_cols"].append(column)

    return out


app = Flask(__name__)

# Admin authentication configuration
ADMIN_USERNAME = os.environ["ADMIN_USERNAME"]
ADMIN_PASSWORD_HASH = os.environ["ADMIN_PASSWORD_HASH"]
SECRET_KEY = os.environ["SECRET_KEY"]

if not ADMIN_USERNAME:
    raise RuntimeError("ADMIN_USERNAME is not configured")

if not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_PASSWORD_HASH is not configured")

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not configured")

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=Timedelta(hours=8),
)


def admin_authenticated():
    return bool(session.get("admin_authenticated"))


@app.before_request
def protect_admin_routes():
    path = request.path

    if path in ("/admin/login", "/admin/logout"):
        return None

    if path == "/admin" or path.startswith("/admin/") or path == "/api/admin" or path.startswith("/api/admin/"):
        if admin_authenticated():
            return None

        if path.startswith("/api/admin/"):
            return jsonify({"error": "authentication required"}), 401

        return redirect(url_for("admin_login"))

    return None


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if admin_authenticated():
        return redirect(url_for("admin_page"))

    if request.method == "GET":
        return render_template("admin_login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if username != ADMIN_USERNAME or not check_password_hash(ADMIN_PASSWORD_HASH, password):
        return render_template(
            "admin_login.html",
            error="Invalid user ID or password.",
        ), 401

    session.clear()
    session.permanent = True
    session["admin_authenticated"] = True
    return redirect(url_for("admin_page"))


@app.route("/admin/logout", methods=["POST", "GET"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))
from onboarding.registry import (
    list_sources,
    get_source,
    get_mappings,
    suggest_mappings,
    confirm_mappings,
    inspect_sqlite,
    delete_source,
    register_uploaded_source,
    UPLOAD_DIR,
    REGISTRY_PATH,
)
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from datetime import timedelta

# Add registry-managed sources without replacing static source definitions.
# Add registry-managed sources without replacing existing source definitions.
for _src in list_sources():
    if not _src.get("enabled"):
        continue

    _key = _src["source_key"]

    # Existing/static sources keep their original search configuration.
    if _key in SOURCES:
        continue

    _table = _src["table_name"]
    _meta = registry_mapping_meta(_key, _table)

    SOURCES[_key] = {
        "label": _src["label"],
        "db": _src["db_key"],
        "db_path": _src["db_path"],
        "table": _table,
        "fts": None,
        "fts_cols": [],
        "field_cols": registry_field_cols(
            _key,
            _table,
        ),
        "id_cols": _meta["id_cols"],
        "phone_cols": _meta["phone_cols"],
    }

    if _src["db_key"] not in DBS:
        DBS[_src["db_key"]] = _src["db_path"]


def is_source_id_query(key, value):
    cfg = SOURCES.get(key, {})

    if not cfg.get("id_cols"):
        return False

    raw = str(value or "").strip()

    if len(tokenize(raw)) != 1:
        return False

    d = digits(raw)
    c = compact(raw)

    if d and 1 <= len(d) <= 40:
        return True

    return (
        1 <= len(c) <= 40
        and any(ch.isdigit() for ch in c)
    )

def source_index_keys(key, value):
    if not is_source_id_query(key, value):
        return index_keys(value, "auto")

    s = str(value or "").strip()
    d = digits(s)
    c = compact(s)

    out = set()

    if d and 1 <= len(d) <= 40:
        out.add("n:" + d)

    if (
        1 <= len(c) <= 40
        and any(ch.isdigit() for ch in c)
    ):
        out.add("a:" + c)

    return sorted(out)
        

def qident(s):
    return '"' + str(s).replace('"', '""') + '"'


def tokenize(s):
    return [x for x in re.split(r"\s+", str(s or "").strip()) if x]


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def compact(s):
    return re.sub(r"[^0-9A-Za-z]+", "", str(s or "")).upper()


def index_keys(s, mode="auto"):
    s = str(s or "").strip()
    d = digits(s)
    c = compact(s)
    out = set()
    if mode in ("auto", "phone"):
        if len(d) == 10:
            out.add("p:" + d)
        elif 11 <= len(d) <= 15:
            out.add("p:" + d[-10:])
            out.add("pn:" + d)
    if mode in ("auto", "idcard"):
        if 8 <= len(d) <= 16:
            out.add("n:" + d)
        if 6 <= len(c) <= 40 and any(ch.isdigit() for ch in c):
            out.add("a:" + c)
    return sorted(out)


def is_identifier_query(s):
    # Treat formatted numeric identifiers as identifiers too. A phone such as
    # "+91 99898 01943" has multiple whitespace tokens but is still one ID.
    raw = str(s or '').strip()
    d = digits(raw)
    c = compact(raw)
    if not d:
        return False
    phone_chars = set('0123456789 +-.()/')
    if 10 <= len(d) <= 15 and all(ch in phone_chars for ch in raw):
        return True
    # Unformatted single-token numeric/alphanumeric identifiers.
    if len(tokenize(raw)) == 1:
        return len(d) >= 8 or (6 <= len(c) <= 40 and any(ch.isdigit() for ch in c))
    return False


def open_ro(dbkey):
    path = DBS.get(dbkey, dbkey)

    c = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=10,
    )
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA cache_size=-131072")
    c.execute("PRAGMA temp_store=MEMORY")
    c.execute("PRAGMA mmap_size=268435456")
    return c


def attach_index(c):
    if not os.path.exists(INDEX_PATH):
        return False
    c.execute("ATTACH DATABASE ? AS search_idx", (f"file:{INDEX_PATH}?mode=ro",))
    return True


def has_fts(c, name):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name=? AND sql LIKE '%fts5%'", (name,)).fetchone())


def quote_fts_token(t):
    return '"' + str(t).replace('"', '""') + '"*'


def fts_expr(cols, text):
    parts = []
    for tok in tokenize(text):
        per_col = " OR ".join(f"{qident(c)}:{quote_fts_token(tok)}" for c in cols)
        parts.append("(" + per_col + ")")
    return " AND ".join(parts)


def like_expr(cols, text):
    if not cols:
        return "0=1", []

    groups, args = [], []
    for tok in tokenize(text):
        groups.append("(" + " OR ".join(
            f"{qident(c)} LIKE ?" for c in cols
        ) + ")")
        args.extend([f"%{tok}%"] * len(cols))

    return " AND ".join(groups) or "0=1", args


def index_exists_expr(source_key, table, keys):
    if not keys:
        return "0=1", []

    ph = ",".join("?" for _ in keys)

    return (
        f"rowid IN ("
        f"SELECT i.source_rowid "
        f"FROM search_idx.identifiers AS i "
        f"WHERE i.source_key=? "
        f"AND i.table_name=? "
        f"AND i.key IN ({ph})"
        f")",
        [source_key, table, *keys],
    )

def add_condition(conds, args, expr, expr_args=()):
    if expr:
        conds.append(expr)
        args.extend(expr_args)


def build_source_query(key, q, fields):
    cfg = SOURCES[key]
    conds, args = [], []
    used_index = False
    used_fts = False
    fallback_like = False

    # Main query: every token is required. Identifier-shaped single tokens use
    # the disposable normalized index; text uses the source's existing FTS.
    if q:
        if is_identifier_query(q) or is_source_id_query(key, q):
            keys = source_index_keys(key, q)

            where, args = index_exists_expr(
                key,
                cfg["table"],
                keys,
            )

            return (
                cfg,
                where,
                args,
                True,
                False,
                False,
            )
        else:
            if has_fts_for(cfg):
                expr = f"rowid IN (SELECT rowid FROM {qident(cfg['fts'])} WHERE {qident(cfg['fts'])} MATCH ?)"
                add_condition(conds, args, expr, [fts_expr(cfg["fts_cols"], q)])
                used_fts = True
            else:
                expr, a = like_expr(cfg["fts_cols"], q)
                add_condition(conds, args, expr, a)
                fallback_like = True

    # All populated field boxes are ANDed on the server. Phone/ID fields use
    # the unified index so malformed source columns do not prevent a match.
    for fk, value in fields.items():
        if fk in ("phone", "idcard"):
            keys = index_keys(value, "phone" if fk == "phone" else "idcard")
            expr, a = index_exists_expr(key, cfg["table"], keys)
            add_condition(conds, args, expr, a)
            used_index = True
        elif fk in cfg["field_cols"]:
            # Pincode is fundamentally an address search because most source
            # tables do not have a dedicated pincode column.
            if fk == "pincode":
                cols = list(dict.fromkeys(
                    source_field_cols(key, "pincode") +
                    source_field_cols(key, "addr")
                ))
            else:
                cols = source_field_cols(key, fk)

            if not cols:
                # A requested field is unavailable in this source, so this
                # source must not return rows for that filter.
                add_condition(conds, args, "0=1")
                continue

            # Father Name is text and can use FTS where available.
            if fk == "father":
                if has_fts_for(cfg) and all(c in cfg["fts_cols"] for c in cols):
                    expr = f"rowid IN (SELECT rowid FROM {qident(cfg['fts'])} WHERE {qident(cfg['fts'])} MATCH ?)"
                    add_condition(conds, args, expr, [fts_expr(cols, value)])
                    used_fts = True
                else:
                    expr, a = like_expr(cols, value)
                    add_condition(conds, args, expr, a)
                    fallback_like = True
            else:
                # Pincode is a required AND filter. Search the supplied
                # pincode as a literal substring in address/location fields
                # (and any dedicated pincode field that actually exists).
                expr, a = like_expr(cols, value)
                add_condition(conds, args, expr, a)
                fallback_like = True

    where = " AND ".join(conds) if conds else "1=0"
    return cfg, where, args, used_index, used_fts, fallback_like


def has_fts_for(cfg):
    # Cached by source metadata to avoid opening a connection for every query.
    return bool(FTS_AVAIL.get((cfg["db"], cfg["fts"]), False))



# Cache actual source-table columns so optional filter candidates that do not
# exist in a particular source are simply skipped.
TABLE_COLS = {}
for _key, _cfg in SOURCES.items():
    try:
        _c = open_ro(_cfg["db"])
        TABLE_COLS[_key] = {r[1] for r in _c.execute(
            f"PRAGMA table_info({qident(_cfg['table'])})"
        ).fetchall()}
        _c.close()
    except Exception:
        TABLE_COLS[_key] = set()

def source_field_cols(key, fk):
    return [c for c in SOURCES[key].get("field_cols", {}).get(fk, [])
            if c in TABLE_COLS.get(key, set())]

def fetch_group(key, q, fields, page, per):
    cfg, where, args, used_index, used_fts, fallback_like = build_source_query(key, q, fields)
    off = (page - 1) * per
    c = open_ro(cfg["db"])
    try:
        if used_index:
            attach_index(c)
        sql = f"SELECT rowid AS __rowid, * FROM {qident(cfg['table'])} AS t WHERE {where} LIMIT ? OFFSET ?"
        rows = [dict(r) for r in c.execute(sql, (*args, per + 1, off)).fetchall()]
        more = len(rows) > per
        rows = rows[:per]
        for r in rows:
            r["__source"] = key
            r["__table"] = cfg["table"]
        note = None
        if fallback_like:
            note = "This source used a direct text filter for at least one field; indexed/FTS paths were used where available."
        return {"key": key, "label": cfg["label"], "rows": rows, "more": more, "page": page,
                "used_index": used_index, "used_fts": used_fts, "note": note}
    finally:
        try:
            c.close()
        except Exception:
            pass


# Discover FTS availability once at startup.
FTS_AVAIL = {}
for _key, _cfg in SOURCES.items():
    try:
        c = open_ro(_cfg["db"])
        FTS_AVAIL[(_cfg["db"], _cfg["fts"])] = has_fts(c, _cfg["fts"])
        c.close()
    except Exception:
        FTS_AVAIL[(_cfg["db"], _cfg["fts"])] = False

@app.route("/api/admin/sources")
def admin_sources():
    try:
        return jsonify({
            "registry": str(REGISTRY_PATH),
            "sources": list_sources()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/sources/<source_key>")
def admin_source_detail(source_key):
    source = get_source(source_key)

    if source is None:
        return jsonify({
            "error": f"source not found: {source_key}"
        }), 404

    return jsonify({
        "source": source
    })

@app.route(
    "/api/admin/sources/<source_key>",
    methods=["DELETE"],
)
def admin_delete_source(source_key):
    try:
        result = delete_source(source_key)

        return jsonify(result)

    except ValueError as e:
        return jsonify({
            "error": str(e)
        }), 400

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500

@app.route("/api/admin/sources/<source_key>/mappings")
def admin_source_mappings(source_key):
    try:
        source = get_source(source_key)

        if source is None:
            return jsonify({
                "error": f"source not found: {source_key}"
            }), 404

        table_name = request.args.get("table")

        return jsonify({
            "source_key": source_key,
            "table": table_name,
            "mappings": get_mappings(source_key, table_name),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route(
    "/api/admin/sources/<source_key>/mappings/confirm",
    methods=["POST"],
)
def admin_confirm_mappings(source_key):
    try:
        source = get_source(source_key)

        if source is None:
            return jsonify({
                "error": f"source not found: {source_key}"
            }), 404

        table_name = request.args.get("table")

        if not table_name:
            return jsonify({
                "error": "table parameter is required"
            }), 400

        data = request.get_json(silent=True) or {}
        mappings = data.get("mappings")

        if not isinstance(mappings, list):
            return jsonify({
                "error": "mappings must be a list"
            }), 400

        saved = confirm_mappings(
            source_key,
            table_name,
            mappings,
        )
        return jsonify({
            "source_key": source_key,
            "table": table_name,
            "saved": saved,
            "count": len(saved),
        })

    except ValueError as e:
        return jsonify({
            "error": str(e)
        }), 400

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500


@app.route(
    "/api/admin/sources/<source_key>/mapping-suggestions"
)
def admin_mapping_suggestions(source_key):

    table_name = request.args.get("table")

    if not table_name:
        return jsonify({
            "error": "table is required"
        }), 400

    try:

        suggestions = suggest_mappings(
            source_key,
            table_name
        )

        return jsonify({
            "source_key": source_key,
            "table_name": table_name,
            "suggestions": suggestions
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 400

@app.route("/api/admin/upload", methods=["POST"])
def admin_upload():
    try:
        if "file" not in request.files:
            return jsonify({
                "error": "file is required"
            }), 400

        file = request.files["file"]

        if not file.filename:
            return jsonify({
                "error": "filename is required"
            }), 400

        filename = secure_filename(file.filename)

        if not filename.lower().endswith(".sqlite"):
            return jsonify({
                "error": "only .sqlite files are supported"
            }), 400

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        target = UPLOAD_DIR / filename

        if target.exists():
            return jsonify({
                "error": "file already exists",
                "path": str(target)
            }), 409

        file.save(target)

        inspection = inspect_sqlite(target)

        return jsonify({
            "status": "uploaded",
            "filename": filename,
            "path": str(target),
            "inspection": inspection,
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500

@app.route(
    "/api/admin/uploads/register",
    methods=["POST"],
)
def admin_register_upload():
    try:
        data = request.get_json(silent=True) or {}

        filename = str(
            data.get("filename", "")
        ).strip()

        label = data.get("label")

        if not filename:
            return jsonify({
                "error": "filename is required"
            }), 400

        filename = secure_filename(filename)

        if not filename.lower().endswith(".sqlite"):
            return jsonify({
                "error": "only .sqlite files are supported"
            }), 400

        db_path = UPLOAD_DIR / filename

        if not db_path.exists():
            return jsonify({
                "error": "uploaded database not found",
                "path": str(db_path),
            }), 404

        result = register_uploaded_source(
            db_path,
            label=label,
        )

        return jsonify(result), 201

    except ValueError as e:
        return jsonify({
            "error": str(e)
        }), 400

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/admin")
def admin_page():
    return render_template("admin.html")

@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    fields = {k: (request.args.get(k) or "").strip()
               for k in ("addr", "phone", "idcard", "city", "pincode", "father")}
    fields = {k: v for k, v in fields.items() if v}
    scope = request.args.get("scope", "all")
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
        per = min(100, max(10, int(request.args.get("per", 50) or 50)))
    except ValueError:
        return jsonify({"error": "invalid page/per"}), 400
    if not q and not fields:
        return jsonify({"error": "empty query"}), 400

    keys = list(SOURCES) if scope == "all" else [scope]
    keys = [k for k in keys if k in SOURCES]
    t0 = time.time()
    if len(keys) == 1:
        groups = [fetch_group(keys[0], q, fields, page, per)]
    else:
        with ThreadPoolExecutor(max_workers=len(keys)) as ex:
            groups = list(ex.map(lambda k: fetch_group(k, q, fields, page, per), keys))
    return jsonify({"q": q, "scope": scope, "page": page, "per": per, "fields": fields,
                    "took_ms": int((time.time() - t0) * 1000), "groups": groups,
                    "index": {"path": INDEX_PATH, "available": os.path.exists(INDEX_PATH)}})



@app.route("/api/stats")
def stats():
    out = []
    for key, cfg in SOURCES.items():
        try:
            c = open_ro(cfg["db"])
            n = c.execute(f"SELECT COUNT(*) FROM {qident(cfg['table'])}").fetchone()[0]
            c.close()
        except Exception:
            n = 0
        out.append({"key": key, "label": cfg["label"], "rows": n})
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
