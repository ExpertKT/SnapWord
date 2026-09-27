"""识别能力自迭代测试台 —— 框一块、存一张、认一次、比一比、反思一次。

为什么要它：OCR 的锅不在引擎（两个引擎各有一半强项，见 `docs\\OCR-NOTES.md`），
而在"我们怎么挑结果"。挑法只能靠**真实框选的图**来验证 —— 之前用合成的图自测
全过，用户一框就出半截词。所以这里的语料**必须是真框出来的**（`collect`），
每次跑都把人眼看到的那张图留在 `cases\\<名字>\\crop.png` 里，跑完写进 `history.jsonl`，
再按"错成什么样"归类，给出下一步该试什么的提示。

    python tools\\ocr_bench.py collect <名字> --expect "serendipity"   # 真框一块（交互）
    python tools\\ocr_bench.py add <图片> <名字> --expect "..."        # 拿现成图片加一条
    python tools\\ocr_bench.py run                                    # 全部跑一遍 + 归类 + 写报告
    python tools\\ocr_bench.py run --engine native                    # 只跑某个引擎
    python tools\\ocr_bench.py report                                 # 看通过率随时间怎么变

语料目录：`data\\ocr-bench\\cases\\<名字>\\{crop.png, case.json}`
历史：    `data\\ocr-bench\\history.jsonl`
报告：    `data\\ocr-bench\\REPORT.md`（给人看的）
"""
import argparse
import difflib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("SNAPWORD_HOME", r"F:\SnapWord"))
sys.path.insert(0, str(ROOT / "src"))

BENCH = ROOT / "data" / "ocr-bench"
CASES = BENCH / "cases"
HIST = BENCH / "history.jsonl"
REPORT = BENCH / "REPORT.md"

# 失败归类 → (人话解释, 下一步该试什么)。归类只看"错成什么样"，因为不同长相的错
# 指向的原因完全不同：截断/丢开头 = 文字框切歪了；等长替换 = 字符级认错；空 = 没框到字。
KINDS = {
    "empty":        ("一个字符都没读出来", "框是不是太紧/太偏？把留白调大再试；也可能是底色和字色太接近"),
    "single-char":  ("只读出一个字符", "最典型的是 native 把整块字切碎后只留了一块 —— 看是不是细长图"),
    "too-short":    ("读出来的不到原文一半", "文字框切错。试：给图上下各补 4px 白边、或者先放大 2 倍再喂"),
    "truncated":    ("从头开始但被截断（尾巴丢了）", "最后一行的检测框掉了或图被裁了 —— 检查留白与最大边缩放"),
    "missing-head": ("尾巴对但开头丢了", "同上，方向相反：图片顶部被切 / 第一行没被检测到"),
    "cjk-confusion": ("长度对但中文认错字", "system 引擎小字中文的通病 → 该走 native；确认 auto 有没有选对引擎"),
    "spacing":      ("字都对，只有空格不一样", "多半是分词/行合并的问题，可以只警告不判错"),
    "substitution": ("等长的字符替换", "锐度/缩放问题：先放大 2 倍、或关掉平滑再喂"),
    "mismatch":     ("其它不一致", "看不出规律就再多攒几条同类语料"),
}


# --------------------------------------------------------------------------- 公共

def norm(s):
    return " ".join((s or "").split())


# 真屏幕上的正文，标点/空格本来就会被"读成别的标点"或"吃掉"，
# 拿它判对错会把"其实读对了"算成失败。所以判定用更宽的尺子：
# 去掉所有空白 + 常见中英标点 + 大小写后再比。
_SQUASH = str.maketrans("", "", " \t\r\n\u3000，。、；：！？（）【】「」《》“”‘’——·,.;:!?()[]{}<>\"'-_/\\|~`")


def same(a, b):
    """判定"算不算读对"。**注意：字母认错仍然算错**（grep→qrep 就该挂）。"""
    return (a or "").translate(_SQUASH).lower() == (b or "").translate(_SQUASH).lower()


def cases():
    if not CASES.is_dir():
        return []
    out = []
    for d in sorted(CASES.iterdir()):
        if (d / "crop.png").is_file():
            meta = {}
            if (d / "case.json").is_file():
                meta = json.loads((d / "case.json").read_text(encoding="utf-8"))
            out.append((d.name, d / "crop.png", meta))
    return out


def load_cfg():
    from snapword import config
    from snapword.ecdict import Ecdict
    from snapword.lookup import Lookup
    cfg = config.load()
    lk = Lookup(cfg, dic=Ecdict(cfg["ecdict"]))
    return cfg, lk


def recognize(cfg, lk, path, engine):
    from snapword import ocr
    return ocr.recognize_file(cfg["ocr_helper"], str(path), engine,
                              ocr_dir=cfg.get("ocr_dir"),
                              ocr_dir_strong=cfg.get("ocr_dir_strong"),
                              judge=lk.score_ocr)


def classify(expect, got):
    """错成什么样 —— 返回 KINDS 里的键。"""
    e, g = norm(expect), norm(got)
    if not g:
        return "empty"
    if len(g) <= 1:
        return "single-char"
    if len(g) < len(e) * 0.5:
        return "too-short"
    if e.startswith(g):
        return "truncated"
    if e.endswith(g):
        return "missing-head"
    if g.replace(" ", "") == e.replace(" ", ""):
        return "spacing"
    cj_e = [c for c in e if "\u4e00" <= c <= "\u9fff"]
    cj_g = [c for c in g if "\u4e00" <= c <= "\u9fff"]
    if cj_e and len(cj_e) == len(cj_g) and cj_e != cj_g:
        return "cjk-confusion"
    # 剩下的大多是字符级认错（多一个字母、错一个字母）。用相似度兜，
    # 因为 serendipity→sereridipity 这种"多一个字"既不是截断也不是丢头。
    if difflib.SequenceMatcher(None, e, g).ratio() >= 0.6:
        return "substitution"
    if len(g) == len(e):
        return "substitution"
    return "mismatch"


# --------------------------------------------------------------------------- collect / add

def cmd_collect(a):
    """真框选一块屏，存下**真正喂给 OCR 的那张图**。"""
    from PySide6 import QtCore, QtGui, QtWidgets
    from snapword.gui import Selector, pixmap_png

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    scr = QtWidgets.QApplication.primaryScreen()
    shot = scr.grabWindow(0)
    box = {}

    sel = Selector(shot, scr.geometry(), shot.devicePixelRatio())

    def picked(rect, pm):
        box["rect"] = [rect.x(), rect.y(), rect.width(), rect.height()]
        box["png"] = pixmap_png(pm)
        app.quit()

    sel.picked.connect(picked)
    sel.cancelled.connect(app.quit)
    sel.showFullScreen()
    sel.raise_()
    app.exec()
    if "png" not in box:
        print("取消了，没存。")
        return 1
    return save_case(a.name, box["png"], a.expect, a.note, box["rect"],
                     shot.devicePixelRatio(), "真实框选")


def save_case(name, png_bytes, expect, note, rect, dpr, origin):
    d = CASES / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "crop.png").write_bytes(png_bytes)
    meta = {"name": name, "expect": expect or "", "note": note or "",
            "rect": rect, "dpr": dpr, "origin": origin,
            "size": len(png_bytes), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    (d / "case.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("存了：%s（%d 字节）expect=%r" % (d / "crop.png", len(png_bytes), expect or ""))
    if not expect:
        print("提醒：没写 expect 的用例只能看结果、判不了对错。")
    return 0


def cmd_add(a):
    src = Path(a.image)
    if not src.is_file():
        print("找不到图片：" + str(src))
        return 1
    return save_case(a.name, src.read_bytes(), a.expect, a.note, None, None, "现成图片")


# --------------------------------------------------------------------------- run

def cmd_run(a):
    cfg, lk = load_cfg()
    cs = cases()
    if not cs:
        print("还没语料。先 collect 几条：python tools\\ocr_bench.py collect <名字> --expect \"...\"")
        return 1
    engine = a.engine or cfg["ocr_engine"]
    rows, hist = [], []
    for name, path, meta in cs:
        if a.case and a.case not in name:
            continue
        expect = meta.get("expect") or ""
        try:
            r = recognize(cfg, lk, path, engine)
            got, used, ms, err = r.get("text") or "", r.get("engine"), r.get("ms"), ""
        except Exception as ex:                      # 引擎炸了也算一条失败
            got, used, ms, err = "", engine, 0, str(ex)
        ok = bool(expect) and same(got, expect)
        kind = "" if ok else (classify(expect, got) if expect else "no-expect")
        rows.append((name, expect, got, used, ms, ok, kind, err, meta))
        hist.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "case": name, "engine": engine,
                     "used": used, "expect": expect, "got": got, "ms": ms, "ok": ok,
                     "kind": kind, "note": meta.get("note", "")})

    print("引擎 = %s，用例 %d 条\n" % (engine, len(rows)))
    for name, expect, got, used, ms, ok, kind, err, meta in rows:
        print("%s %-22s %-9s %5sms  %r" % ("OK  " if ok else "FAIL", name, used or "-",
                                           ms, got))
        if not ok:
            print("      期望 %r" % expect)
            if kind in KINDS:
                why, todo = KINDS[kind]
                print("      归类 %s：%s → %s" % (kind, why, todo))
            if err:
                print("      报错 " + err)
    n_ok = sum(1 for r in rows if r[5])
    print("\n通过 %d/%d" % (n_ok, len(rows)))

    HIST.parent.mkdir(parents=True, exist_ok=True)
    with HIST.open("a", encoding="utf-8") as f:
        for h in hist:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")
    write_report(engine, rows)
    print("历史追加到 %s\n报告写到 %s" % (HIST, REPORT))
    return 0 if n_ok == len(rows) else 2


def write_report(engine, rows):
    all_hist = []
    if HIST.is_file():
        for line in HIST.read_text(encoding="utf-8").splitlines():
            try:
                all_hist.append(json.loads(line))
            except ValueError:
                pass
    # 每种错法出现过几次 —— 这就是"反思"的材料：哪种错最多，就先去修哪种
    kind_count = {}
    for h in all_hist:
        if h.get("kind") and h["kind"] != "no-expect":
            kind_count[h["kind"]] = kind_count.get(h["kind"], 0) + 1
    # 每个引擎的历史通过率
    by_engine = {}
    for h in all_hist:
        e = h.get("engine") or "?"
        t = by_engine.setdefault(e, [0, 0])
        t[0] += 1
        t[1] += 1 if h.get("ok") else 0

    L = ["# OCR 识别能力测试报告", "",
         "自动生成，别手改 —— 手改请改 `tools/ocr_bench.py`。最新一次：%s（引擎 `%s`）"
         % (time.strftime("%Y-%m-%d %H:%M:%S"), engine), "",
         "## 本次结果", "", "| 用例 | 引擎 | 毫秒 | 期望 | 实际 | 判定 |", "|---|---|---|---|---|---|"]
    for name, expect, got, used, ms, ok, kind, err, meta in rows:
        L.append("| %s | %s | %s | `%s` | `%s` | %s |"
                 % (name, used, ms, expect, got.replace("|", "\\|"),
                    "OK" if ok else "**FAIL**（" + kind + "）"))
    L += ["", "通过 %d/%d。" % (sum(1 for r in rows if r[5]), len(rows)), ""]

    if kind_count:
        L += ["## 错法统计（全部历史）", "", "| 错法 | 次数 | 大概是什么原因 | 下一步 |", "|---|---|---|---|"]
        for k, n in sorted(kind_count.items(), key=lambda kv: -kv[1]):
            why, todo = KINDS.get(k, ("其它", "再多攒几条同类语料"))
            L.append("| %s | %d | %s | %s |" % (k, n, why, todo))
        top = max(kind_count.items(), key=lambda kv: kv[1])[0]
        why, todo = KINDS.get(top, ("其它", "再多攒几条同类语料"))
        L += ["", "**最该先修的是 `%s`**（%s）：%s" % (top, why, todo), ""]

    if by_engine:
        L += ["## 各引擎历史通过率", "", "| 引擎 | 次数 | 通过 | 通过率 |", "|---|---|---|---|"]
        for e, (t, o) in sorted(by_engine.items()):
            L.append("| %s | %d | %d | %.0f%% |" % (e, t, o, 100.0 * o / t if t else 0))
        L.append("")

    L += ["## 怎么迭代", "",
          "1. `collect` 攒**真实框选**的图 —— 合成图骗过我们一次了，别再用它下结论。",
          "2. 跑 `run`，看上面的错法统计，挑出现最多的那一种。",
          "3. 改 `src/snapword/ocr.py` 的挑选/预处理（或 `lookup.score_ocr` 的打分）。",
          "4. 再跑 `run` —— 同一个用例的历史会一行行留下来，报告里的通过率就是「改了到底有没有用」的证据。",
          "5. 只有通过率真的涨了才算修好；不涨就把改动撤掉，别把复杂度留在代码里。", ""]
    REPORT.write_text("\n".join(L), encoding="utf-8")


def cmd_report(a):
    if not HIST.is_file():
        print("还没有历史。" + str(HIST))
        return 1
    hist = []
    for line in HIST.read_text(encoding="utf-8").splitlines():
        try:
            hist.append(json.loads(line))
        except ValueError:
            pass
    print("历史 %d 条，%s ~ %s\n" % (len(hist), hist[0]["ts"], hist[-1]["ts"]))
    by_engine, kind_count = {}, {}
    for h in hist:
        e = h.get("engine") or "?"
        t = by_engine.setdefault(e, [0, 0])
        t[0] += 1
        t[1] += 1 if h.get("ok") else 0
        if h.get("kind") and h["kind"] != "no-expect":
            kind_count[h["kind"]] = kind_count.get(h["kind"], 0) + 1
    for e, (t, o) in sorted(by_engine.items()):
        print("引擎 %-8s 跑 %3d 次，通过 %3d 次（%.0f%%）" % (e, t, o, 100.0 * o / t))
    for k, n in sorted(kind_count.items(), key=lambda kv: -kv[1]):
        print("错法 %-14s %d 次" % (k, n))
    for c in sorted({h["case"] for h in hist}):
        seq = [h for h in hist if h["case"] == c]
        print("\n%-22s %s" % (c, "".join("." if h.get("ok") else "x" for h in seq)))
        print("     %s" % " ".join(h.get("got", "") for h in seq[-3:]))
    return 0


# --------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="ocr_bench", description="识别能力自迭代测试台")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="真框选一块屏，存下这张图")
    p.add_argument("name")
    p.add_argument("--expect", default="")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("add", help="拿一张现成图片加一条用例")
    p.add_argument("image")
    p.add_argument("name")
    p.add_argument("--expect", default="")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("run", help="跑一遍，判定 + 归类 + 写报告")
    p.add_argument("--engine", default=None, help="auto / native / system，默认用配置里的")
    p.add_argument("--case", default=None, help="只跑名字里含这个子串的用例")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="看历史通过率怎么变")
    p.set_defaults(fn=cmd_report)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
