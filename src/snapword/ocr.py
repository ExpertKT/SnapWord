"""取词：调用 helper\\ocr-helper.exe（由 SnapWheel 的 OCR 源码编出来的单文件）。

Python 这一侧只负责把一个图片文件递过去、把 JSON 读回来。

两个引擎、两套模型各自的脾气（都实测过，数字见 `docs/OCR-NOTES.md`）：
- `system`：Windows 自带的 WinRT OCR。快（~60-190ms），拉丁字够用，小字中文很差。
- `native`：本地 PP-OCR（`lw.OpenCVDNN.PPOCR.dll`）。模型是这个 DLL 自己从 `ocr_dir` 里
  按 `*det*.onnx` / `*rec*.onnx` / `*dict*.txt` 找的，所以换模型 = 换目录：
  - **tiny（PP-OCRv6 tiny）**：94ms 但通过 10/16，小字/细长拉丁会把文字框切错
    （`serendipity` → `sereridipity`、`idipity⏎C`）。
  - **PP-OCRv5 server**（`ocr_dir_strong`，165MB）：通过 **15/16**，上面那些全部读对，
    代价是每次调用 1.2s（一个进程一次调用，每次都要从磁盘加载 165MB）。
  helper 只认环境变量 `SNAPWHEEL_OCR_DIR`，所以这里由 Python 透传。
- `auto` = 按**便宜在前**的阶梯跑，**谁的分高用谁**（分由 `judge` 算，见下），
  拿到 `STRONG` 就收工。顺序：native+tiny → native+大模型 → system。
  于是"普通词 ~90ms 出结果，tiny 读得可疑时才花那 1.2 秒"，两头都不亏。

  为什么不能沿用"native 第一个非空结果就收工"：native 在小字/细长图上的**文字框会切错**，
  实测 `serendipity` 认成 `sereridipity` / `seren\nndipity`，甚至只吐半截 —— 但它是**非空**的，
  旧逻辑就把它当成了答案，用户看到的就是"框选识字只能识别出一个字母什么的"。

  `judge(text) -> float`：调用方给的打分函数（`lookup.Lookup.score_ocr`），
  约定 **>= `STRONG` 表示够可信**。不传就按长度算（CLI/没词典时）。
  注意 helper 自己的 `auto` 也不是这个语义：它只在「拿不到系统组件」时退到 native，
  「跑通了但一个字没认出来」那种情况不会兜底，所以那一层得我们自己写。
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

# judge 的约定：拿到这个分就认定结果可信，不再花第二个引擎的钱
STRONG = 10.0


class OcrError(RuntimeError):
    pass


def _env(ocr_dir):
    if ocr_dir and os.path.isdir(ocr_dir):
        e = dict(os.environ)
        e["SNAPWHEEL_OCR_DIR"] = str(ocr_dir)
        return e
    return None


def _run(helper, args, timeout, env=None):
    if not os.path.exists(helper):
        raise OcrError("找不到 OCR helper：" + str(helper))
    # CREATE_NO_WINDOW：helper 是个控制台程序，不抑制的话**每框选一次都闪一个黑框**
    # （实测用户报的就是这个）。加了之后 pythonw 下完全看不见。
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    p = subprocess.run([str(helper)] + list(args), capture_output=True, timeout=timeout,
                       env=env, creationflags=flags)
    out = p.stdout.decode("utf-8", "replace").strip()
    if not out:
        raise OcrError("helper 没有输出（stderr：%s）" % p.stderr.decode("utf-8", "replace")[:300])
    # native 引擎初始化时那个 DLL 会往 stdout 打两行作者横幅，JSON 不一定在第一行
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise OcrError("helper 输出里没有 JSON：" + out[:300])


def _pick(helper, path, steps, timeout, judge):
    """按顺序跑一串 (引擎, 模型目录)，返回 (最好的结果, 最后一个能拿到的结果)。

    - 打分只针对**非空**结果；空结果直接跳过（旧逻辑就是这里被 native 的半截文本骗了）。
    - 拿到 `STRONG` 分就收工 —— 顺序是按**便宜在前**排的，所以这一步决定了快路径有多快。
    """
    best, best_s, last = None, None, None
    for eng, env in steps:
        try:
            r = _run(helper, [str(path), "--engine", eng], timeout, env)
        except OcrError as ex:
            last = {"ok": False, "error": str(ex), "engine": eng}
            continue
        last = r
        text = (r.get("text") or "").strip()
        if not text:
            continue
        s = judge(text) if judge else float(len(text))
        if best is None or s > best_s:
            best, best_s = r, s
        if s >= STRONG:
            break
    return best, last


def _ladder(engine, ocr_dir, ocr_dir_strong):
    """auto 的阶梯：**快的先上，贵的最后**。

    实测（16 条语料，`tmp\\model_ab.py`）：PP-OCRv6 tiny 只要 94ms 但通过 10/16；
    PP-OCRv5 server 通过 15/16 但要 1182ms（一个进程一次调用，每次都得从磁盘加载 165MB 模型）。
    所以普通词走 tiny 就够快，只有 tiny 读得可疑时才去花那 1.2 秒。
    """
    steps = [("native", _env(ocr_dir))]
    big = _env(ocr_dir_strong) if ocr_dir_strong else None
    if big and big.get("SNAPWHEEL_OCR_DIR") != steps[0][1].get("SNAPWHEEL_OCR_DIR"):
        steps.append(("native", big))
    steps.append(("system", None))      # 永远可用的兜底，最便宜
    return steps


def probe(helper, engine="auto", ocr_dir=None):
    return _run(helper, ["--probe", "--engine", engine], 30, _env(ocr_dir))


def recognize_file(helper, path, engine="auto", timeout=90, ocr_dir=None, judge=None,
                   ocr_dir_strong=None):
    if engine == "auto":
        best, last = _pick(helper, path, _ladder(engine, ocr_dir, ocr_dir_strong), timeout, judge)
        if best is not None:
            return best
        if last and last.get("ok"):
            return last            # 跑通了但一个字没读到，交给调用方去说"没看到字"
        raise OcrError((last or {}).get("error") or "识别失败")
    r = _run(helper, [str(path), "--engine", engine], timeout, _env(ocr_dir))
    if r.get("ok"):
        return r
    raise OcrError(r.get("error") or "识别失败")


def recognize_png(helper, png_bytes, engine="auto", timeout=90, tmpdir=None, ocr_dir=None,
                  judge=None, ocr_dir_strong=None):
    """截屏拿到的 PNG 字节直接识别。临时文件放 F 盘，别往 C 盘塞。

    设了环境变量 `SNAPWORD_KEEP=<目录>` 的话，**把每次真正喂给 OCR 的那张图留一份**，
    这是 `tools\\ocr_bench.py` 攒语料的来源（语料必须是"真实框选出来的图"，
    自己合成的图骗过我们一次了）。
    """
    d = tmpdir or os.environ.get("SNAPWORD_TMP") or str(Path(helper).parent.parent / "data" / "tmp")
    Path(d).mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".png", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png_bytes)
        keep = os.environ.get("SNAPWORD_KEEP")
        if keep:
            try:
                Path(keep).mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                with open(tmp, "rb") as src:
                    data = src.read()
                (Path(keep) / ("crop-%s-%03d.png" % (stamp, int(time.time() * 1000) % 1000))
                 ).write_bytes(data)
            except OSError:
                pass
        return recognize_file(helper, tmp, engine, timeout, ocr_dir, judge, ocr_dir_strong)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
