"""Settings: one JSON file at the project root (or `$SNAPWORD_HOME`).

项目要能 clone 到任何地方就跑起来，所以默认根目录是**从这个文件的位置推出来的**
（`src/snapword/config.py` 往上三层），而不是写死某个盘符。
`SNAPWORD_HOME` 仍然可以覆盖（测试和便携副本用）。
"""
import copy
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("SNAPWORD_HOME") or Path(__file__).resolve().parents[2])
PATH = ROOT / "config.json"

DEFAULTS = {
    "hotkey": "ctrl+alt+Q",             # 这台机器上 ctrl+alt+D / ctrl+alt+W 已经被别的软件占了
    "hotkey_selection": "ctrl+alt+S",   # 模拟 Ctrl+C 取划词；控制台窗口自动让路给框选
    "ocr_helper": str(ROOT / "helper" / "ocr-helper.exe"),
    "ocr_engine": "auto",               # auto = 便宜在前逐个试，拿离线词典打分择优（见 ocr._ladder）
    # 本地 PP-OCR 组件目录（lw.OpenCVDNN.PPOCR.dll + 模型）。helper 只认环境变量
    # SNAPWHEEL_OCR_DIR，所以这一项由 Python 喂给它；目录不存在时自动降级成只用系统引擎。
    # 模型是 DLL 自己按 *det*.onnx / *rec*.onnx / *dict*.txt 找的，换模型 = 换目录。
    # 两个目录都放在项目里的 models\（.gitignore 掉了，用 tools\fetch_ocr_model.py 下）。
    "ocr_dir": str(ROOT / "models" / "ppocrv6-tiny"),
    # 大模型（PP-OCRv5 server，165MB）：通过率 10/16 → 15/16，但一次 1.2s，
    # 所以只在 tiny 读得可疑时才用（`tools\fetch_ocr_model.py --model ppocrv5-server`；
    # 清空这项 = 关掉大模型，只跑 tiny）
    "ocr_dir_strong": str(ROOT / "models" / "ppocrv5-server"),
    "ecdict": str(ROOT / "data" / "ecdict.db"),
    "cache": str(ROOT / "data" / "cache.db"),
    # 记词板：存下来的词（卡片上点「存词」）。跟 cache 分开存 —— cache 是可以随时清掉的
    # 查询缓存，记词板是用户攒的东西，混在一起早晚会一起被清掉。
    "wordbook": str(ROOT / "data" / "wordbook.db"),
    # 卡片宽度：0 = 按屏幕自适应（可用宽的 45%，夹在 360~430）。拖过边之后写回这里，
    # 下次开卡片就照这个宽度。
    "card_width": 0,
    # 慢路径要不要自动跑（false = 卡片上按「详细解释」才跑）
    "auto_detail": False,
    # 屏幕右边缘那条常驻小面板：不依赖热键（框选/剪贴板/手输/设置/退出都能点出来）。
    # expanded = 这次是收起还是展开（你点一下就记下来）；y = 竖直位置，None = 居中。
    # 挂屏幕右边缘的常驻面板。y=None 表示竖直居中；screen=None 表示启动时取鼠标所在那块屏。
    "dock": {"enabled": True, "expanded": False, "y": None, "screen": None},
    "providers": {
        # 本地 9B：出初稿、答疑、格式化。免费。
        "ollama": {
            "enabled": True,
            "url": "http://127.0.0.1:11434/v1/chat/completions",
            "model": "qwen3.5:9b",
        },
        # 联网强模型：只用来出「必须准且详细」的那一问。
        "deepseek": {
            "enabled": False,
            "url": "https://api.deepseek.com/v1/chat/completions",
            "model": "deepseek-chat",
            "key": "",
        },
        "youdao": {"enabled": True},
        # 有道不够用时轮到的第二家翻译（要自己去 fanyi-api.baidu.com 免费领 appid + 密钥）。
        "baidu": {"enabled": True, "appid": "", "key": "", "to": "zh"},
        "mymemory": {"enabled": True},
    },
}


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load():
    cfg = copy.deepcopy(DEFAULTS)
    if PATH.exists():
        # utf-8-sig：记事本/PowerShell 存 UTF-8 会带 BOM，带上 json 直接解析失败
        _merge(cfg, json.loads(PATH.read_text(encoding="utf-8-sig")))
    # 空字符串不算配置：.env 里没填 key 时别把默认值顶掉
    for name, p in cfg["providers"].items():
        for k in list(p):
            if p[k] == "" and name == "deepseek" and k == "key":
                p["enabled"] = bool(p.get("key"))
    return cfg


def save(cfg):
    ROOT.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
