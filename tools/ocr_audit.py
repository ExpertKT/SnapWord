"""把 OCR 测试台的失败条目喂给**本地 9B**，让它说"病因 + 下一步"，写进报告。

这是 `ocr_bench.py` 缺的那一半：那边只做**判定和归类**（错成什么样，用正则分到
截断/少头/形近字…），归类到"下一步试什么"是**写死的文本**，不是反思。真正要看的是：
同样是"文字框被切碎"，10px Georgia 的英文和 1690x46 的整行中文，该试的东西不一样。

所以这里把每条失败的**事实**（期望、实得、引擎、耗时、图尺寸、启发式归类、用例名里
带的字体字号）交给本地 qwen3.5:9b，让它逐条给：

    病因：……
    下一步：……

**它说的话不是事实**，只是待验证的假设 —— 验证手段仍然是 `ocr_bench.py run` 的通过率：
AUDIT.md 里每个用例都跟着它的历史通过序列（`.` 通过 / `x` 失败），谁的建议真的有用，
跑一遍就看得出来。

    python tools\\ocr_audit.py                  # 把最近一次 run 的失败条目全反思一遍
    python tools\\ocr_audit.py --engine native  # 只看某个引擎的失败
    python tools\\ocr_audit.py --limit 3        # 先试 3 条（调 prompt 用）
    python tools\\ocr_audit.py --dry-run        # 只把 prompt 打出来，不叫模型

产物：`data\\ocr-bench\\AUDIT.md`（给人看）+ `data\\ocr-bench\\audit.jsonl`（历次记录）
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ROOT：环境变量优先，否则从本文件位置往上推 —— 不能写死盘符（仓库是公开的，
# 别人 clone 下来在任何路径都得能跑）。
ROOT = Path(os.environ.get("SNAPWORD_HOME") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "src"))

BENCH = ROOT / "data" / "ocr-bench"
CASES = BENCH / "cases"
HIST = BENCH / "history.jsonl"
AUDIT_JSONL = BENCH / "audit.jsonl"
AUDIT_MD = BENCH / "AUDIT.md"

SYS_PROMPT = (
    "你是一个 OCR（光学字符识别）工程师，负责分析识别失败的原因。"
    "说话要具体、可执行，不要客套，不要重复题目里已经给出的事实。"
    "只输出两行，格式必须是：\n病因：<一句话，指认最可能的机理>\n下一步：<一句话，具体到能照着做的动作>\n"
    "例：病因：图太扁，文字检测把整行切成了几块，只留了中间一块。"
    "下一步：喂之前给上下各补 24px 底色，再跑同一条用例比通过率。"
)


# --------------------------------------------------------------------------- 读历史

def png_size(path):
    """从 PNG 头里读宽高（不引 Pillow，就 16 个字节的事）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    except OSError:
        pass
    return None, None


def read_hist():
    if not HIST.is_file():
        return []
    out = []
    for line in HIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def latest_failures(hist, engine=None, only_case=None):
    """每个用例取**最新**那条记录，只留失败的（没写 expect 的判不了，跳过）。"""
    latest = {}
    for h in hist:
        c = h.get("case")
        if c:
            latest[c] = h
    out = []
    for c in sorted(latest):
        h = latest[c]
        if h.get("ok") or not (h.get("expect") or ""):
            continue
        if engine and h.get("engine") != engine:
            continue
        if only_case and only_case not in c:
            continue
        out.append(h)
    return out


def case_meta(name):
    p = CASES / name / "case.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def seq_of(hist, name, n=10):
    """这个用例最近几次的通过序列，`.` = 过、`x` = 挂 —— 建议有没有用就看它。"""
    return "".join("." if h.get("ok") else "x" for h in hist if h.get("case") == name)[-n:]


# --------------------------------------------------------------------------- 问模型

def build_prompt(rec, meta, hist):
    name = rec.get("case", "")
    w, h = png_size(CASES / name / "crop.png")
    kind = rec.get("kind") or "?"
    try:
        from ocr_bench import KINDS
        why, todo = KINDS.get(kind, ("其它", ""))
    except Exception:
        why, todo = "其它", ""

    # 这批失败是**强制**某个引擎跑出来的、还是 auto 自己挑的引擎？对病因的结论影响极大：
    # 强制 system 跑出来的错，正确的动作往往是"让 auto 别挑 system"，而不是"去修 system"。
    forced = (rec.get("engine") or "auto") != "auto"
    facts = [
        "用例名：%s" % name,
        "这次测试是指定用 `%s` 引擎跑的（auto = 便宜在前的阶梯：native 快模型 → 可疑才上大模型 → 系统引擎）"
        % rec.get("engine"),
        "实际用的引擎：%s，耗时 %s ms" % (rec.get("used"), rec.get("ms")),
        ("**注意：%s 是测试强制指定的，不是 auto 自己选的。病因要说清两件事："
         "①这个引擎在这张图上为什么错；②auto 的阶梯在这种情况下会不会选中它。"
         % rec.get("engine")) if forced
        else "这次是 auto 跑的，引擎是阶梯自己挑的。",
        "期望文字：%r" % rec.get("expect", ""),
        "实际文字：%r" % rec.get("got", ""),
        "这张图：%s" % ("%d×%d 像素（宽高比 %.1f:1）" % (w, h, w / h) if w else "尺寸未知"),
        "用例来路：%s，备注：%s" % (meta.get("origin") or "未知", meta.get("note") or "无"),
        "启发式归类：%s（%s）—— 这类通常的对策是：%s" % (kind, why, todo),
        "这个用例最近 %d 次的通过序列（. = 通过，x = 失败）：%s"
        % (len(seq_of(hist, name)), seq_of(hist, name)),
    ]
    user = ("下面是 OCR 的一次失败。请判断真正的病因，并给出下一步该试什么。\n\n"
            + "\n".join("- " + f for f in facts)
            + "\n\n按「病因：」「下一步：」两行回答。")
    return [{"role": "system", "content": SYS_PROMPT}, {"role": "user", "content": user}]


def parse_answer(text):
    """宽松解析：找不到标签就把整段当病因，绝不把模型说的话丢掉。

    模型经常把两个标签挤在同一行（实测 7 条里就有 1 条），所以先按标签**强行断行**
    再逐行看 —— 不然「下一步」会被整段吞掉，看起来像模型没给建议。
    """
    text = re.sub(r"\s*(下一步[：:]|建议[：:])", r"\n\1", text or "")
    cause, nxt = "", ""
    for line in text.splitlines():
        s = line.strip().lstrip("#*- ").strip()
        for pre in ("病因：", "病因:", "原因：", "原因:"):
            if s.startswith(pre):
                cause = s[len(pre):].strip()
        for pre in ("下一步：", "下一步:", "建议：", "建议:"):
            if s.startswith(pre):
                nxt = s[len(pre):].strip()
    if not cause and not nxt:
        cause = (text or "").strip()
    return cause, nxt


def ask(prov, messages, timeout):
    from snapword import providers
    t0 = time.time()
    txt = providers.ollama_chat(prov, messages, timeout=timeout)
    return txt, time.time() - t0


# --------------------------------------------------------------------------- 跑

def cmd_audit(a):
    hist = read_hist()
    if not hist:
        print("还没有历史。先跑：python tools\\ocr_bench.py run")
        return 1
    fails = latest_failures(hist, a.engine, a.case)
    if not fails:
        print("最近一次里没有失败条目（引擎过滤 = %s）—— 没得反思，挺好。" % (a.engine or "不过滤"))
        return 0
    if a.limit:
        fails = fails[:a.limit]

    from snapword import config, providers
    cfg = config.load()
    prov = cfg["providers"]["ollama"]

    if a.dry_run:
        for rec in fails:
            msgs = build_prompt(rec, case_meta(rec["case"]), hist)
            print("=" * 70)
            print("用例 %s" % rec["case"])
            print(msgs[1]["content"])
        print("=" * 70)
        print("--dry-run：只打 prompt，没叫模型。模型是 %s @ %s" % (prov["model"], prov["url"]))
        return 0

    # 没在跑就别让用户干等 —— 60 秒超时体验很差，先说清怎么办
    if not providers.ollama_up(prov["url"]):
        print("本地模型没在跑（%s）。起一下：ollama serve，然后确认有模型：ollama list"
              % prov["url"])
        return 1

    print("本地 %s @ %s，要反思 %d 条失败\n" % (prov["model"], prov["url"], len(fails)))
    recs, t_all = [], time.time()
    for i, rec in enumerate(fails, 1):
        name = rec["case"]
        print("[%d/%d] %-24s %-9s 期望 %r 实得 %r"
              % (i, len(fails), name, rec.get("used") or "-", rec.get("expect", ""),
                 rec.get("got", "")))
        msgs = build_prompt(rec, case_meta(name), hist)
        cause = nxt = ""
        err = ""
        try:
            txt, secs = ask(prov, msgs, a.timeout)
            cause, nxt = parse_answer(txt)
            print("        病因：%s" % (cause or "(空)"))
            print("        下一步：%s" % (nxt or "(空)"))
            print("        %.1fs" % secs)
        except Exception as ex:                      # 超时/空内容/连不上，都只算这一条没反思出来
            secs, err = 0.0, "%s: %s" % (type(ex).__name__, ex)
            print("        模型没答出来：%s" % err)
        recs.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "case": name,
                     "engine": rec.get("engine"), "used": rec.get("used"),
                     "expect": rec.get("expect", ""), "got": rec.get("got", ""),
                     "ms": rec.get("ms"), "kind": rec.get("kind"), "model": prov["model"],
                     "secs": round(secs, 1), "cause": cause, "next": nxt, "error": err})
        print()

    BENCH.mkdir(parents=True, exist_ok=True)
    with AUDIT_JSONL.open("a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    write_md(recs, hist, probed=(prov["model"], prov["url"]), secs=time.time() - t_all)

    done = [r for r in recs if r["next"]]
    print("反思完 %d 条，%d 条给出了下一步。总 %.1fs" % (len(recs), len(done), time.time() - t_all))
    if done:
        # 先试哪条？挑启发式归类出现最多的那些里的第一条 —— 修最普遍的错，收益最大
        cnt = {}
        for r in recs:
            cnt[r.get("kind") or "?"] = cnt.get(r.get("kind") or "?", 0) + 1
        top = max(cnt.items(), key=lambda kv: kv[1])[0]
        first = next((r for r in recs if r.get("kind") == top and r["next"]), done[0])
        print("建议先试（错得最多的 `%s`）：%s" % (top, first["next"]))
        print("试完照旧：python tools\\ocr_bench.py run —— 通过率涨了才算真修好。")
    print("报告：%s" % AUDIT_MD)
    return 0


def write_md(recs, hist, probed, secs):
    model, url = probed
    L = ["# OCR 失败反思（本地模型写的）", "",
         "自动生成，别手改 —— 手改请改 `tools/ocr_audit.py`。",
         "%s · 模型 `%s`（%s）· 反思 %d 条 · 花了 %.1fs"
         % (time.strftime("%Y-%m-%d %H:%M:%S"), model, url, len(recs), secs), "",
         "> 这是**本地模型看着事实猜的病因**，不是结论。它的用处是给方向：",
         "> 每条都跟着用例的历史通过序列（`.` 通过 / `x` 失败）和复现命令，",
         "> 照着试完跑一次 `python tools\\ocr_bench.py run`，通过率涨了才算数。", ""]
    for i, r in enumerate(recs, 1):
        name = r["case"]
        w, h = png_size(CASES / name / "crop.png")
        L += ["## %d. `%s`" % (i, name), "",
              "- 期望 `%s` → 实得 `%s`（引擎 %s，%s ms）"
              % (r.get("expect"), r.get("got"), r.get("used") or "-", r.get("ms")),
              "- 这张图：%s" % ("%d×%d" % (w, h) if w else "尺寸未知"),
              "- 启发式归类：`%s`" % (r.get("kind") or "?"),
              "- 最近几次：`%s`" % seq_of(hist, name),
              "- 复现：`python -m snapword.cli ocr \"data\\ocr-bench\\cases\\%s\\crop.png\" --engine %s`"
              % (name, r.get("used") or r.get("engine") or "auto"), ""]
        if r.get("error"):
            L += ["**模型没答出来**：%s" % r["error"], ""]
        else:
            L += ["- **病因**：%s" % (r.get("cause") or "(空)"),
                  "- **下一步**：%s" % (r.get("next") or "(空)"), ""]

    # 历次建议：同一条用例重复反思时，能看出"上次照它做有没有用"
    past = []
    if AUDIT_JSONL.is_file():
        for line in AUDIT_JSONL.read_text(encoding="utf-8").splitlines():
            try:
                past.append(json.loads(line))
            except ValueError:
                pass
    if past:
        L += ["## 历次建议（同一条用例多次反思时，对着上一条的建议看通过率有没有动）", "",
              "| 时间 | 用例 | 归类 | 模型给的下一步 |", "|---|---|---|---|"]
        for p in past[-40:]:
            L.append("| %s | `%s` | %s | %s |"
                     % (p.get("ts"), p.get("case"), p.get("kind") or "?",
                        (p.get("next") or p.get("error") or "")[:120]))
        L.append("")

    L += ["## 这套循环怎么转", "",
          "1. `python tools\\ocr_bench.py collect <名字> --expect \"...\"` 攒真实语料。",
          "2. `python tools\\ocr_bench.py run` 判定 + 归类，失败的在 `history.jsonl` 里。",
          "3. `python tools\\ocr_audit.py` ← **这一步**：本地模型逐条说病因和下一步（不联网、不花 token）。",
          "4. 照着改 `src/snapword/ocr.py` / `helper/Program.cs` / `lookup.score_ocr`，",
          "   再 `run` 一遍；这个文件里的历史通过序列就是「有没有用」的证据。",
          "5. 不涨就撤掉改动 —— 别把复杂度留在代码里。", ""]
    AUDIT_MD.write_text("\n".join(L), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ocr_audit",
                                 description="把 OCR 失败条目喂给本地模型，让它说病因和下一步")
    ap.add_argument("--engine", default=None, help="只看某个引擎（auto/native/system）的失败")
    ap.add_argument("--case", default=None, help="只看名字里含这个子串的用例")
    ap.add_argument("--limit", type=int, default=0, help="最多反思几条（默认全部）")
    ap.add_argument("--timeout", type=float, default=90.0, help="每条给模型多少秒（默认 90）")
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt，不叫模型")
    a = ap.parse_args(argv)
    return cmd_audit(a)


if __name__ == "__main__":
    sys.exit(main())
