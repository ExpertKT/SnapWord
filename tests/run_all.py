"""SnapWord 自我评测总入口：跑全量测试，按维度加权打分，输出报告。

    python tests\\run_all.py              # 跑一遍，打印维度分与总分
    python tests\\run_all.py --min 92     # 总分低于 92 就以退出码 1 失败（当回归闸门用）
    python tests\\run_all.py --out docs\\SELFTEST.md   # 顺手写一份报告

评分维度（总分 100）：可用性 60 + 特色性 40。
  a1 核心逻辑 10   a2 冷启动 8    a3 极端数据 12  a4 令牌一致性 8
  a5 可访问性 8    a6 状态完整 6  a7 键盘与习惯 8
  b1 光边 10       b2 自绘品牌 8  b3 质感 6       b4 入门引导 8   b5 动效 8

维度分 = 权重 × (该维度通过的断言数 / 该维度断言总数)。
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

WEIGHTS = {
    "a1": 10, "a2": 8, "a3": 12, "a4": 8, "a5": 8, "a6": 6, "a7": 8,
    "b1": 10, "b2": 8, "b3": 6, "b4": 8, "b5": 8,
}
NAMES = {
    "a1": "核心逻辑（查词/缓存/兜底）", "a2": "冷启动（能起来、贴边）",
    "a3": "极端数据（不溢出不压扁）", "a4": "令牌一致性（色/圆角/间距）",
    "a5": "可访问性（对比度/焦点环）", "a6": "状态完整（四态齐全）",
    "a7": "键盘与习惯（Esc/回车/反馈）",
    "b1": "特色·光边（三个表面）", "b2": "特色·自绘品牌（细边）",
    "b3": "特色·质感（渐变/受光）", "b4": "特色·入门引导（接 DS）",
    "b5": "特色·动效（都真跑完）",
}
GROUPS = {"可用性": [k for k in WEIGHTS if k.startswith("a")],
          "特色性": [k for k in WEIGHTS if k.startswith("b")]}


def run_core():
    """跑 test_core.py（它是脚本，用子进程拿结果）。"""
    try:
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "test_core.py")],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    except subprocess.TimeoutExpired:
        # 没有 timeout 的话，test_core 一挂这里就永远等下去，外面看就是"闸门卡死"
        return 0, 1, "test_core 跑超过 180 秒还没完（闸门不再干等）", ""
    line = [l for l in p.stdout.strip().splitlines() if "passed" in l]
    if not line:
        return 0, 1, (p.stdout + p.stderr).strip()[-400:]
    ok, total = line[-1].split()[0].split("/")
    # 光有比分没有名字，掉哪一项全靠猜 —— 掉项时把失败名一起带出去
    bad = [l.strip() for l in p.stdout.splitlines() if l.strip().startswith("[X]")]
    skip = [l.strip() for l in p.stdout.splitlines() if l.strip().startswith("[SKIP]")]
    return int(ok), int(total), "；".join(bad), "；".join(skip)


def run_ui():
    """跑 test_ui.py 的每个断言，按名字前缀归类（不让它自己 sys.exit）。

    逐条**实时**打印：整套要跑好几分钟，全憋到最后一次性输出的话，
    某一条卡住时你只能对着空白发呆，不知道卡在哪。
    """
    import test_ui
    res = {}
    for name, fn in test_ui.TESTS:
        dim = name.split("_")[1] if name.startswith("test_") else "?"
        ok, msg = True, ""
        t0 = time.monotonic()
        try:
            fn()
        except Exception as ex:
            ok, msg = False, "%s: %s" % (type(ex).__name__, ex)
        dt = time.monotonic() - t0
        print("  [%s] %-46s %.1fs" % ("OK" if ok else "X ", name, dt), flush=True)
        res.setdefault(dim, []).append((name, ok, msg))
    return res


def main():
    ap = argparse.ArgumentParser(description="SnapWord 自我评测")
    ap.add_argument("--min", type=float, default=0.0, help="总分低于这个值就返回 1")
    ap.add_argument("--out", default="", help="把报告写到这个文件")
    a = ap.parse_args()

    t0 = time.monotonic()
    ui = run_ui()
    t_ui = time.monotonic()
    core_ok, core_total, core_err, core_skip = run_core()
    t_core = time.monotonic()
    print("\n[耗时] UI 断言 %.0fs，test_core %.0fs" % (t_ui - t0, t_core - t_ui),
          flush=True)

    scores, lines, fails, skipped, notes = {}, [], [], [], {}
    for dim, w in sorted(WEIGHTS.items()):
        if dim == "a1":
            ok, total = core_ok, core_total
            detail = "test_core %d/%d" % (ok, total)
            if core_err:
                fails.append("a1 " + core_err)
            if core_skip:
                skipped.append("a1 " + core_skip)
            # 没验到和没验过是两回事，别混着写成"有未过项"
            notes[dim] = ("有未过项" if core_err
                          else "有未验证项" if core_skip else "全部通过")
        else:
            items = ui.get(dim, [])
            total = len(items)
            ok = sum(1 for _, good, _ in items if good)
            detail = ", ".join(n for n, good, _ in items if good) or "无断言"
            for n, good, msg in items:
                if not good:
                    fails.append("%s %s -> %s" % (dim, n, msg))
            notes[dim] = "全部通过" if ok == total else "有未过项"
        got = w * (ok / total) if total else 0.0
        scores[dim] = got
        lines.append("  %-3s %-26s %5.1f / %-3d  （%s）" % (dim, NAMES[dim], got, w, detail))

    total_score = sum(scores.values())
    print("============ SnapWord 自我评测 ============")
    for g, dims in GROUPS.items():
        print("\n[%s] 小计 %.1f / %d" % (g, sum(scores[d] for d in dims), sum(WEIGHTS[d] for d in dims)))
        for ln in lines:
            if ln.strip().split()[0] in dims:
                print(ln)
    print("\n总计 %.1f / 100" % total_score)
    if fails:
        print("\n未过项：")
        for f in fails:
            print("  - " + f)
    if skipped:
        print("\n未验证项（环境不具备，不是通过也不是失败）：")
        for s in skipped:
            print("  - " + s)

    if a.out:
        body = ["# SnapWord 自我评测报告\n",
                "> 本文件由 `tests\\run_all.py --out` 自动生成，别手改。\n",
                "> 这套断言只覆盖「我列出来的项」，是回归闸门不是审美裁判——"
                "发现新的丑，请先补断言，再改代码。\n",
                "| 维度 | 得分 | 满分 | 说明 |", "| --- | --- | --- | --- |"]
        for dim in sorted(WEIGHTS):
            body.append("| %s %s | %.1f | %d | %s |" % (
                dim, NAMES[dim], scores[dim], WEIGHTS[dim],
                notes.get(dim, "全部通过" if scores[dim] == WEIGHTS[dim] else "有未过项")))
        body.append("\n**总计 %.1f / 100**\n" % total_score)
        if fails:
            body.append("\n## 未过项\n")
            body += ["- " + f for f in fails]
        if skipped:
            body.append("\n## 未验证项（环境不具备，不等于通过）\n")
            body += ["- " + s for s in skipped]
        Path(a.out).write_text("\n".join(body), encoding="utf-8")
        print("\n报告已写入 %s" % a.out)

    hard_exit(1 if (a.min and total_score < a.min) else 0)


def hard_exit(code):
    """跑完必须真的结束 —— 而 Qt 建过窗口/动画之后，正常退出流程会**挂死**。

    实测：UI 断言 36s + test_core 3s 就全跑完了，进程却挂到 560 秒还不退（得手动 kill）。
    最小复现：只 `import PySide6` 建个窗口再 `os._exit(0)`，退出也要 26 秒+；
    不碰 Qt 的同款脚本 2.2 秒就退了。原因是 `os._exit` → ExitProcess 会跑一遍各 DLL 的
    DLL_PROCESS_DETACH，Qt（或它挂进去的某个钩子）在 detach 里卡住。
    `TerminateProcess` 不做这层卸载，直接终止 —— 实测 2.7 秒。
    闸门挂住 = 定时/CI 里卡死，所以这里只能走硬的；代价是缓冲区得自己先刷。
    """
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        # argtypes/restype 不声明的话，64 位句柄会被当 c_int 截断，调用静默失败
        # （第一次就是这样，白等了 26 秒）
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.TerminateProcess.restype = ctypes.c_int
        k.TerminateProcess(k.GetCurrentProcess(), int(code))
    except Exception:
        pass
    os._exit(code)      # 非 Windows / 调用失败时的兜底


if __name__ == "__main__":
    sys.exit(main())
