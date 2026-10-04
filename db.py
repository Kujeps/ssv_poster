import sqlite3
import time

conn = sqlite3.connect("poster.db", check_same_thread=False)
conn.row_factory = sqlite3.Row

POSTS_DDL = """CREATE TABLE posts (
    worker_id INTEGER, message_id INTEGER, media_group_id TEXT, active INTEGER DEFAULT 1,
    added_at REAL, PRIMARY KEY (worker_id, message_id))"""


def _has(table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def _cols(table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


conn.executescript("""
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, bot_id INTEGER, username TEXT,
    name TEXT, channel_ref TEXT, channel_id INTEGER, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT,
    enabled INTEGER DEFAULT 1, interval INTEGER, next_at REAL DEFAULT 0,
    active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS sent (chat_id INTEGER, unit TEXT, PRIMARY KEY (chat_id, unit));
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat_id INTEGER, chat_title TEXT,
    unit TEXT, status TEXT, error TEXT);
""")
for _t, _c, _d in [("chats", "link", "TEXT"), ("chats", "worker_id", "INTEGER DEFAULT 1"),
                   ("log", "worker_id", "INTEGER DEFAULT 1"), ("log", "worker_name", "TEXT")]:
    if _c not in _cols(_t):
        conn.execute(f"ALTER TABLE {_t} ADD COLUMN {_c} {_d}")
if not _has("posts"):
    conn.execute(POSTS_DDL)
elif "worker_id" not in _cols("posts"):  # old single-bot schema -> worker 1
    conn.execute("ALTER TABLE posts RENAME TO posts_old")
    conn.execute(POSTS_DDL)
    conn.execute("INSERT INTO posts SELECT 1, message_id, media_group_id, active, added_at "
                 "FROM posts_old")
    conn.execute("DROP TABLE posts_old")
conn.commit()

UNIT = "COALESCE(media_group_id, 'm' || message_id)"


# ---------- settings ----------

def get_setting(key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))
    conn.commit()


def default_interval():
    return int(get_setting("default_interval", 120))


def chat_interval(chat):
    return chat["interval"] or default_interval()


# ---------- workers ----------

def ensure_main_worker(token, bot_id, channel_ref):
    if get_worker(1):
        conn.execute("UPDATE workers SET token=?, bot_id=?, channel_ref=? WHERE id=1",
                     (token, bot_id, channel_ref))
    else:
        conn.execute("INSERT INTO workers (id,token,bot_id,name,channel_ref,enabled) "
                     "VALUES (1,?,?,?,?,1)", (token, bot_id, "Главный", channel_ref))
    conn.commit()


def add_worker(token, bot_id, username, channel_ref, channel_id):
    cur = conn.execute(
        "INSERT INTO workers (token,bot_id,username,name,channel_ref,channel_id,enabled) "
        "VALUES (?,?,?,?,?,?,1)", (token, bot_id, username, f"@{username}", channel_ref, channel_id))
    conn.commit()
    return cur.lastrowid


def get_worker(wid):
    return conn.execute("SELECT * FROM workers WHERE id=?", (wid,)).fetchone()


def worker_by_bot(bot_id):
    return conn.execute("SELECT * FROM workers WHERE bot_id=?", (bot_id,)).fetchone()


def list_workers():
    return conn.execute("SELECT * FROM workers ORDER BY id").fetchall()


def update_worker(wid, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE workers SET {sets} WHERE id=?", (*fields.values(), wid))
    conn.commit()


def delete_worker(wid):
    conn.execute("DELETE FROM sent WHERE chat_id IN (SELECT chat_id FROM chats WHERE worker_id=?)",
                 (wid,))
    for t in ("chats", "posts"):
        conn.execute(f"DELETE FROM {t} WHERE worker_id=?", (wid,))
    conn.execute("DELETE FROM workers WHERE id=?", (wid,))
    conn.commit()


# ---------- chats ----------

def set_link(chat_id, link):
    if link:
        conn.execute("UPDATE chats SET link=? WHERE chat_id=?", (link, chat_id))
        conn.commit()


def upsert_chat(chat_id, title, ctype, worker_id, active=1):
    row = conn.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    if row:
        conn.execute("UPDATE chats SET title=?, type=?, active=?, worker_id=? WHERE chat_id=?",
                     (title, ctype, active, worker_id, chat_id))
    else:
        conn.execute("INSERT INTO chats (chat_id,title,type,active,next_at,worker_id) "
                     "VALUES (?,?,?,?,?,?)",
                     (chat_id, title, ctype, active, time.time() + default_interval() * 60, worker_id))
    conn.commit()


def list_chats(active_only=True, worker_id=None):
    q, args = "SELECT * FROM chats WHERE 1=1", []
    if active_only:
        q += " AND active=1"
    if worker_id:
        q += " AND worker_id=?"
        args.append(worker_id)
    return conn.execute(q + " ORDER BY title", args).fetchall()


def get_chat(chat_id):
    return conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()


def update_chat(chat_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE chats SET {sets} WHERE chat_id=?", (*fields.values(), chat_id))
    conn.commit()


def due_chats():
    return conn.execute(
        "SELECT c.* FROM chats c JOIN workers w ON w.id=c.worker_id "
        "WHERE c.active=1 AND c.enabled=1 AND w.enabled=1 AND c.next_at<=?", (time.time(),)
    ).fetchall()


def migrate_chat(old, new):
    for t in ("chats", "sent", "log"):
        conn.execute(f"UPDATE OR REPLACE {t} SET chat_id=? WHERE chat_id=?", (new, old))
    conn.commit()


def delete_chat(chat_id):
    conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM sent WHERE chat_id=?", (chat_id,))
    conn.commit()


# ---------- posts (per worker) ----------

def add_post(worker_id, message_id, media_group_id):
    conn.execute("INSERT OR IGNORE INTO posts VALUES (?,?,?,1,?)",
                 (worker_id, message_id, media_group_id, time.time()))
    conn.commit()


def units_count(worker_id=None):
    q, args = f"SELECT COUNT(DISTINCT worker_id || '/' || {UNIT}) c FROM posts WHERE active=1", []
    if worker_id:
        q += " AND worker_id=?"
        args.append(worker_id)
    return conn.execute(q, args).fetchone()["c"]


def unit_ids(worker_id, unit):
    rows = conn.execute(
        f"SELECT message_id FROM posts WHERE worker_id=? AND active=1 AND {UNIT}=? "
        "ORDER BY message_id", (worker_id, unit)).fetchall()
    return [r["message_id"] for r in rows]


def unit_first(worker_id, unit):
    r = conn.execute(f"SELECT MIN(message_id) m FROM posts WHERE worker_id=? AND {UNIT}=?",
                     (worker_id, unit)).fetchone()
    return r["m"] if r else None


def deactivate_unit(worker_id, unit):
    conn.execute(f"UPDATE posts SET active=0 WHERE worker_id=? AND {UNIT}=?", (worker_id, unit))
    conn.commit()


def pick_unit(worker_id, chat_id):
    """Random unit not yet sent in the current cycle; when all are sent, start a new cycle."""
    q = (f"SELECT {UNIT} u FROM posts WHERE worker_id=? AND active=1 AND {UNIT} NOT IN "
         "(SELECT unit FROM sent WHERE chat_id=?) GROUP BY u ORDER BY RANDOM() LIMIT 1")
    r = conn.execute(q, (worker_id, chat_id)).fetchone()
    if not r:
        conn.execute("DELETE FROM sent WHERE chat_id=?", (chat_id,))
        conn.commit()
        r = conn.execute(q, (worker_id, chat_id)).fetchone()
    return r["u"] if r else None


def mark_sent(chat_id, unit):
    conn.execute("INSERT OR IGNORE INTO sent VALUES (?,?)", (chat_id, unit))
    conn.commit()


def list_units(worker_id, offset, limit):
    return conn.execute(
        f"SELECT {UNIT} u, MIN(message_id) first, COUNT(*) n FROM posts "
        "WHERE worker_id=? AND active=1 GROUP BY u ORDER BY first DESC LIMIT ? OFFSET ?",
        (worker_id, limit, offset)).fetchall()


def delete_unit(worker_id, unit):
    conn.execute(f"DELETE FROM posts WHERE worker_id=? AND {UNIT}=?", (worker_id, unit))
    conn.execute("DELETE FROM sent WHERE unit=? AND chat_id IN "
                 "(SELECT chat_id FROM chats WHERE worker_id=?)", (unit, worker_id))
    conn.commit()


def clear_posts(worker_id):
    conn.execute("DELETE FROM posts WHERE worker_id=?", (worker_id,))
    conn.execute("DELETE FROM sent WHERE chat_id IN (SELECT chat_id FROM chats WHERE worker_id=?)",
                 (worker_id,))
    conn.commit()


# ---------- log ----------

def add_log(worker, chat_id, title, unit, status, error=""):
    conn.execute(
        "INSERT INTO log (ts,worker_id,worker_name,chat_id,chat_title,unit,status,error) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), worker["id"], worker["name"], chat_id, title, unit, status, error))
    conn.commit()
