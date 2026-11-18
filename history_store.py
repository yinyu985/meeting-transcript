"""Local transcript storage; no audio is saved."""
import os
import pathlib
import sqlite3
import time
import uuid
from contextlib import contextmanager

DB = pathlib.Path(os.environ.get("MEETING_HISTORY_DB", str(pathlib.Path.home() / "Library/Application Support/meeting-transcript/history.sqlite3")))


@contextmanager
def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY, started INTEGER NOT NULL, ended INTEGER
    );
    CREATE TABLE IF NOT EXISTS segments (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
      at INTEGER NOT NULL, duration INTEGER NOT NULL, channel TEXT NOT NULL,
      text TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS segments_session ON segments(session_id, at);
    CREATE TABLE IF NOT EXISTS deleted_sessions (id TEXT PRIMARY KEY);
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def create():
    item = {"id": str(uuid.uuid4()), "started": round(time.time() * 1000)}
    with connect() as db:
        db.execute("INSERT INTO sessions(id,started) VALUES(:id,:started)", item)
    return item


def finish(sid):
    with connect() as db:
        cur = db.execute("UPDATE sessions SET ended=COALESCE(ended,?) WHERE id=?", (round(time.time()*1000), sid))
        if not cur.rowcount and not db.execute("SELECT 1 FROM deleted_sessions WHERE id=?", (sid,)).fetchone():
            raise ValueError("记录不存在")


def exists(sid):
    with connect() as db:
        return db.execute("SELECT id FROM sessions WHERE id=? UNION SELECT id FROM deleted_sessions WHERE id=?", (sid, sid)).fetchone() is not None


def append(sid, segment_id, at, duration, channel, text):
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM deleted_sessions WHERE id=?", (sid,)).fetchone():
            return  # 删除后返回的在途请求不能把记录重新写回来。
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            raise ValueError("记录不存在")
        db.execute("INSERT OR IGNORE INTO segments VALUES(?,?,?,?,?,?)", (segment_id, sid, at, duration, channel, text))


def listing():
    with connect() as db:
        return [dict(r) for r in db.execute("""
          SELECT s.*,COUNT(t.id) AS segments,
          MAX(t.at+t.duration) AS last_audio_end
          FROM sessions s LEFT JOIN segments t ON t.session_id=s.id AND length(trim(t.text)) > 0
          GROUP BY s.id ORDER BY s.started DESC
        """)]


def delete(sid):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            if db.execute("SELECT 1 FROM deleted_sessions WHERE id=?", (sid,)).fetchone():
                return
            raise ValueError("记录不存在")
        db.execute("INSERT OR IGNORE INTO deleted_sessions VALUES(?)", (sid,))
        db.execute("DELETE FROM segments WHERE session_id=?", (sid,))
        db.execute("DELETE FROM sessions WHERE id=?", (sid,))


def detail(sid):
    with connect() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["segments"] = [dict(r) for r in db.execute("SELECT at,duration,channel,text FROM segments WHERE session_id=? AND length(trim(text)) > 0 ORDER BY at,id", (sid,))]
        return result
