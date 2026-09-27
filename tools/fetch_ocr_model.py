"""下载 OCR 模型到 `models\\<名字>\\`（默认两套都用得上，见 README）。

- **模型本体**（det/rec/dict）走 ModelScope 上的 `RapidAI/RapidOCR`：实测 ~5 MB/s，
  比 GitHub 快两个数量级。下完用上游返回的 **sha256** 校验，不对就删掉重来。
- **`lw.OpenCVDNN.PPOCR.dll`** 只有上游 GitHub release 里有（那个 zip 17MB，
  实测 ~34 KB/s，要 8 分钟），所以优先级是：
  ① `--dll-from <目录>` 指定的现成副本 → ② 仓库里 `helper\\lw.OpenCVDNN.PPOCR.dll`
  （已随仓库提供，MIT 许可）→ ③ 别的 `models\\*\\` 里已有的 → ④ 最后才去下那个 zip。

    python tools\\fetch_ocr_model.py                     # 快模型 ppocrv6-tiny（~5MB，秒下）
    python tools\\fetch_ocr_model.py --model ppocrv5-server   # 准模型（~165MB，半分钟）
    python tools\\fetch_ocr_model.py --check             # 只看现在有什么
    python tools\\fetch_ocr_model.py --dll-from D:\\some\\payload   # 用现成的 DLL，别去 GitHub 下

模型不提交进仓库（`.gitignore` 了 `models/`），下游各自下。
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(os.environ.get("SNAPWORD_HOME") or Path(__file__).resolve().parents[1])
MODELS_DIR = ROOT / "models"
CACHE_DIR = MODELS_DIR / ".cache"
DLL_NAME = "lw.OpenCVDNN.PPOCR.dll"

MS_API = "https://modelscope.cn/api/v1/models"
MS_REPO = "RapidAI/RapidOCR"
MS_REV = "master"
GH_ZIP = ("https://github.com/lxw112190/lw.OpenCVDNN.PPOCR/releases/download/"
          "v1.2.1.0/lw.OpenCVDNN.PPOCR-v1.2.1.0-win7-x64.zip")

# 名字 -> {角色: (仓库里的路径, 落盘文件名)}
MODELS = {
    "ppocrv6-tiny": {
        "det": ("onnx/PP-OCRv6/det/PP-OCRv6_det_tiny.onnx", "PP-OCRv6_tiny_det.onnx"),
        "rec": ("onnx/PP-OCRv6/rec/PP-OCRv6_rec_tiny.onnx", "PP-OCRv6_tiny_rec.onnx"),
        "dict": ("paddle/PP-OCRv6/rec/PP-OCRv6_rec_tiny/ppocrv6_tiny_dict.txt",
                 "PP-OCRv6_tiny_rec_dict.txt"),
    },
    "ppocrv5-server": {
        "det": ("onnx/PP-OCRv5/det/ch_PP-OCRv5_det_server.onnx", "PP-OCRv5_det_server.onnx"),
        "rec": ("onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_server.onnx", "PP-OCRv5_rec_server.onnx"),
        "dict": ("paddle/PP-OCRv5/rec/ch_PP-OCRv5_rec_server/ppocrv5_dict.txt",
                 "ppocrv5_dict.txt"),
    },
}


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "SnapWord/0.1"})
    return urllib.request.urlopen(req, timeout=timeout)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_dir(root):
    """仓库某个目录下的文件 -> {文件名: (路径, 大小, sha256)}。"""
    url = "%s/%s/repo/files?Revision=%s&Root=%s" % (MS_API, MS_REPO, MS_REV,
                                                    urllib.parse.quote(root))
    with _get(url, 40) as r:
        data = json.loads(r.read().decode("utf-8"))
    out = {}
    for f in (data.get("Data") or {}).get("Files") or []:
        if f.get("Size"):
            out[f["Name"]] = (f["Path"], f["Size"], (f.get("Sha256") or "").lower())
    return out


def download(path, dest, expect_size, expect_sha, force=False):
    if dest.exists() and dest.stat().st_size == expect_size and not force:
        if not expect_sha or sha256(dest) == expect_sha:
            print("  已有，跳过 %s" % dest.name)
            return True
    tmp = dest.with_suffix(dest.suffix + ".part")
    url = "%s/%s/repo?Revision=%s&FilePath=%s" % (MS_API, MS_REPO, MS_REV,
                                                  urllib.parse.quote(path))
    print("  下 %s（%.1f MB）" % (dest.name, expect_size / 1048576))
    done = 0
    with _get(url, 300) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if done % (16 << 20) < (1 << 20):
                sys.stdout.write("\r    %.1f/%.1f MB" % (done / 1048576, expect_size / 1048576))
                sys.stdout.flush()
    print("\r    %.1f MB 下完" % (done / 1048576))
    if done != expect_size:
        tmp.unlink(missing_ok=True)
        print("  !! 大小不对：拿到 %d，期望 %d" % (done, expect_size))
        return False
    have = sha256(tmp)
    if expect_sha and have != expect_sha:
        tmp.unlink(missing_ok=True)
        print("  !! sha256 不对：%s != %s" % (have, expect_sha))
        return False
    os.replace(tmp, dest)
    return True


def _local_dll(out, dll_from=None):
    """找一个现成的 DLL，别为了它去 GitHub 下 8 分钟。"""
    cands = []
    if dll_from:
        cands.append(Path(dll_from) / DLL_NAME)
        cands.append(Path(dll_from))            # 直接给了文件也认
    cands.append(ROOT / "helper" / DLL_NAME)    # 随仓库提供的那份
    if MODELS_DIR.is_dir():
        cands += sorted(MODELS_DIR.glob("*/" + DLL_NAME))
    for c in cands:
        try:
            if c.is_file() and c.stat().st_size > 1 << 20:
                return c
        except OSError:
            pass
    return None


def ensure_dll(out, dll_from=None, force=False):
    dest = out / DLL_NAME
    if dest.is_file() and dest.stat().st_size > 1 << 20 and not force:
        return True
    src = _local_dll(out, dll_from)
    if src and src != dest:
        shutil.copy2(src, dest)
        print("  拷了 %s（%.1f MB，来自 %s）" % (DLL_NAME, dest.stat().st_size / 1048576, src))
        return True
    # 最后才下上游 zip（慢），下过就缓存在 .cache 里，换模型不用再下第二遍
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE_DIR / os.path.basename(GH_ZIP)
    if not zip_path.is_file() or force:
        print("  本地没有 DLL，去 GitHub 下上游 release（17MB，很慢，实测 ~34 KB/s，请耐心）")
        tmp = zip_path.with_suffix(".part")
        done = 0
        with _get(GH_ZIP, 1800) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                sys.stdout.write("\r    %.1f MB" % (done / 1048576))
                sys.stdout.flush()
        print("\r    %.1f MB 下完" % (done / 1048576))
        os.replace(tmp, zip_path)
    want = DLL_NAME.lower()
    with zipfile.ZipFile(zip_path) as z:
        member = next((n for n in z.namelist() if n.lower().endswith(want)), None)
        if not member:
            print("  !! zip 里没有 %s" % DLL_NAME)
            return False
        with z.open(member) as src_f, open(dest, "wb") as out_f:
            shutil.copyfileobj(src_f, out_f)
    print("  从 release 里取出了 %s（%.1f MB）" % (DLL_NAME, dest.stat().st_size / 1048576))
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fetch_ocr_model",
                                 description="下载 SnapWord 用的 OCR 模型到 models\\")
    ap.add_argument("--model", default="ppocrv6-tiny", choices=sorted(MODELS))
    ap.add_argument("--out", default=None, help="默认 <项目>\\models\\<model>")
    ap.add_argument("--dll-from", default=None, help="现成的 %s 所在目录（省得去 GitHub 下）" % DLL_NAME)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true", help="只列现在有什么")
    a = ap.parse_args(argv)

    if a.check:
        if not MODELS_DIR.is_dir():
            print("还没有 models\\：" + str(MODELS_DIR))
            return 1
        for d in sorted(MODELS_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                print("%s/" % d.name)
                for p in sorted(d.iterdir()):
                    print("  %-34s %10d" % (p.name, p.stat().st_size))
        return 0

    out = Path(a.out) if a.out else MODELS_DIR / a.model
    out.mkdir(parents=True, exist_ok=True)

    print("ModelScope 上核对文件清单（%s）…" % a.model)
    cache, ok = {}, True
    for role, (remote, _local) in MODELS[a.model].items():
        d = os.path.dirname(remote)
        if d not in cache:
            cache[d] = list_dir(d)
        if os.path.basename(remote) not in cache[d]:
            print("!! 上游没有 %s" % remote)
            return 1
    for role, (remote, local) in MODELS[a.model].items():
        _p, size, sha = cache[os.path.dirname(remote)][os.path.basename(remote)]
        ok = download(remote, out / local, size, sha, a.force) and ok
    ok = ensure_dll(out, a.dll_from, a.force) and ok

    if not ok:
        print("\n有文件没下成，别用这个目录。")
        return 1
    print("\n好了：%s" % out)
    print("config.json 里对应的是：")
    print('  "ocr_dir"        = "%s"   （快模型）' % str(MODELS_DIR / "ppocrv6-tiny"))
    print('  "ocr_dir_strong" = "%s"   （准模型，没下也不影响，会自动跳过）'
          % str(MODELS_DIR / "ppocrv5-server"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
