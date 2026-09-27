"""按词缓存：同一个词只查一次（又快又省 token）。"""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    word   TEXT PRIMARY KEY,
    at     REAL NOT NULL,
    brief  TEXT,          -- 快路径结果（JSON）
    detail TEXT           -- 慢路径结果（Markdown），NULL = 还没要过
);
"""


class Cache:
    """查词是在后台线程里跑的，所以连接得跨线程用 —— sqlite3 默认不许，
    这里开 check_same_thread=False 并用一把锁串起来（同时只可能有一次查询）。"""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.db.execute(SCHEMA)
            self.db.commit()

    def get(self, word):
        with self._lock:
            row = self.db.execute(
                "SELECT at, brief, detail FROM lookups WHERE word = ?", (_key(word),)
            ).fetchone()
        if not row:
            return None
        return {
            "at": row[0],
            "brief": json.loads(row[1]) if row[1] else None,
            "detail": row[2],
        }

    def put_brief(self, word, brief):
        with self._lock:
            self.db.execute(
                "INSERT INTO lookups(word, at, brief) VALUES(?,?,?) "
                "ON CONFLICT(word) DO UPDATE SET at=excluded.at, brief=excluded.brief",
                (_key(word), time.time(), json.dumps(brief, ensure_ascii=False)),
            )
            self.db.commit()

    def put_detail(self, word, detail):
        with self._lock:
            self.db.execute(
                "INSERT INTO lookups(word, at, detail) VALUES(?,?,?) "
                "ON CONFLICT(word) DO UPDATE SET detail=excluded.detail",
                (_key(word), time.time(), detail),
            )
            self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()


def _key(word):
    return (word or "").strip().lower()
