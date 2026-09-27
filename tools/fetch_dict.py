"""下离线词库（ECDICT 全量：77 万条，182 MB）。

    python tools\\fetch_dict.py [--force]

走 npm 镜像里的 node-ecdict-sqlite-lastest 包 —— 实测 **41 MB/s**（1.5 秒下完）。
GitHub 那条路（release 里的 ecdict-sqlite-28.zip / 仓库里的 ecdict.csv）在国内只有
20~200 KB/s，而且要下 66~206 MB，别走。详见 DIRECTIONS.md「网络」。

产物：F:\\SnapWord\\data\\ecdict.db，表 stardict（列和 snapword/ecdict.py 对齐）。
"""
import os
import shutil
import sqlite3
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("SNAPWORD_HOME", r"F:\SnapWord"))
DB = ROOT / "data" / "ecdict.db"
TGZ = ROOT / "data" / "ecdict-npm.tgz"
URL = ("https://registry.npmmirror.com/node-ecdict-sqlite-lastest/-/"
       "node-ecdict-sqlite-lastest-0.0.1.tgz")
MEMBER = "package/db/ecdict.sqlite"
NEEDED = {"word", "phonetic", "definition", "translation", "exchange", "tag",
          "collins", "oxford", "bnc", "frq"}


def main(argv):
    force = "--force" in argv
    if DB.exists() and not force:
        n = _count(DB)
        if n > 100000:
            print("词库已经好了（%d 条）：%s。要重下加 --force。" % (n, DB))
            return 0

    ROOT.joinpath("data").mkdir(parents=True, exist_ok=True)
    print("下载 %s" % URL)
    t0 = time.time()
    got = 0
    with urllib.request.urlopen(URL, timeout=300) as r, open(TGZ, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            print("\r  %.1f / %.1f MB" % (got / 1048576, total / 1048576), end="", flush=True)
    print("\n  %.1f MB in %.1fs (%.1f MB/s)"
          % (got / 1048576, time.time() - t0, got / 1048576 / max(time.time() - t0, 0.01)))

    tmp = str(DB) + ".part"
    with tarfile.open(TGZ) as t:
        src = t.extractfile(t.getmember(MEMBER))
        with open(tmp, "wb") as f:
            shutil.copyfileobj(src, f, 1 << 20)
    os.replace(tmp, DB)
    os.remove(TGZ)

    missing = NEEDED - _columns(DB)
    if missing:
        print("词库表结构不对，缺列：%s" % missing)
        return 2
    print("完成：%d 条 -> %s（%.1f MB）" % (_count(DB), DB, DB.stat().st_size / 1048576))
    return 0


def _count(path):
    try:
        return sqlite3.connect(str(path)).execute("SELECT COUNT(*) FROM stardict").fetchone()[0]
    except sqlite3.Error:
        return 0


def _columns(path):
    db = sqlite3.connect(str(path))
    return {r[1] for r in db.execute("PRAGMA table_info(stardict)")}


if __name__ == "__main__":
    sys.exit(main(sys.argv))
