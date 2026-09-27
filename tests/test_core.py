"""无框架裸断言：直接跑 `python tests\\test_core.py`，全过则打印 OK。

只测那些"坏了就会静默出错"的地方：词形还原、缓存命不命中、快路径的兜底顺序。
不碰网络，也不依赖那 200 MB 的 ECDICT 真库 —— 用一张临时小表。
"""
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from snapword.cache import Cache          # noqa: E402
from snapword.ecdict import Ecdict        # noqa: E402
from snapword.lookup import Lookup        # noqa: E402

SCHEMA = """
CREATE TABLE stardict (
    word TEXT, phonetic TEXT, translation TEXT, definition TEXT,
    exchange TEXT, tag TEXT, collins INTEGER, oxford INTEGER, bnc INTEGER, frq INTEGER)
"""

ROWS = [
    ("run", "rʌn", "vi. 跑\nn. 奔跑", "to move fast on foot", "3:runs/i:running", "zk gk", 5, 1, 100, 200),
    ("serendipity", "ˌserənˈdɪpəti", "n. 意外发现珍奇事物的本领", "happy accident", "", "gre", 1, 0, 0, 9000),
]


def _tmpdir():
    d = tempfile.mkdtemp(prefix="snapword-test-")
    return d


def make_dict():
    path = os.path.join(_tmpdir(), "ecdict.db")
    db = sqlite3.connect(path)
    db.execute(SCHEMA)
    db.executemany("INSERT INTO stardict VALUES (?,?,?,?,?,?,?,?,?,?)", ROWS)
    db.commit()
    db.close()
    return path


def cfg_for(dict_path, cache_path):
    return {
        "ecdict": dict_path,
        "cache": cache_path,
        "providers": {
            "ollama": {"enabled": False},
            "deepseek": {"enabled": False, "key": ""},
            "youdao": {"enabled": False},
            "mymemory": {"enabled": False},
        },
    }


def test_exact_hit():
    d = Ecdict(make_dict())
    e = d.lookup("serendipity")
    assert e is not None, "精确命中失败"
    assert e["phonetic"] == "ˌserənˈdɪpəti"
    assert "意外发现" in e["translation"]
    assert e["tag"] == ["gre"]
    assert e["matched"] == "serendipity"


def test_case_insensitive():
    d = Ecdict(make_dict())
    assert d.lookup("Serendipity")["matched"] == "serendipity"


def test_lemma_running():
    d = Ecdict(make_dict())
    e = d.lookup("running")
    assert e is not None and e["matched"] == "run", "running 没还原成 run（拿到 %r）" % (e and e["matched"],)
    assert e["asked"] == "running"


def test_lemma_runs():
    d = Ecdict(make_dict())
    assert d.lookup("runs")["matched"] == "run"


def test_miss():
    d = Ecdict(make_dict())
    assert d.lookup("zzzznotaword") is None


def test_missing_db_is_not_a_crash():
    d = Ecdict(os.path.join(_tmpdir(), "nope.db"))
    assert d.available is False
    assert d.lookup("run") is None


def test_cache_roundtrip():
    c = Cache(os.path.join(_tmpdir(), "cache.db"))
    assert c.get("Run") is None
    c.put_brief("Run", {"query": "Run", "cn": ["vi. 跑"]})
    got = c.get("run")
    assert got and got["brief"]["cn"] == ["vi. 跑"], "缓存大小写不敏感取回失败"
    c.put_detail("run", "# 详解")
    assert c.get("run")["detail"] == "# 详解"


def test_brief_offline_and_cache():
    dd = make_dict()
    cd = os.path.join(_tmpdir(), "cache.db")
    cache = Cache(cd)
    lk = Lookup(cfg_for(dd, cd), dic=Ecdict(dd), cache=cache)
    b = lk.brief("running")
    assert b["source"] == "ecdict"
    assert b["lexeme"] == "run"
    assert b["cn"] and "跑" in b["cn"][0]
    assert "词典匹配到原形" in " ".join(b["notes"])

    b2 = lk.brief("running")
    assert b2.get("cached") is True, "第二次没吃到缓存"


def test_empty_text_rejected():
    lk = Lookup(cfg_for(make_dict(), os.path.join(_tmpdir(), "c.db")), dic=Ecdict(make_dict()))
    try:
        lk.brief("   ")
    except ValueError:
        return
    raise AssertionError("空文本应该报错")


def test_offline_miss_without_providers_returns_none_source():
    dd = make_dict()
    lk = Lookup(cfg_for(dd, os.path.join(_tmpdir(), "c.db")), dic=Ecdict(dd))
    b = lk.brief("zzzznotaword", allow_network=False)
    assert b["source"] == "none"
    assert b["notes"], "没查到要给出人话原因"


def test_dict_and_cache_are_usable_from_a_worker_thread():
    """查词全在后台线程里跑。连接忘了 check_same_thread=False 会当场炸
    （SQLite objects created in a thread can only be used in that same thread）——
    这个坑踩过两次，所以钉一个测试在这儿。"""
    dd = make_dict()
    cd = os.path.join(_tmpdir(), "cache.db")
    dic, cache = Ecdict(dd), Cache(cd)
    cache.put_brief("run", {"query": "run", "cn": ["vi. 跑"]})
    errs, out = [], {}

    def work():
        try:
            out["hit"] = dic.lookup("running")["matched"]
            out["cached"] = cache.get("Run")["brief"]["cn"]
        except Exception as ex:      # noqa: BLE001
            errs.append(ex)

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert not errs, "后台线程里用不了：%r" % (errs,)
    assert out["hit"] == "run", out
    assert out["cached"] == ["vi. 跑"], out


def test_input_struct_is_big_enough_for_sendinput():
    """cbSize 给错的话 SendInput 一个事件都不发，而且不报错 —— 划词会静默失效。

    Windows 的 INPUT union 里最大的是 MOUSEINPUT：x64 = 32 字节、x86 = 24，
    我们那个 union 只放了 ki（x64 上 24），所以必须自己补齐到 40 / 28。
    """
    import ctypes

    from snapword import winput

    assert ctypes.sizeof(winput._INPUT) in (28, 40), ctypes.sizeof(winput._INPUT)
    # VK_F24 什么功能都没有，按一下不影响任何人；结构不对时这里会得到 0
    seq = (winput._INPUT * 2)(winput._key(0x87), winput._key(0x87, True))
    n = winput.user32.SendInput(2, ctypes.byref(seq), ctypes.sizeof(winput._INPUT))
    assert n == 2, "SendInput 只接受了 %d 个事件（GetLastError=%d）" % (n, ctypes.get_last_error())


def test_ocr_reads_json_past_the_native_banner():
    """native 引擎初始化时那个 DLL 会先往 stdout 打两行作者横幅，JSON 不在第一行。"""
    d = _tmpdir()
    fake = os.path.join(d, "fake-helper.py")
    with open(fake, "w", encoding="utf-8") as f:
        f.write("import sys\n"
                "print('lw.OpenCVDNN.PPOCR 1.2.1.0')\n"
                "print(u'\\u4f5c\\u8005\\uff1a\\u5929\\u5929\\u4ee3\\u7801\\u7801\\u5929\\u5929')\n"
                "print('{\"ok\": true, \"engine\": \"native\", \"text\": \"serendipity\"}')\n")

    from snapword import ocr

    r = ocr._run(sys.executable, [fake], 30)
    assert r["ok"] and r["text"] == "serendipity", r


def test_chat_stream_yields_each_piece():
    """流式：服务端按 SSE 一段段发，我们一段段收 —— 界面才有「字一点点冒出来」。"""
    import http.server
    import json as _json
    from snapword import providers as P

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ["你好", "，", "世界"]:
                self.wfile.write(("data: %s\n\n" % _json.dumps(
                    {"choices": [{"delta": {"content": piece}}]})).encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = "http://127.0.0.1:%d/v1/chat/completions" % srv.server_address[1]
    try:
        out = list(P.chat_stream(url, "", "m", [{"role": "user", "content": "hi"}], timeout=10))
    finally:
        srv.server_close()
    assert out == ["你好", "，", "世界"], out


def test_ask_plan_prefers_local_then_falls_back_to_deepseek():
    """本地 9B 在跑就免费用它；没在跑但填了 DeepSeek key 就自动改成上网；都没有要说人话。"""
    from snapword import providers as P

    cfg = {"providers": {
        "ollama": {"enabled": True, "url": "http://127.0.0.1:11434/v1/chat/completions",
                   "model": "m"},
        "deepseek": {"enabled": True, "url": "u", "model": "d", "key": "k"},
    }}
    lk = Lookup(cfg, dic=object())          # ask_plan 不碰词典，给个占位就行
    brief = {"query": "run", "cn": ["跑"], "en": ""}

    old = P.ollama_up
    try:
        P.ollama_up = lambda *a, **k: True
        assert lk.ask_plan(brief, "问一句")[0] == "ollama"
        P.ollama_up = lambda *a, **k: False
        assert lk.ask_plan(brief, "问一句")[0] == "deepseek"      # 本地没跑 -> 自动上网
        assert lk.ask_plan(brief, "问一句", escalate=True)[0] == "deepseek"
        cfg["providers"]["deepseek"]["key"] = ""
        try:
            lk.ask_plan(brief, "问一句")
            raise AssertionError("一个模型都用不了的时候应该抛错")
        except RuntimeError as ex:
            assert "ollama serve" in str(ex), ex
    finally:
        P.ollama_up = old


def test_baidu_sign_is_md5_of_appid_q_salt_key():
    """百度翻译的签名顺序写错了它只回一个 error_code，所以这里钉住顺序。"""
    import hashlib
    import json as _json
    import urllib.parse as _up
    from snapword import providers as P

    seen = {}

    def fake_http(url, data=None, headers=None, timeout=30):
        seen.update(_up.parse_qs((data or b"").decode("utf-8")))
        return _json.dumps({"trans_result": [{"src": "hi", "dst": "你好"}]})

    old = P._http
    try:
        P._http = fake_http
        assert P.baidu("hi", "app", "sec", to="zh") == "你好"
    finally:
        P._http = old
    want = hashlib.md5(("app" + "hi" + seen["salt"][0] + "sec").encode("utf-8")).hexdigest()
    assert seen["sign"][0] == want, (seen["sign"], want)
    assert seen["q"][0] == "hi" and seen["to"][0] == "zh"


def test_score_ocr_prefers_the_real_word_over_the_longer_garbage():
    """OCR 择优只能靠词典，不能靠长度：native 把 serendipity 认成 sereridipity 时**更长**。"""
    from snapword.lookup import OCR_STRONG

    lk = Lookup(cfg_for(make_dict(), os.path.join(_tmpdir(), "c.db")), dic=Ecdict(make_dict()))
    good = lk.score_ocr("serendipity")
    bad = lk.score_ocr("sereridipity")
    assert good >= OCR_STRONG, good
    assert good > bad, (good, bad)
    assert lk.score_ocr("") < -1e8
    assert lk.score_ocr("s") < lk.score_ocr("run")


def test_ocr_auto_asks_the_second_engine_when_the_first_one_is_garbage():
    """auto 的择优：native 吐半截词就必须去问 system，并选 system 那个结果。

    这是用户报的「框选识字只能识别出一个字母」的根因 —— 旧逻辑"第一个非空就收工"。
    """
    from snapword import ocr

    lk = Lookup(cfg_for(make_dict(), os.path.join(_tmpdir(), "c.db")), dic=Ecdict(make_dict()))
    seen = []

    def fake_run(helper, args, timeout, env=None):
        eng = args[args.index("--engine") + 1]
        seen.append(eng)
        text = "sereridipity" if eng == "native" else "serendipity"
        return {"ok": True, "engine": eng, "text": text, "ms": 1}

    old = ocr._run
    try:
        ocr._run = fake_run
        r = ocr.recognize_file("h.exe", "x.png", "auto", 5, None, lk.score_ocr)
        assert seen == ["native", "system"], seen
        assert r["text"] == "serendipity", r
        # native 一次就够好时不应该白跑第二个引擎
        seen.clear()
        fake_run_ok = lambda h, a, t, e=None: (seen.append("native"),
                                               {"ok": True, "engine": "native",
                                                "text": "serendipity", "ms": 1})[1]
        ocr._run = fake_run_ok
        assert ocr.recognize_file("h.exe", "x.png", "auto", 5, None, lk.score_ocr)["text"] \
            == "serendipity"
        assert seen == ["native"], seen
    finally:
        ocr._run = old


def test_ocr_bench_classifies_what_went_wrong():
    """测试台的自查归类 —— 归类错了反思方向就错了，所以钉住几个代表。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ocr_bench", str(Path(__file__).resolve().parent.parent / "tools" / "ocr_bench.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cases = [("serendipity", "sereridipity", "substitution"),
             ("serendipity", "", "empty"),
             ("serendipity", "s", "single-char"),
             ("serendipity", "seren", "too-short"),
             ("serendipity", "serendip", "truncated"),
             ("serendipity", "dipity", "missing-head"),
             ("屏幕查词", "屏墓查词", "cjk-confusion"),
             ("SnapWord 屏幕查词", "SnapWord屏幕查词", "spacing"),
             ("serendipity", "the quick brown fox jumps", "mismatch"),
             ("serendipity", "serendipity", "")]
    for expect, got, want in cases:
        if want == "":
            continue
        have = mod.classify(expect, got)
        assert have == want, (expect, got, have, want)


def test_autostart_round_trip_leaves_registry_as_it_was():
    """开关一次要能干净地还原 —— 这个测试动的是真的 HKCU\\...\\Run。

    最要紧的是**别把用户自己开的开机启动搞没**：所以先把原状态记下来，测完还原。
    """
    if os.name != "nt":
        return
    import winreg
    from snapword import winput

    before = winput.autostart_get()
    try:
        assert winput.autostart_set(True) is True
        assert winput.autostart_enabled() is True
        cmd = winput.autostart_get()
        assert "pythonw.exe" in cmd.lower(), cmd        # 开机启动不能弹黑框
        assert "-m snapword.gui" in cmd, cmd
        assert winput.autostart_set(False) is True
        assert winput.autostart_enabled() is False
        # 删一个本来就不存在的值也不能炸
        assert winput.autostart_set(False) is True
    finally:
        winput.autostart_set(before is not None)        # 还原
        if before is not None:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, winput._RUN_KEY) as k:
                winreg.SetValueEx(k, winput._RUN_NAME, 0, winreg.REG_SZ, before)


TESTS = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]


def main():
    bad = 0
    for name, fn in TESTS:
        try:
            fn()
            print("  [OK] %s" % name)
        except Exception as ex:
            bad += 1
            print("  [X]  %s -> %s: %s" % (name, type(ex).__name__, ex))
    print("%d/%d passed" % (len(TESTS) - bad, len(TESTS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
