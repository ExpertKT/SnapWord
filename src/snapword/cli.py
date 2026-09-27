"""命令行入口 —— 主程序是 GUI，但这个 CLI 是它的"可跑的检查"。

    python -m snapword.cli probe                看看 OCR helper 能不能用
    python -m snapword.cli ocr <图片>           只识别，不出释义
    python -m snapword.cli look <词>            快路径
    python -m snapword.cli look <词> --detail   连慢路径一起
    python -m snapword.cli ask <词> <问题>      问小 AI
    python -m snapword.cli gui                  起悬浮窗
"""
import argparse
import json
import sys

from . import __version__, config, ocr, providers
from .cache import Cache
from .ecdict import Ecdict
from .lookup import Lookup


def build_lookup(cfg):
    return Lookup(cfg, dic=Ecdict(cfg["ecdict"]), cache=Cache(cfg["cache"]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="snapword",
                                description="SnapWord v%s —— 屏幕上看到陌生单词时快速查词" % __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe")
    p_ocr = sub.add_parser("ocr")
    p_ocr.add_argument("image")
    p_ocr.add_argument("--engine", default=None)

    p_look = sub.add_parser("look")
    p_look.add_argument("text")
    p_look.add_argument("--detail", action="store_true")
    p_look.add_argument("--no-net", action="store_true")
    p_look.add_argument("--json", action="store_true")

    p_ask = sub.add_parser("ask")
    p_ask.add_argument("text")
    p_ask.add_argument("question")
    p_ask.add_argument("--deepseek", action="store_true")

    sub.add_parser("gui")

    a = ap.parse_args(argv)
    cfg = config.load()

    if a.cmd == "probe":
        print(json.dumps(ocr.probe(cfg["ocr_helper"], cfg["ocr_engine"], cfg.get("ocr_dir")),
                         ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "ocr":
        # judge 传下去，auto 才会用词典在几个引擎/模型之间择优（跟 GUI 走同一条路）
        lk = build_lookup(cfg) if not a.engine else None
        r = ocr.recognize_file(cfg["ocr_helper"], a.image,
                               a.engine or cfg["ocr_engine"], ocr_dir=cfg.get("ocr_dir"),
                               ocr_dir_strong=cfg.get("ocr_dir_strong"),
                               judge=lk.score_ocr if lk else None)
        print(r["text"])
        print("-- %s %d ms" % (r.get("engine"), r["ms"]), file=sys.stderr)
        return 0

    if a.cmd == "look":
        lk = build_lookup(cfg)
        b = lk.brief(a.text, allow_network=not a.no_net)
        if a.json:
            print(json.dumps(b, ensure_ascii=False, indent=2))
        else:
            print(render_brief(b))
        if a.detail:
            d = lk.detail(b)
            print("\n" + (d or "（慢路径没拿到内容）"))
        return 0

    if a.cmd == "ask":
        lk = build_lookup(cfg)
        b = lk.brief(a.text)
        ans, src = lk.ask(b, a.question, escalate=a.deepseek)
        print("[%s]\n%s" % (src, ans))
        return 0

    if a.cmd == "gui":
        from . import gui
        return gui.run(cfg)


def render_brief(b):
    lines = ["%s  %s" % (b.get("query", ""), ("/ " + b["phonetic"]) if b.get("phonetic") else "")]
    if b.get("tags"):
        lines.append("标签: " + " ".join(b["tags"]))
    for s in b.get("cn") or []:
        lines.append("  " + s)
    if b.get("en"):
        lines.append("  EN: " + b["en"])
    if b.get("exchange"):
        lines.append("变化: " + b["exchange"])
    lines.append("来源: %s%s" % (b.get("source"), "  (缓存)" if b.get("cached") else ""))
    for n in b.get("notes") or []:
        lines.append("注: " + n)
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
