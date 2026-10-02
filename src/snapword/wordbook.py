"""生词本：用户主动存下来的词 + 存词那一刻的释义快照。

为什么和 `cache.py` 分开一个库：cache 是随时可以重算的缓存（删了不心疼），
生词本是**用户资产**（删了要心疼）。所以单独 `data\\wordbook.db`，随便备份、
随便拷走。表结构故意跟 cache 一样朴素：一个词一行。

    wb = Wordbook(cfg["wordbook"])
    wb.add(brief)                    # 存（已存在返回 False，不覆盖旧快照）
    wb.items()                       # 未掌握的在前、新存的在前
    wb.export_md(path) / export_csv(path)
"""
import csv
import json
import sqlite3
import threading
import time
from pathlib import Path


def key_of(brief):
    """一个词在生词本里的主键：词形（有 lexeme 用它，否则用查询原文），小写。"""
    b = brief or {}
    return str(b.get("lexeme") or b.get("query") or "").strip().lower()


class Wordbook:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + Lock：查词在 Job 线程里跑，界面在主线程里读。
        # 和 cache.py 同一个理由（GUI 和后台线程共用一个连接）。
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS words("
                " word TEXT PRIMARY KEY,"
                " at REAL,"                 # 存词时间
                " brief TEXT,"              # 释义快照（JSON）
                " mastered INTEGER DEFAULT 0)")
            self.conn.commit()

    # ------------------------------------------------------------- 写
    def add(self, brief):
        """存一个词。返回 True = 新存下来的，False = 本来就在里面（不覆盖旧快照）。"""
        w = key_of(brief)
        if not w:
            return False
        with self.lock:
            if self.conn.execute("SELECT 1 FROM words WHERE word=?", (w,)).fetchone():
                return False
            self.conn.execute(
                "INSERT INTO words(word, at, brief, mastered) VALUES(?,?,?,0)",
                (w, time.time(), json.dumps(brief or {}, ensure_ascii=False)))
            self.conn.commit()
            return True

    def remove(self, word):
        with self.lock:
            self.conn.execute("DELETE FROM words WHERE word=?", (str(word).lower(),))
            self.conn.commit()

    def clear(self):
        with self.lock:
            self.conn.execute("DELETE FROM words")
            self.conn.commit()

    def set_mastered(self, word, on=True):
        with self.lock:
            self.conn.execute("UPDATE words SET mastered=? WHERE word=?",
                              (1 if on else 0, str(word).lower()))
            self.conn.commit()

    # ------------------------------------------------------------- 读
    def has(self, word):
        with self.lock:
            return bool(self.conn.execute("SELECT 1 FROM words WHERE word=?",
                                          (str(word).lower(),)).fetchone())

    def count(self):
        with self.lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM words").fetchone()[0])

    def items(self):
        """未掌握的在前，同组里新存的在前（记词板就按这个顺序画）。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT word, brief, mastered, at FROM words "
                "ORDER BY mastered ASC, at DESC").fetchall()
        out = []
        for w, brief, mastered, at in rows:
            try:
                d = json.loads(brief or "{}")
            except ValueError:              # 快照坏了也要能看词本身
                d = {}
            out.append({
                "word": w, "brief": d, "mastered": bool(mastered), "at": at,
                "cn": "；".join(d.get("cn") or []),
                "phonetic": d.get("phonetic") or "",
                "en": d.get("en") or "",
            })
        return out

    # ------------------------------------------------------------- 导出
    def export_md(self, path):
        rows = self.items()
        out = ["# SnapWord 生词本", "",
               "共 %d 词（导出时间 %s）" % (len(rows), time.strftime("%Y-%m-%d %H:%M")), ""]
        for r in rows:
            head = "- **%s**%s" % (r["word"], "　✔ 已掌握" if r["mastered"] else "")
            if r["phonetic"]:
                head += " /%s/" % r["phonetic"]
            out.append(head)
            if r["cn"]:
                out.append("  - %s" % r["cn"])
            if r["en"]:
                out.append("  - %s" % r["en"])
        Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
        return len(rows)

    def export_csv(self, path):
        rows = self.items()
        # utf-8-sig：Excel 双击打开中文不乱码
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["word", "phonetic", "cn", "en", "mastered", "saved_at"])
            for r in rows:
                w.writerow([r["word"], r["phonetic"], r["cn"], r["en"],
                            "1" if r["mastered"] else "0",
                            time.strftime("%Y-%m-%d %H:%M", time.localtime(r["at"]))])
        return len(rows)
