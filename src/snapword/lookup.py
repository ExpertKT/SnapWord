"""查词管线：快路径（词典/免费接口）→ 慢路径（LLM 详解）→ 答疑。

设计原则（用户拍板）：
  · 释义的权威源是词典（ECDICT）和有道，LLM 只负责"展开"；
  · 本地 9B 干粗活（初稿/答疑/格式化），免费的；
  · 只有"必须准且详细"的那一问才发给 DeepSeek；
  · 同一个词只查一次，结果进缓存。
"""
import re
import time

from . import ecdict as ecdict_mod
from . import providers

WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'\u2019\-]*$")

# OCR 打分（score_ocr）的约定：拿到这个分就说明"这段文本里每个拉丁词词典都认识"，
# 不用再问第二个引擎了。见 ocr.recognize_file 的 judge 参数。
OCR_STRONG = 10.0
_OCR_TOK = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]{0,40}")


DETAIL_SYS = (
    "你是一位英语词典编辑，服务对象是中国的大学英语学习者（词汇量约 4000~6000）。"
    "下面会给你一个词和它的权威词典释义，请据此写一份中文详解。"
    "硬规矩：不要改写或替换词典给的释义；不要编造词源，没把握就写「（语源待考）」；"
    "例句要自然、地道、贴近考试与日常；全部输出中文 Markdown，不要客套话。"
)

DETAIL_TPL = """词：{word}
词典释义（权威，不能改）：{cn}
英文释义：{en}
词形变化：{exchange}
{extra}
请按这六节输出，每节标题用 `## `：
1. 一句话核心意思（不超过 25 字）
2. 逐义项详解（每个义项：词性 + 中文解释 + 一句英文原句）
3. 常见搭配（4~6 条，`英文 - 中文`）
4. 例句（3 句，每句英文 + 紧跟中文翻译，最好带这个搭配）
5. 易混词辨析（和它最容易混的 1~2 个词各一句）
6. 记忆法（一句话）"""

CHAT_SYS = (
    "你是英语学习助教。用户正在看一个词，会就这个词、它的用法、近义词或句子提问。"
    "回答要短（一般 3~6 句）、直接、可执行，用中文讲，英文例子要地道。"
    "不知道就说不知道，不要编。"
)


class Lookup:
    def __init__(self, cfg, dic=None, cache=None):
        self.cfg = cfg
        self.dic = dic if dic is not None else ecdict_mod.Ecdict(cfg["ecdict"])
        self.cache = cache
        self._youdao = {}

    # ---------------- 快路径 ----------------
    def brief(self, text, allow_network=True):
        """立刻能给用户看的东西。先缓存，再离线词典，再有道，最后本地 9B 兜底。"""
        t = (text or "").strip()
        if not t:
            raise ValueError("空文本")

        if self.cache:
            hit = self.cache.get(t)
            if hit and hit.get("brief"):
                b = dict(hit["brief"])
                b["cached"] = True
                return b

        b = self._brief_offline(t) if self.is_word(t) else None
        if b is None and allow_network and self._on("youdao"):
            b = self._brief_youdao(t)
        if b is None and allow_network:
            b = self._brief_baidu(t)
        if b is None:
            b = self._brief_llm(t)
        if b is not None and self.cache:
            self.cache.put_brief(t, b)
        if b is None:
            b = {
                "kind": "word" if self.is_word(t) else "phrase",
                "query": t,
                "cn": [],
                "en": "",
                "phonetic": "",
                "exchange": "",
                "tags": [],
                "notes": ["没查到释义（离线词典没命中，联网也失败了）"],
                "source": "none",
            }
        return b

    def is_word(self, text):
        return bool(WORD_RE.match((text or "").strip()))

    def score_ocr(self, text):
        """给 OCR 读出的一段文本打分，用来在两个引擎的结果里挑更可信的那个。

        **为什么不能只比长度**：实测 PP-OCR 把 `serendipity` 从小字认成
        `sereridipity`（把词切两半再拼，多出一个字），比正确答案还长；光比长度会挑错。
        这个项目本来就有 77 万词的离线词典，**"查不查得到"才是硬信号**：
        真词 +10；词典不认识的"单词" **-1000**（够狠才对 —— 只要有一段认糊了，
        分数就该掉到 `OCR_STRONG` 以下，逼 `auto` 去问第二个引擎；
        代价是专有名词（SnapWord/Chromium 这种）也会被当成"不认识"，
        但那只是白跑一趟第二个引擎，结果不会更差）。光秃秃一个字母 -2
        （用户报的"只能识别出一个字母"就是这种噪声），长度只给一点点权重做同分时的取舍。

        中文没法查词典，**而且"词典里有几个英文真词"对中文文本完全不适用**：
        实测真实抓屏的 DSH 聊天正文（整条 1690x46），system 读出近乎正确的长句、
        native 只吐一个 `保`；可那句正确的中文里夹着 `DSH`/`SnapWheel`/`agent` 这些
        **词典里没有的产品名**，按 -1000 罚下去分数比一个字还低，`auto` 就会挑那个 `保`。
        所以两个补丁：① 只吐出 1~2 个字的直接判死（根本不算读出内容）；
        ② 以中文为主的文本里，夹的英文按 -3 轻罚（多半是产品名/命令，不是认糊）。
        """
        t = (text or "").strip()
        if not t:
            return -1e9
        cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
        latin = sum(1 for c in t if c.isascii() and c.isalpha())
        lines = [ln for ln in t.splitlines() if ln.strip()]
        if max((len(ln) for ln in lines), default=0) <= 2:
            return -60.0            # 只吐出 1~2 个字，肯定没读出东西（用户报的那个症状）
        score = min(len(t), 120) * 0.05
        unknown = 1000.0 if latin > cjk * 2 else 3.0
        for tok in _OCR_TOK.findall(t):
            if len(tok) == 1 and tok.lower() not in ("a", "i"):
                score -= 2.0
            elif self._known(tok):
                score += 10.0
            else:
                score -= unknown
        return score

    def _known(self, tok):
        """这个词离线词典里到底有没有（含词形还原）。词典不可用就一律当"不认识"。"""
        try:
            e = self.dic.lookup(tok)
        except Exception:
            return False
        return bool(e and e.get("matched"))

    def _brief_offline(self, t):
        e = self.dic.lookup(t)
        if not e:
            return None
        cn = [s.strip() for s in e["translation"].split("\n") if s.strip()]
        notes = []
        if e["matched"].lower() != t.lower():
            notes.append("词典匹配到原形：" + e["matched"])
        return {
            "kind": "word",
            "query": t,
            "lexeme": e["matched"],
            "phonetic": e["phonetic"],
            "cn": cn,
            "en": e["definition"],
            "exchange": e["exchange"],
            "tags": e["tag"],
            "collins": e["collins"],
            "oxford": e["oxford"],
            "notes": notes,
            "source": "ecdict",
        }

    def _brief_youdao(self, t):
        """离线词典没命中：有道顶上（它给的 basic.explains 就是词典式释义）。"""
        y = self._youdao_of(t)
        if not y:
            return None
        cn = list(y["explains"])
        if not cn and y["translation"]:
            cn = [y["translation"]]
        if not cn:
            return None
        return {
            "kind": "word" if self.is_word(t) else "phrase",
            "query": t,
            "phonetic": y["phonetic"],
            "cn": cn,
            "en": "",
            "exchange": "",
            "tags": [],
            "notes": ["有道"],
            "source": "youdao",
        }

    def _brief_baidu(self, t):
        """有道也没结果时用百度翻译（要自己在设置里填 appid + 密钥，没填就跳过）。"""
        b = (self.cfg["providers"].get("baidu") or {})
        if not (b.get("enabled") and b.get("appid") and b.get("key")):
            return None
        try:
            out = providers.baidu(t, b["appid"], b["key"], to=b.get("to") or "zh")
        except Exception:
            return None
        lines = [s.strip() for s in out.splitlines() if s.strip()]
        if not lines:
            return None
        return {
            "kind": "word" if self.is_word(t) else "phrase",
            "query": t,
            "phonetic": "",
            "cn": lines,
            "en": "",
            "exchange": "",
            "tags": [],
            "notes": ["百度翻译"],
            "source": "baidu",
        }

    def enrich(self, brief):
        """后台补一步：把有道的释义拿来和离线词典对照（不阻塞第一屏）。"""
        t = brief.get("lexeme") or brief.get("query")
        if not (t and self.is_word(t) and self._on("youdao")):
            return None
        return self._youdao_of(t)

    def _brief_llm(self, t):
        """词典和免费接口都没命中：让本地 9B 出个初稿，先把东西显示出来。"""
        p = self.cfg["providers"]["ollama"]
        # 先探一下再发请求：ollama 没在跑时这里是几秒的失败，而不是干等 60 秒超时。
        if not p.get("enabled") or not providers.ollama_up(p.get("url", "")):
            return None
        try:
            out = providers.ollama_chat(
                p,
                [
                    {"role": "system", "content": "你在做英汉词典的初稿。只输出释义本身，一行一个义项，"
                                                  "格式「词性. 中文释义」。查不到这个词就只输出：UNKNOWN"},
                    {"role": "user", "content": t},
                ],
                timeout=60,
            )
        except Exception as ex:
            return {
                "kind": "word" if self.is_word(t) else "phrase",
                "query": t, "cn": [], "en": "", "phonetic": "", "exchange": "", "tags": [],
                "notes": ["本地模型没答上：" + str(ex)[:120]],
                "source": "none",
            }
        if "UNKNOWN" in out.upper() and len(out) < 40:
            return None
        return {
            "kind": "word" if self.is_word(t) else "phrase",
            "query": t,
            "cn": [s.strip("-• ").strip() for s in out.splitlines() if s.strip()][:8],
            "en": "",
            "phonetic": "",
            "exchange": "",
            "tags": [],
            "notes": ["本地 9B 初稿（未经词典核对）"],
            "source": "ollama",
        }

    # ---------------- 慢路径 ----------------
    def detail(self, brief):
        """详细的展开。已经缓存过就直接给；否则本地 9B 出稿，DeepSeek 可用时用它定稿。"""
        word = brief.get("query") or brief.get("lexeme") or ""
        if self.cache:
            hit = self.cache.get(word)
            if hit and hit.get("detail"):
                return hit["detail"]

        msgs = self._detail_messages(brief)
        text, src = None, None
        ds = self.cfg["providers"]["deepseek"]
        use_ds = brief.get("source") == "ecdict" or brief.get("prefer") == "deepseek"
        if ds.get("enabled") and ds.get("key") and use_ds:
            try:
                text, src = providers.deepseek_chat(ds, msgs), "deepseek"
            except Exception:
                text = None
        if text is None:
            p = self.cfg["providers"]["ollama"]
            if p.get("enabled"):
                try:
                    text, src = providers.ollama_chat(p, msgs, timeout=300), "ollama"
                except Exception:
                    text = None
        if text is None:
            return None

        text = "> 详解来源：%s（词典释义为准）\n\n" % (
            "DeepSeek" if src == "deepseek" else "本地 9B（免费，仅供参考）"
        ) + text
        if self.cache:
            self.cache.put_detail(word, text)
        return text

    def _detail_messages(self, brief):
        cn = "\n".join(brief.get("cn") or []) or "（无）"
        extra = ""
        if brief.get("exchange"):
            extra = "词形变化：" + brief["exchange"] + "\n"
        if brief.get("tags"):
            extra += "考试标签：" + " ".join(brief["tags"]) + "\n"
        user = DETAIL_TPL.format(
            word=brief.get("lexeme") or brief.get("query"),
            cn=cn,
            en=brief.get("en") or "（无）",
            exchange=brief.get("exchange") or "（无）",
            extra=extra,
        )
        return [{"role": "system", "content": DETAIL_SYS}, {"role": "user", "content": user}]

    # ---------------- 答疑 ----------------
    def _chat_messages(self, brief, question, history=None):
        ctx = "用户正在看的词：%s\n词典释义：%s\n英文释义：%s" % (
            brief.get("lexeme") or brief.get("query"),
            "；".join(brief.get("cn") or []) or "（无）",
            brief.get("en") or "（无）",
        )
        msgs = [{"role": "system", "content": CHAT_SYS}, {"role": "system", "content": ctx}]
        msgs += list(history or [])
        msgs.append({"role": "user", "content": question})
        return msgs

    def ask_plan(self, brief, question, history=None, escalate=False):
        """这一问交给谁答，返回 (来源名, provider 配置, messages)。

        本地 9B 优先（免费，不花 token）；本地 ollama 没在跑就**自动**改成联网的 DeepSeek
        （只要填了 key）—— 没本地模型的机器上也照样能问。都没配就抛一句能照做的人话。
        """
        p = self.cfg["providers"]["ollama"]
        ds = self.cfg["providers"]["deepseek"]
        can_ds = bool(ds.get("enabled") and ds.get("key"))
        msgs = self._chat_messages(brief, question, history)
        if escalate and can_ds:
            return "deepseek", ds, msgs
        if p.get("enabled") and providers.ollama_up(p.get("url", "")):
            return "ollama", p, msgs
        if can_ds:
            return "deepseek", ds, msgs
        if not p.get("enabled"):
            raise RuntimeError(
                "没有能答话的模型：本地模型被关掉了，DeepSeek key 也没填。"
                "点右下角「设置…」填一个就能用。")
        raise RuntimeError(
            "本地模型（%s）没在跑，DeepSeek key 也没填。"
            "要么开 ollama serve，要么在「设置…」里填 DeepSeek key。"
            % p.get("url", ""))

    def ask(self, brief, question, history=None, escalate=False):
        """一次拿完整答案，返回 (文本, 来源)。CLI 和测试用这个。"""
        src, prov, msgs = self.ask_plan(brief, question, history, escalate)
        fn = providers.deepseek_chat if src == "deepseek" else providers.ollama_chat
        return fn(prov, msgs, timeout=180), src

    def ask_stream(self, brief, question, history=None, escalate=False):
        """流式版：先 yield ("src", 来源名)，再一段段 yield ("text", 片段)。"""
        src, prov, msgs = self.ask_plan(brief, question, history, escalate)
        yield ("src", src)
        fn = providers.deepseek_chat_stream if src == "deepseek" else providers.ollama_chat_stream
        for piece in fn(prov, msgs, timeout=300):
            if piece:
                yield ("text", piece)

    # ---------------- 小工具 ----------------
    def _on(self, name):
        return bool(self.cfg["providers"].get(name, {}).get("enabled"))

    def _youdao_of(self, text):
        if text not in self._youdao:
            try:
                self._youdao[text] = providers.youdao(text)
            except Exception:
                self._youdao[text] = None
        return self._youdao[text]
