"""离线词典：ECDICT 的 SQLite 版（表 stardict）。

查不到精确词时按常见词形变化退一步找原形 —— 词库只有一个 word 索引，
但 `exchange` 字段里带着 p/过去式/d/过去分词/i/现在分词/3/三单/s/复数/r/比较级/t/最高级，
所以原形 → 变体是免费的。
"""
import re
import sqlite3
import threading
from pathlib import Path

COLS = "word, phonetic, translation, definition, exchange, tag, collins, oxford, bnc, frq"


class Ecdict:
    def __init__(self, path):
        self.path = str(path)
        self.available = Path(self.path).exists()
        self.db = None
        self._lock = threading.Lock()
        if self.available:
            # 查词是在后台线程里跑的：sqlite3 默认不许跨线程用连接，
            # 这里开 check_same_thread=False 并用一把锁串起来（读一个词只要 0.1 ms）。
            self.db = sqlite3.connect(
                "file:%s?mode=ro" % self.path.replace("?", "%3f"), uri=True,
                check_same_thread=False)
            self.db.row_factory = sqlite3.Row

    def close(self):
        if self.db:
            with self._lock:
                self.db.close()

    def lookup(self, word):
        """精确 → 大小写不敏感 → 词形还原。找不到返回 None。"""
        w = (word or "").strip()
        if not w or not self.available:
            return None
        with self._lock:
            for cand in [w] + _lemmas(w):
                row = self.db.execute(
                    "SELECT %s FROM stardict WHERE word = ? COLLATE NOCASE LIMIT 1" % COLS,
                    (cand,)
                ).fetchone()
                if row:
                    entry = _entry(dict(row))
                    entry["asked"] = w
                    entry["matched"] = row["word"]
                    return entry
        return None


def _entry(r):
    return {
        "word": r["word"],
        "phonetic": (r["phonetic"] or "").strip(),
        "translation": (r["translation"] or "").strip(),
        "definition": (r["definition"] or "").strip(),
        "exchange": (r["exchange"] or "").strip(),
        "tag": [t for t in (r["tag"] or "").split() if t],
        "collins": r["collins"] or 0,
        "oxford": bool(r["oxford"]),
        "bnc": r["bnc"] or 0,
        "frq": r["frq"] or 0,
    }


_SUFFIX = [
    ("ies", "y"), ("ied", "y"), ("ier", "y"), ("iest", "y"),
    ("es", ""), ("ed", ""), ("ing", ""), ("ly", ""), ("s", ""),
    ("er", ""), ("est", ""),
]


def _lemmas(word):
    """粗糙但够用的还原：给候选，不保证对。宁可多试几个也要能命中。"""
    w = word.lower()
    out = []
    if not re.fullmatch(r"[a-z][a-z'\-]*", w):
        return out

    def add(x):
        if x and x != w and x not in out:
            out.append(x)

    for suf, rep in _SUFFIX:
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            stem = w[: -len(suf)]
            add(stem + rep)
            # running → run / stopped → stop（双写辅音）
            if rep == "" and len(stem) > 2 and stem[-1] == stem[-2]:
                add(stem[:-1])
            # making → make / hoped → hope
            if rep == "" and stem:
                add(stem + "e")
    return out
