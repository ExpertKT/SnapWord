"""外部数据源：本地 9B(Ollama) / DeepSeek / 有道 / 百度翻译 / MyMemory。

只用标准库（urllib），所以除了 PySide6 没有别的依赖。
有道和 MyMemory 的地址与参数是从 SnapWheel 的 57-Translate.cs 抄来的（同一个免费接口）。
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request


def _http(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def chat(url, key, model, messages, timeout=180, **extra):
    """OpenAI 兼容的 chat completions，返回正文文本。"""
    body = {"model": model, "messages": messages, "stream": False}
    body.update(extra)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    txt = _http(url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers, timeout)
    j = json.loads(txt)
    choices = j.get("choices") or []
    msg = (choices[0].get("message") if choices else None) or {}
    content = (msg.get("content") or "").strip()
    if not content:
        # 这是本机 Qwen3.5 的经典坑：思考模型不给 reasoning_effort=none 就回空 content。
        raise RuntimeError("模型返回了空内容（思考模型需 reasoning_effort=none）：" + txt[:200])
    return content


def ollama_chat(p, messages, timeout=180):
    # reasoning_effort=none 是这台机器上 qwen3.5 必带的（见 Ollama 部署记录「坑 1」）。
    return chat(p["url"], "", p["model"], messages, timeout=timeout, reasoning_effort="none")


def deepseek_chat(p, messages, timeout=180):
    if not p.get("key"):
        raise RuntimeError("没填 DeepSeek key")
    return chat(p["url"], p["key"], p["model"], messages, timeout=timeout)


def chat_stream(url, key, model, messages, timeout=180, **extra):
    """OpenAI 兼容的流式 chat：一段一段 yield 正文，让字先出来再等句子说完。

    服务端不认流式（或一个字都不吐）就退回一次性拿完整结果 —— 反正调用方的接口一样。
    """
    body = {"model": model, "messages": messages, "stream": True}
    body.update(extra)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers)
    got = False
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                j = json.loads(payload)
            except ValueError:
                continue
            choices = j.get("choices") or []
            delta = (choices[0].get("delta") if choices else None) or {}
            piece = delta.get("content") or ""
            if piece:
                got = True
                yield piece
    if not got:
        yield chat(url, key, model, messages, timeout=timeout, **extra)


def ollama_chat_stream(p, messages, timeout=180):
    return chat_stream(p["url"], "", p["model"], messages, timeout=timeout,
                       reasoning_effort="none")


def deepseek_chat_stream(p, messages, timeout=180):
    if not p.get("key"):
        raise RuntimeError("没填 DeepSeek key")
    return chat_stream(p["url"], p["key"], p["model"], messages, timeout=timeout)


_OLLAMA_UP = {}


def ollama_up(url, timeout=1.5, ttl=30):
    """本地模型在不在跑？问「问 AI」之前先问一句，免得用户干等一个 180 秒的超时。

    结果缓存 ttl 秒，这样连点几下不会每个问题都去探一遍。
    """
    now = time.time()
    hit = _OLLAMA_UP.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    root = url.split("/v1/")[0].rstrip("/")
    ok = False
    try:
        j = json.loads(_http(root + "/api/tags", timeout=timeout))
        ok = isinstance(j.get("models"), list)
    except Exception:
        ok = False
    _OLLAMA_UP[url] = (now, ok)
    return ok


def youdao(word, timeout=10):
    """有道免费 demo 接口。单词会带回 basic（音标 + 释义），句子只回译文。"""
    data = urllib.parse.urlencode({"q": word, "from": "en", "to": "zh-CHS"}).encode("utf-8")
    txt = _http(
        "https://aidemo.youdao.com/trans",
        data,
        {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        timeout,
    )
    j = json.loads(txt)
    basic = j.get("basic") or {}
    trans = j.get("translation") or []
    return {
        "phonetic": (basic.get("usphonetic") or basic.get("phonetic") or "").strip(),
        "explains": [s for s in (basic.get("explains") or []) if s],
        "translation": (trans[0] if trans else "") or "",
        "raw_senses": basic.get("exam_type") or [],
    }


def baidu(text, appid, key, to="zh", timeout=10):
    """百度翻译开放平台（通用文本翻译）。

    要自己去 https://fanyi-api.baidu.com 免费注册一个「通用文本翻译」应用，拿 appid + 密钥。
    签名规矩是 md5(appid + q + salt + 密钥)，别改顺序。
    """
    if not (appid and key):
        raise RuntimeError("没填百度翻译的 appid / 密钥")
    salt = str(int(time.time() * 1000))
    sign = hashlib.md5((appid + text + salt + key).encode("utf-8")).hexdigest()
    data = urllib.parse.urlencode({
        "q": text, "from": "auto", "to": to,
        "appid": appid, "salt": salt, "sign": sign,
    }).encode("utf-8")
    txt = _http(
        "https://fanyi-api.baidu.com/api/trans/vip/translate",
        data,
        {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        timeout,
    )
    j = json.loads(txt)
    if j.get("error_code"):
        raise RuntimeError("百度翻译报错 %s：%s" % (j["error_code"], j.get("error_msg", "")))
    out = "\n".join(s.get("dst", "") for s in (j.get("trans_result") or []) if s.get("dst"))
    if not out:
        raise RuntimeError("百度翻译没给结果")
    return out


def mymemory(text, src="en", dst="zh-CN", timeout=10):
    url = (
        "https://api.mymemory.translated.net/get?q="
        + urllib.parse.quote(text)
        + "&langpair="
        + src
        + "|"
        + dst
    )
    j = json.loads(_http(url, timeout=timeout))
    t = ((j.get("responseData") or {}).get("translatedText") or "").strip()
    if not t or "MYMEMORY WARNING" in t.upper():
        raise RuntimeError((j.get("responseDetails") or "MyMemory 限流/无结果"))
    return t
