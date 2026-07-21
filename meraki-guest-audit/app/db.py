"""
db.py -- SQLite เก็บ audit log ถาวร (dedup เป็น "session")
session_key = mac | guest_email | sponsor_email | total_requested_time | authorized_hour
"""
import os
import sqlite3
import hashlib
from datetime import datetime, timezone, timedelta

DB_PATH = os.getenv("DB_PATH", "/data/guest_audit.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guest_sessions (
    session_key           TEXT PRIMARY KEY,
    guest_name            TEXT,
    guest_email           TEXT,
    sponsor_email         TEXT,
    client_mac            TEXT,
    client_ip             TEXT,
    client_id             TEXT,
    client_description    TEXT,
    ssid                  TEXT,
    auth_reason           TEXT,
    total_requested_time  INTEGER,
    authorized_label      TEXT,
    expires_label         TEXT,
    authorized_at         TEXT,
    expires_at            TEXT,
    first_captured_at     TEXT,
    last_captured_at      TEXT,
    raw_json              TEXT
);
CREATE INDEX IF NOT EXISTS idx_guest_email  ON guest_sessions(guest_email);
CREATE INDEX IF NOT EXISTS idx_first_cap     ON guest_sessions(first_captured_at);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _conn() as c:
        c.executescript(_SCHEMA)


def _make_key(rec: dict) -> str:
    # bucket authorized_at เป็นราย "ชั่วโมง" เพื่อให้ session เดิม upsert, session ใหม่เป็นแถวใหม่
    anchor = (rec.get("authorized_at") or "")[:13]  # YYYY-MM-DDTHH
    raw = "|".join([
        (rec.get("client_mac") or "").lower(),
        (rec.get("guest_email") or "").lower(),
        (rec.get("sponsor_email") or "").lower(),
        str(rec.get("total_requested_time") or ""),
        anchor,
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def upsert_sessions(records: list[dict]) -> list[dict]:
    """คืน list ของ record ที่เป็น 'ใหม่' (เพิ่งเห็นครั้งแรก) -> เอาไปยิง syslog"""
    now = datetime.now(timezone.utc).isoformat()
    new_rows = []
    with _conn() as c:
        for rec in records:
            key = _make_key(rec)
            exists = c.execute(
                "SELECT 1 FROM guest_sessions WHERE session_key=?", (key,)
            ).fetchone()
            if exists:
                c.execute(
                    """UPDATE guest_sessions
                       SET last_captured_at=?, client_ip=?, expires_label=?, expires_at=?, raw_json=?
                       WHERE session_key=?""",
                    (now, rec.get("client_ip"), rec.get("expires_label"),
                     rec.get("expires_at"), rec.get("raw_json"), key),
                )
            else:
                c.execute(
                    """INSERT INTO guest_sessions
                       (session_key, guest_name, guest_email, sponsor_email, client_mac,
                        client_ip, client_id, client_description, ssid, auth_reason,
                        total_requested_time, authorized_label, expires_label,
                        authorized_at, expires_at, first_captured_at, last_captured_at, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, rec.get("guest_name"), rec.get("guest_email"), rec.get("sponsor_email"),
                     rec.get("client_mac"), rec.get("client_ip"), rec.get("client_id"),
                     rec.get("client_description"), rec.get("ssid"), rec.get("auth_reason"),
                     rec.get("total_requested_time"), rec.get("authorized_label"),
                     rec.get("expires_label"), rec.get("authorized_at"), rec.get("expires_at"),
                     now, now, rec.get("raw_json")),
                )
                rec["_session_key"] = key
                new_rows.append(rec)
    return new_rows


COLUMNS = [
    "guest_name", "guest_email", "sponsor_email", "client_mac", "client_ip",
    "client_description", "ssid", "auth_reason", "total_requested_time",
    "authorized_at", "expires_at", "first_captured_at", "last_captured_at",
]


def query(q: str = "", date_from: str = "", date_to: str = "", limit: int = 5000):
    sql = "SELECT * FROM guest_sessions WHERE 1=1"
    args: list = []
    if q:
        sql += " AND (guest_email LIKE ? OR guest_name LIKE ? OR sponsor_email LIKE ? OR client_mac LIKE ?)"
        like = f"%{q}%"
        args += [like, like, like, like]
    if date_from:
        sql += " AND first_captured_at >= ?"; args.append(date_from)
    if date_to:
        sql += " AND first_captured_at <= ?"; args.append(date_to)
    sql += " ORDER BY first_captured_at DESC LIMIT ?"; args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM guest_sessions").fetchone()[0]


def stats() -> dict:
    with _conn() as c:
        r = c.execute(
            "SELECT COUNT(*) n, MIN(first_captured_at) oldest, MAX(first_captured_at) newest "
            "FROM guest_sessions"
        ).fetchone()
    return {"total": r["n"], "oldest": r["oldest"], "newest": r["newest"]}


def purge_older_than(days: int) -> int:
    """ลบ session ที่ first_captured_at เก่ากว่า <days> วัน -> คืนจำนวนที่ลบ
    days=0 = ลบทั้งหมด. VACUUM คืนพื้นที่ disk ให้ด้วย"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 0))).isoformat()
    with _conn() as c:
        cur = c.execute("DELETE FROM guest_sessions WHERE first_captured_at < ?", (cutoff,))
        deleted = cur.rowcount
    with _conn() as c:      # VACUUM ต้องอยู่นอก transaction
        c.execute("VACUUM")
    return deleted
