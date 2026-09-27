"""打一个可以直接给别人的发布包（zip）。

只装 **git 跟踪的文件**（所以不会把 182 MB 词库、165 MB 模型、你自己的 config.json
和注册表状态装进去），再额外生成两个纯 ASCII 的 cmd：

  setup.cmd    第一次用：建 venv → 装 PySide6 → 下词库/模型 → 编译 OCR helper
  SnapWord.cmd 启动（没装过会提示先跑 setup.cmd）

用法：
    python tools\\make_release.py                    # 输出到桌面
    python tools\\make_release.py -o D:\\out         # 输出到别处
    python tools\\make_release.py --list             # 只列出会打包哪些文件

为什么 cmd 里一个中文字都不能有：cmd.exe 按系统 OEM 代码页（中文机器是 936）逐字节读
脚本，UTF-8 的中文会被解码成乱码并当成命令执行 —— 这个坑本项目踩过一次。
"""
import argparse
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from snapword import __version__  # noqa: E402

SETUP_CMD = r"""@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo   SnapWord setup
echo ============================================
echo.

set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" where python >nul 2>nul && set PY=python
if "%PY%"=="" (
  echo [!] Python not found in PATH.
  echo     Install Python 3.10 or newer first: https://www.python.org/downloads/
  pause
  exit /b 1
)
echo Using Python: %PY%

if not exist ".venv\Scripts\python.exe" (
  echo Creating the virtual environment ^(.venv^) ...
  %PY% -m venv .venv
  if errorlevel 1 ( echo [!] could not create the venv & pause & exit /b 1 )
)
set VP=.venv\Scripts\python.exe

echo Installing PySide6 ^(about 77 MB, aliyun mirror^) ...
"%VP%" -m pip install -q --disable-pip-version-check PySide6-Essentials ^
  --index-url https://mirrors.aliyun.com/pypi/simple
if errorlevel 1 ( echo [!] pip failed & pause & exit /b 1 )

echo Hooking this folder into the venv ...
for %%I in ("%~dp0src") do set SRC=%%~fI
(echo %SRC%)> ".venv\Lib\site-packages\snapword.pth"

echo Downloading the offline dictionary ^(182 MB^) ...
"%VP%" tools\fetch_dict.py
if errorlevel 1 ( echo [!] dictionary download failed & pause & exit /b 1 )

echo Downloading the OCR model ...
"%VP%" tools\fetch_ocr_model.py
if errorlevel 1 ( echo [!] OCR model download failed & pause & exit /b 1 )

echo Building the OCR helper ^(uses the .NET Framework shipped with Windows^) ...
powershell -NoProfile -ExecutionPolicy Bypass -File helper\build-ocr.ps1
if errorlevel 1 ( echo [!] helper build failed & pause & exit /b 1 )

echo.
echo ============================================
echo   Done.  Double-click SnapWord.cmd to start.
echo ============================================
echo.
echo Optional: a slower but more accurate OCR model:
echo   "%VP%" tools\fetch_ocr_model.py --model ppocrv5-server
echo.
pause
"""

LAUNCH_CMD = r"""@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo SnapWord is not set up yet in this folder.
  echo Please run setup.cmd first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m snapword.gui
"""


def tracked_files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                         capture_output=True, check=True).stdout
    return [f.decode("utf-8") for f in out.split(b"\0") if f]


def build(out_dir: Path):
    files = tracked_files()
    top = "SnapWord-v%s" % __version__
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / ("%s.zip" % top)

    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            p = ROOT / rel
            if not p.is_file():
                continue
            zi = zipfile.ZipInfo.from_file(p, "%s/%s" % (top, rel.replace("\\", "/")))
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(p, "rb") as fh, z.open(zi, "w") as dst:
                dst.write(fh.read())
            total += 1
        z.writestr("%s/setup.cmd" % top, SETUP_CMD.replace("\n", "\r\n"))
        z.writestr("%s/SnapWord.cmd" % top, LAUNCH_CMD.replace("\n", "\r\n"))
        total += 2

    print("%s" % zip_path)
    print("%d 个文件，%.1f MB" % (total, zip_path.stat().st_size / 1048576))
    return zip_path


def main():
    ap = argparse.ArgumentParser(description="打一个发布 zip（只装 git 跟踪的文件）")
    default_out = Path(os.environ.get("USERPROFILE", "~")).expanduser() / "Desktop"
    ap.add_argument("-o", "--out", default=str(default_out), help="输出目录（默认桌面）")
    ap.add_argument("--list", action="store_true", help="只列出要打包的文件")
    a = ap.parse_args()
    if a.list:
        for f in tracked_files():
            print(f)
        return 0
    build(Path(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
