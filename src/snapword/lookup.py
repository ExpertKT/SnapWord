"""查词管线：快路径（词典/免费接口）→ 慢路径（LLM 详解）→ 答疑。

设计原则（用户拍板）：
  · 释义的权威源是词典（ECDICT）和有道，LLM 只负责"展开"；
  · 模型按"免费/不出网在前"排队：本机 Ollama（装了才用）→ 填了 key 的联网模型 → 免费云端（kilo / pollinations）；
  · 免费云端不用注册不用 key，所以别人 clone 下来不做任何配置也能用详解和答疑；
  · 同一个词只查一次，结果进缓存。
"""
import functools
import re
import time

from . import ecdict as ecdict_mod
from . import providers

WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'\u2019\-]*$")

# OCR 打分（score_ocr）的约定：拿到这个分就说明"这段文本里每个拉丁词词典都认识"，
# 不用再问第二个引擎了。见 ocr.recognize_file 的 judge 参数。
OCR_STRONG = 10.0
_OCR_TOK = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]{0,40}")

# 详解最多等多久。原来写 300 秒 —— 模型冷加载 + 慢机器上那就是用户眼里的"卡死"，
# 而卡片上只有一句「生成中…」，分不清是在想还是已经死了。冷启动实测 13 秒。
DETAIL_TIMEOUT = 120

# 详解/问答来源的人话名字。卡片上要写清"这段话是谁说的"，尤其免费通道只是参考。
SOURCE_LABEL = {
    "deepseek": "DeepSeek",
    "ollama": "本地 9B（免费，仅供参考）",
    "kilo": "免费云端 kilo.ai（不用 key，仅供参考）",
    "pollinations": "免费云端 pollinations.ai（不用 key，仅供参考）",
}


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
        """立刻能给用户看的东西。先缓存，再离线词典，再有道，最后让模型兜底出个初稿。"""
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
        # 有道查不到这个词时会把**原词原样回显**（实测 zzqxyzzy → ['zzqxyzzy']）。
        # 那不是释义，拿它当结果就等于"识别出来的解释是英文"，而且会把后面的百度翻译、
        # 本地 9B 初稿全挡住（它们是按顺序兜底的）。所以原样回显一律视为未命中。
        if all(c.strip().lower() == t.strip().lower() for c in cn):
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
        """词典和免费接口都没命中：让模型出个初稿，先把东西显示出来。

        本地 9B 在跑就用它（免费、不出网）；没装本地模型的机器上退到免费云端
        （kilo / pollinations，都不用 key）—— 别人 clone 下来不该先被要求装个模型。
        """
        p = self.cfg["providers"]["ollama"]
        tries = []
        # 先探一下再发请求：ollama 没在跑时这里是几秒的失败，而不是干等 60 秒超时。
        if p.get("enabled") and providers.ollama_up(p.get("url", "")):
            tries.append((providers.ollama_chat, p, "ollama", "本地模型"))
        tries += [(functools.partial(providers.free_chat, name), prov, name, "免费云端")
                  for name, prov in self._free_list()]
        if not tries:
            return None
        msgs = [
            {"role": "system", "content": "你在做英汉词典的初稿。只输出释义本身，一行一个义项，"
                                          "格式「词性. 中文释义」。查不到这个词就只输出：UNKNOWN"},
            {"role": "user", "content": t},
        ]
        out, src, who, why = None, None, None, ""
        for call, prov, src, who in tries:
            try:
                # 免费通道两家各 45 秒（它们挂住时是一个字节都不吐）；最坏加起来仍在一分钟出头
                out = call(prov, msgs, timeout=45 if src in providers.FREE_ORDER else 60)
                break
            except Exception as ex:
                out, why = None, who + "没答上：" + str(ex)[:120]
        if out is None:
            return {
                "kind": "word" if self.is_word(t) else "phrase",
                "query": t, "cn": [], "en": "", "phonetic": "", "exchange": "", "tags": [],
                "notes": [why],
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
            "notes": [who + "初稿（未经词典核对）"],
            "source": src,
        }

    # ---------------- 慢路径 ----------------
    def detail(self, brief):
        """详细的展开。已经缓存过就直接给；否则拿词典释义去让模型出稿。

        谁出稿：本地 9B（装了才在）→ 填了 key 的联网模型（词典命中得好的词优先给它）→
        免费云端（kilo / pollinations，都不用 key，谁都能用）。
        """
        word = brief.get("query") or brief.get("lexeme") or ""
        if self.cache:
            hit = self.cache.get(word)
            if hit and hit.get("detail"):
                return hit["detail"]

        msgs = self._detail_messages(brief)
        p = self.cfg["providers"]["ollama"]
        ds = self.cfg["providers"]["deepseek"]
        free = self._free_list()
        can_ds = bool(ds.get("enabled") and ds.get("key"))
        use_ds = brief.get("source") == "ecdict" or brief.get("prefer") == "deepseek"
        local_up = bool(p.get("enabled")) and providers.ollama_up(p.get("url", ""))
        free_on = bool(free)

        # 一个模型都接不上就别去等超时：直接把词典里有的摊开，并说清怎么才能用上详解。
        # （这类用户没有本地 9B、也不打算去申请 DeepSeek key —— 不能让他们对着转圈发呆。）
        if not (can_ds or local_up or free_on):
            return self._detail_offline(brief, "")

        text, src, why = None, None, ""
        if can_ds and use_ds:
            try:
                text, src = providers.deepseek_chat(ds, msgs), "deepseek"
            except Exception as ex:
                why = "DeepSeek 没答上：" + str(ex)[:80]
        if text is None and local_up:
            try:
                text = providers.ollama_chat(p, msgs, timeout=DETAIL_TIMEOUT)
                src = "ollama"
            except Exception as ex:
                why = "本地模型没答上：" + str(ex)[:80]
        if text is None:
            for name, prov in free:
                try:
                    # 免费云端是公共端点，会直接挂住不吐字（实测 pollinations 后端 ENOSPC
                    # 时就这样），所以不给它和"自家 key / 本机模型"一样长的绳子；
                    # 一家挂了（限流/抽风）就换下一家，这正是排两家免费通道的意义 ——
                    # 45 秒 × 最多两家，最坏也比"对着正在整理…干等"短。
                    text, src = providers.free_chat(name, prov, msgs, timeout=45), name
                    break
                except Exception as ex:
                    why = "免费云端（%s）没答上：" % name + str(ex)[:80]
        if text is None:
            return self._detail_offline(brief, why)

        text = "> 详解来源：%s（词典释义为准）\n\n" % SOURCE_LABEL.get(src, src) + text
        if self.cache:
            self.cache.put_detail(word, text)
        return text

    def _detail_offline(self, brief, why):
        """接不上模型时的兜底详解（**不进缓存**：接上模型后要能重新生成真的）。

        为什么不能返回 None：卡片会显示"没能生成详解：模型没返回内容"，而真实原因
        八成是这台机器压根没接模型 —— 用户既看不懂，也不知道下一步该干什么。
        """
        out = ["> " + (why or "这台机器没接模型，先把词典里能给的都摊开"), ""]
        if brief.get("cn"):
            out += ["**词典释义**", ""] + ["- " + s for s in brief["cn"] if s.strip()] + [""]
        if brief.get("en"):
            out += ["**英文释义**", "", brief["en"], ""]
        if brief.get("exchange"):
            out += ["**词形变化**", "", brief["exchange"], ""]
        if brief.get("tags"):
            out += ["**考试标签**", "", " ".join(brief["tags"]), ""]
        out += ["---", "",
                "想要展开成六段详解（逐义项 / 搭配 / 例句 / 辨析 / 记忆法），任选一条：",
                "1. 免费云端：`config.json` 里 `providers.kilo.enabled`（备用 `providers.pollinations.enabled`）"
                " 保持 true 就行"
                "（不用注册、不用 key）—— 它没答上多半是网络问题，过会儿再试；",
                "2. 本机装 Ollama 跑个小模型（免费、不出网），SnapWord 会自动用它；",
                "3. 或在右下角「设置…」里填任意一家 OpenAI 兼容服务的 key（智谱 GLM-4-Flash、"
                "硅基流动也有免费档，DeepSeek 几块钱能用很久）。"]
        return "\n".join(out)

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

        本地 9B 在跑就优先用它（免费、不出网、不花 token）；没在跑就自动往下降：
        填了 key 的联网模型 → 免费云端（kilo / pollinations，都不用 key，开箱即用）。
        所以「没装本地模型」的机器上也照样能问答，而且不用先去申请什么。
        """
        p = self.cfg["providers"]["ollama"]
        ds = self.cfg["providers"]["deepseek"]
        free = self._free_list()
        can_ds = bool(ds.get("enabled") and ds.get("key"))
        msgs = self._chat_messages(brief, question, history)
        if escalate and can_ds:
            return "deepseek", ds, msgs
        if p.get("enabled") and providers.ollama_up(p.get("url", "")):
            return "ollama", p, msgs
        if can_ds:
            return "deepseek", ds, msgs
        if free:
            name, prov = free[0]
            return name, prov, msgs
        if not p.get("enabled"):
            raise RuntimeError(
                "没有能答话的模型：本地模型被关掉了，免费云端也关着，联网模型的 key 也没填。"
                "点右下角「设置…」填一个，或把 config.json 里 providers.kilo.enabled "
                "改回 true 就能用免费云端。")
        raise RuntimeError(
            "本地模型（%s）没在跑，免费云端也关着，联网模型的 key 也没填。"
            "要么开 ollama serve，要么把 config.json 里 providers.kilo.enabled 改回 "
            "true（免费、不用注册），要么在「设置…」里填一个 key。"
            % p.get("url", ""))

    def ask(self, brief, question, history=None, escalate=False):
        """一次拿完整答案，返回 (文本, 来源)。CLI 和测试用这个。"""
        src, prov, msgs = self.ask_plan(brief, question, history, escalate)
        free = src in providers.FREE_ORDER
        if src == "deepseek":
            fn = providers.deepseek_chat
        elif free:
            fn = functools.partial(providers.free_chat, src)
        else:
            fn = providers.ollama_chat
        return fn(prov, msgs, timeout=90 if free else 180), src

    def ask_stream(self, brief, question, history=None, escalate=False):
        """流式版：先 yield ("src", 来源名)，再一段段 yield ("text", 片段)。"""
        src, prov, msgs = self.ask_plan(brief, question, history, escalate)
        yield ("src", src)
        free = src in providers.FREE_ORDER
        if src == "deepseek":
            fn = providers.deepseek_chat_stream
        elif free:
            fn = functools.partial(providers.free_chat_stream, src)
        else:
            fn = providers.ollama_chat_stream
        # 免费公共端点给短一点的绳子：它挂住的时候是一个字节都不吐（不是慢慢吐），
        # 让用户对着"正在想…"等 300 秒是最糟的失败方式
        for piece in fn(prov, msgs, timeout=120 if free else 300):
            if piece:
                yield ("text", piece)

    # ---------------- 小工具 ----------------
    def _on(self, name):
        return bool(self.cfg["providers"].get(name, {}).get("enabled"))

    def _free_list(self):
        """零注册零 key 的免费云端那一排，按 providers.FREE_ORDER 的顺序。

        老配置文件里没有某一格就当它关着；两格都开着时先试 kilo（实测更抗造），
        它限流/抽风再退到 pollinations。
        """
        return [(name, self.cfg["providers"].get(name) or {})
                for name in providers.FREE_ORDER
                if (self.cfg["providers"].get(name) or {}).get("enabled")]

    def _youdao_of(self, text):
        if text not in self._youdao:
            try:
                self._youdao[text] = providers.youdao(text)
            except Exception:
                self._youdao[text] = None
        return self._youdao[text]
