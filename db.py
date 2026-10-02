import sqlite3
import time

conn = sqlite3.connect("poster.db", check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript("""
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT,
    enabled INTEGER DEFAULT 1, interval INTEGER, next_at REAL DEFAULT 0,
    active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS posts (
    message_id INTEGER PRIMARY KEY, media_group_id TEXT, active INTEGER DEFAULT 1,
    added_at REAL);
CREATE TABLE IF NOT EXISTS sent (chat_id INTEGER, unit TEXT, PRIMARY KEY (chat_id, unit));
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat_id INTEGER, chat_title TEXT,
    unit TEXT, status TEXT, error TEXT);
""")

UNIT = "COALESCE(media_group_id, 'm' || message_id)"


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


def upsert_chat(chat_id, title, ctype, active=1):
    row = conn.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    if row:
        conn.execute("UPDATE chats SET title=?, type=?, active=? WHERE chat_id=?",
                     (title, ctype, active, chat_id))
    else:
        conn.execute("INSERT INTO chats (chat_id,title,type,active,next_at) VALUES (?,?,?,?,?)",
                     (chat_id, title, ctype, active, time.time() + default_interval() * 60))
    conn.commit()


def list_chats(active_only=True):
    q = "SELECT * FROM chats" + (" WHERE active=1" if active_only else "") + " ORDER BY title"
    return conn.execute(q).fetchall()


def get_chat(chat_id):
    return conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()


def update_chat(chat_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE chats SET {sets} WHERE chat_id=?", (*fields.values(), chat_id))
    conn.commit()


def due_chats():
    return conn.execute(
        "SELECT * FROM chats WHERE active=1 AND enabled=1 AND next_at<=?", (time.time(),)
    ).fetchall()


def migrate_chat(old, new):
    for t in ("chats", "sent", "log"):
        conn.execute(f"UPDATE OR REPLACE {t} SET chat_id=? WHERE chat_id=?", (new, old))
    conn.commit()


def add_post(message_id, media_group_id):
    conn.execute("INSERT OR IGNORE INTO posts VALUES (?,?,1,?)",
                 (message_id, media_group_id, time.time()))
    conn.commit()


def units_count():
    return conn.execute(
        f"SELECT COUNT(DISTINCT {UNIT}) c FROM posts WHERE active=1").fetchone()["c"]


def unit_ids(unit):
    rows = conn.execute(
        f"SELECT message_id FROM posts WHERE active=1 AND {UNIT}=? ORDER BY message_id", (unit,)
    ).fetchall()
    return [r["message_id"] for r in rows]


def deactivate_unit(unit):
    conn.execute(f"UPDATE posts SET active=0 WHERE {UNIT}=?", (unit,))
    conn.commit()


def pick_unit(chat_id):
    """Random unit not yet sent in the current cycle; when all are sent, start a new cycle."""
    q = (f"SELECT {UNIT} u FROM posts WHERE active=1 AND {UNIT} NOT IN "
         "(SELECT unit FROM sent WHERE chat_id=?) GROUP BY u ORDER BY RANDOM() LIMIT 1")
    r = conn.execute(q, (chat_id,)).fetchone()
    if not r:
        conn.execute("DELETE FROM sent WHERE chat_id=?", (chat_id,))
        conn.commit()
        r = conn.execute(q, (chat_id,)).fetchone()
    return r["u"] if r else None


def mark_sent(chat_id, unit):
    conn.execute("INSERT OR IGNORE INTO sent VALUES (?,?)", (chat_id, unit))
    conn.commit()


def add_log(chat_id, title, unit, status, error=""):
    conn.execute("INSERT INTO log (ts,chat_id,chat_title,unit,status,error) VALUES (?,?,?,?,?,?)",
                 (time.time(), chat_id, title, unit, status, error))
    conn.commit()


def list_units(offset, limit):
    return conn.execute(
        f"SELECT {UNIT} u, MIN(message_id) first, COUNT(*) n FROM posts WHERE active=1 "
        f"GROUP BY u ORDER BY first DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()


def delete_unit(unit):
    conn.execute(f"DELETE FROM posts WHERE {UNIT}=?", (unit,))
    conn.execute("DELETE FROM sent WHERE unit=?", (unit,))
    conn.commit()


def clear_posts():
    conn.execute("DELETE FROM posts")
    conn.execute("DELETE FROM sent")
    conn.commit()


def delete_chat(chat_id):
    conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM sent WHERE chat_id=?", (chat_id,))
    conn.commit()
