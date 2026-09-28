# OCR 这块的实测记录与迭代方法

> 这份文档是"识别为什么不给人确认感"这个问题的**证据档**。
> 每次改 OCR 挑选逻辑、或者跑出新的失败模式，都往这里补一行实测数据 —— 结论要能追到数字。
> 配套工具：`tools\ocr_bench.py`（语料 + 历史 + 报告）和 `tools\ocr_audit.py`（本机 9B 逐条说病因），
> 见本文第 6 节和第 6.1 节。

## 1. 症状

用户（m01495）："框选识字有时候只能识别出一个字母什么的，很奇怪，识别这一块还是有问题，
不能给人确认感。"

## 2. 复现：真实字号 × 中英文 × 两个引擎的矩阵

脚本 `tmp\ocr_matrix.py`（一次性，结果 `tmp\matrix.txt`）：5 段文本 × 字号
`[9,10,11,12,13,14,16,20,28]` × `{native, system}`，图是 Qt 现画的白底黑字（左右留 10px）。

**结论：两个引擎是互补的，而且各自都有稳定的短板。**

| 文本 | 字号 | native（PP-OCR） | system（WinRT） |
|---|---|---|---|
| `serendipity` | 10 | `serendipit` ❌ | `serendipity` ✅ |
| `serendipity` | 11 | `serer⏎idipity` ❌ | `serendipity` ✅ |
| `serendipity` | 13 | `seren⏎ndipity` ❌ | `serendipity` ✅ |
| `serendipity` | 20 | `idipity⏎C` ❌ | `serendipity` ✅ |
| `serendipity` | 28 | `serendipity` ✅ | `serendipity` ✅ |
| `the quick brown fox` | 9 | `the quick brown fox` ✅ | `the quick fox` ❌ |
| `the quick brown fox` | 20 | `th⏎juick brown fox` ❌ | `the quick brown fox` ✅ |
| `SnapWord 屏幕查词` | 9 | `SnapWord 屏幕查词` ✅ | `SnapWord屏荨彐司` ❌ |
| `SnapWord 屏幕查词` | 10 | `SnapWord 屏幕查词` ✅ | `SnapWord屏草查词` ❌ |
| `SnapWord 屏幕查词` | 12 | `SnapWord 屏幕查词` ✅ | `SnapWord屏墓查词` ❌ |
| `SnapWord 屏幕查词` | 20 | `SnapWord 屏幕查词` ✅ | `SnapWord屏幕查词`（少空格）❌ |
| `屏幕查词` | 10 | `屏幕查词` ✅ | `屏草查词` ❌ |

- **native 强中文、弱小字拉丁**：它在细长图上会把**文字检测框切错**，把 `serendipity`
  切成两块再拼起来（`serer` + `idipity`），或者干脆只留一块（`idipity⏎C`）。
- **system 强拉丁、弱小字中文**：9~12px 的中文会认成同音/形近的错字。
- 所以**没有任何一个引擎够用**，问题在于"谁来挑"。

## 3. 根因：旧 `auto` 是"第一个非空就收工"

```python
for eng in ("native", "system"):
    if r["ok"] and r["text"].strip():
        return r          # ← native 在小字上吐了半截词，非空，于是直接成了答案
```
native 的残缺结果**是非空的**，所以 system 永远没机会上场。用户看到的"只识别出一个字母"
就是 native 把整块字切碎后只留下一小块。

## 4. 怎么挑：用离线词典打分，不能比长度

`sereridipity` 比正确的 `serendipity` **还长**，比长度会挑错。
项目自带 77 万词的 ECDICT，**"查不查得到"才是硬信号**（`lookup.Lookup.score_ocr`）：

| 情况 | 加分 |
|---|---|
| 词在词典里（含词形还原） | **+10** |
| 词典不认识的"单词" | **-1000** |
| 光秃秃一个字母（`a`/`i` 除外） | -2 |
| 长度 | `min(len, 120) * 0.05`（只做同分时的取舍） |

`-1000` 是**故意的**：只要有一段认糊了，分数就该掉到 `OCR_STRONG = 10` 以下，
逼 `auto` 去问第二个引擎。代价是专有名词（`SnapWord`、`Chromium`）也被当成"不认识"，
不过那只是白跑一趟第二个引擎，**结果不会更差**。

`oc.recognize_file(engine="auto")` 的规则：native 先跑 → 分数 ≥ `OCR_STRONG` 就收工
（正常词一次就够，快路径不变）→ 否则再跑 system → **谁分高用谁**。

中文查不了词典，分数只来自长度：两个引擎都只读出中文时分数几乎一样，
`_pick` 保留先跑的那个（native，实测中文更准）。

## 5. 给用户的"确认感"

卡片底部的备注会写 `识别自 本地 PP-OCR（124 毫秒）`（`gui.py` 的 `OCR_ENGINE_CN` +
`self._ocr_info`）—— 认错了至少知道该怪谁，反馈时也有话说。

## 6. 自迭代测试台 `tools\ocr_bench.py`

```
python tools\ocr_bench.py collect serendipity-小字 --expect "serendipity"   # 真框一块
python tools\ocr_bench.py add 某图.png 名字 --expect "..."                  # 拿现成图加一条
python tools\ocr_bench.py run                                              # 判定+归类+写报告
python tools\ocr_bench.py run --engine native                              # 只看某个引擎
python tools\ocr_bench.py report                                           # 通过率随时间
```

- 语料 `data\ocr-bench\cases\<名字>\{crop.png, case.json}`，历史 `data\ocr-bench\history.jsonl`，
  人看的报告 `data\ocr-bench\REPORT.md`。
- 截图用的是**真正喂给 OCR 的那张图**（`ocr.recognize_png` 的 `SNAPWORD_KEEP` 钩子会留一份）。
- 失败会按"错成什么样"归类（空 / 只有一个字 / 太短 / 截断 / 丢开头 / 中文错字 / 空格差 /
  字符替换），每一类都给出**下一步该试什么**；报告会把出现最多的那一类标成"最该先修"。
- **合成图会骗人**：一开始用合成图自测全过，用户一框就出半截词。所以 `collect` 真框出来的
  才算数，`add` 进来的合成用例只是让测试台一上线就有东西可跑。

### 6.1 归类之后的反思：`tools\ocr_audit.py`（本机 9B，2026-09-28 加）

归类是**正则**做的，它只看"错成什么样"，分不出同一类错的不同原因 —— 10px 英文和 1690×46
整行中文都归到"截断"，但该试的东西完全不同。于是把失败条目再交给本机 `qwen3.5:9b`：

```
python tools\ocr_audit.py                 # 最近一次 run 的失败条目，逐条给「病因/下一步」
python tools\ocr_audit.py --limit 3 --engine native --dry-run
```

- 进 prompt 的事实：期望/实得、引擎、耗时、**图的宽高比**、用例来路、正则归类、最近几次通过序列；
  并且**明确写清"这次是测试指定用 X 引擎跑的，不是 auto 选的"** —— 少了这句，模型会一本正经
  建议"去修 system 引擎"，而真正该动的是 `auto` 的阶梯（实测踩到过）。
- 全本地：走 `providers.ollama_chat`（必须带 `reasoning_effort:"none"`）；跑之前探活，没开就
  提示 `ollama serve`，不干等超时。
- 解析宽松：标签被模型挤进同一行也要切开；一个标签都没有就当整段是"病因"，不丢模型的原话。
- 产物 `data\ocr-bench\AUDIT.md` + `data\ocr-bench\audit.jsonl`（**都 gitignore**，expect 是截屏原文）。
- **它是假设不是事实**：实测它会编机制（"auto 因耗时 238ms 跳过了 native"）。验证手段仍是
  `ocr_bench.py run` 的通过率，AUDIT.md 末尾的「历次建议」表就是"照它做有没有用"的对照。

### 2026-09-27 首次基准（14 条合成用例，含上面矩阵里最容易翻车的格子）

| 引擎 | 通过 |
|---|---|
| `native` 单跑 | 9/14（64%） |
| `system` 单跑 | 7/14（50%） |
| **`auto` 择优** | **14/14（100%）** |

`report` 的输出一眼能看出择优在干活（`.` = 通过，`x` = 挂）：

```
serendipity-Georgia-10px   x..     serendipit → serendipity → serendipity
SnapWord_屏幕查词-…-9px     .x.     SnapWord 屏幕查词 → SnapWord屏荨彐司 → SnapWord 屏幕查词
```

每行依次是 `native`、`system`、`auto`：写死的单引擎必挂，择优两次都能救回来。

## 7. 真实屏幕上的字：DSH 聊天界面「认不出来」（2026-09-27 第二轮机）

用户 m01693：「我和你聊天的这个界面的代码的那种字体它很难正常识别」。

### 7.1 复现工具

`tmp\grab_dsh.py`：`PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)` + `CreateDIBSection`
把 **DSH 窗口本身**抓到 `tmp\dsh-window.png`（2578x1458）。不用 `grabWindow(0)`，
那是桌面合成结果、窗口被盖住就拍到别人、分层窗口还会整个漏掉。

### 7.2 关键发现：**长宽比**，不是字号也不是颜色

同一批像素：

| 喂进去的样子 | native（PP-OCR）读出来 |
|---|---|
| 整个窗口 2578x1458 | 整段中文**全对**（又快又准） |
| 裁成 `1690x46` 一条 | **只吐 1~2 个字**：`保` / `2` / `式` / `Y元⏎五⏎T2⏎2` |

**这就是用户报的「只能识别出一个字母」**。PP-OCR 的文字检测在
**极端长宽比（约 37:1）** 的图上直接失效。同理，`1560x30` 那种又扁又窄的（而且我给的那张
还把字的上下切掉了）两个引擎都没救。

### 7.3 修法：喂之前上下补边（已落盘）

`F:\SnapWord\helper\Program.cs` 新增 `const double PadRatio = 6.0;` 和 `PadForOcr()`：
高度 < 宽度/6 时用左上角像素色上下各补到宽高比 6:1 再交给 `Ocr.Recognize`；
JSON 多打 `iw/ih/pw/ph` 四个字段（补边前后尺寸，方便诊断）。
重建：`F:\SnapWord\helper\build-ocr.ps1`。

**效果（同一张文件，只是重建了 helper）**：

| 用例（真屏幕像素） | 补边前 | 补边后 |
|---|---|---|
| `dsh-line1-宽留白` 1690x46 | native `保` | native `它想调的那条路，在你的仓库里不存在。我在C:\…\SnapWheel\src\里 qrep 了--capture 和CaptureMode——0处。那个入口`（只错 `grep→qrep`） |
| `dsh-line4-宽留白` 1690x46 | native `保` | native 整句**全对** |

`prep_probe.py` 的逐变体对照（真裁图，只变预处理）：**上下补边**是最有效的一格
（补白 24px 比补底色还好一点，`grep` 都读对了）；**放大 2x/3x** 也行但 3x 会到 5070px
反而截断；**反色**让 native 直接吐空；**灰度**略差。

### 7.4 打分也得跟着改（否则择优会挑错）

`auto` 曾经挑中 native 的那个 `保` —— 因为正确的中文句子里夹着 `DSH`/`SnapWheel`/`agent`
这些词典里没有的产品名，按 -1000 罚下去比一个字还低。`lookup.score_ocr` 改了两处：
① 最长行 ≤ 2 个字 → 直接 `-60.0`；② `unknown = 1000.0 if latin > cjk * 2 else 3.0`
（以中文为主的文本里夹的英文按 -3 轻罚）。

### 7.5 补边后的基准（`tools\ocr_bench.py`）

16 条（14 合成 + 2 张 PrintWindow 真屏幕裁图）时：

| 引擎 | 通过 |
|---|---|
| `native` | 10/16 |
| `system` | 9/16 |
| **`auto`** | **15/16**（唯一挂的是 `grep→qrep` 那一个字母） |

再把 7.6 那张**真 `grabWindow(0)` 抓屏**的 `dsh-screen-line1` 也放进语料（17 条）：
**`auto` 15/17** —— 多出来的那条挂掉是理所当然的，它记录的就是下面还没解决的问题。

真实用例在 `data\ocr-bench\cases\`（`origin` = `dsh-window` / `screen-grab`）。
**expect 必须照着裁图像素读**：我第一次凭记忆写错了两处（漏了「你」、把「绘制密集型」
写成「给模型装的东西」），两个引擎却一致地读出正确内容 —— 那就说明是我错。
另外判定放宽了：`ocr_bench.same()` 会去掉空白和中英标点再比（真屏幕上标点常被读成别的），
**但字母认错仍然算错**。

### 7.6 试过但**没用**的：让窗口自己重画（PrintWindow）—— 已回退

想法：`grabWindow(0)` 拿的是 DWM 合成像素，而 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)`
能让窗口（Chromium/Electron 认这个）自己重画一遍，理论上是更干净的灰度抗锯齿像素。

**实现过，又删掉了**：`winput` 里写了 `window_at()/grab_window()/looks_blank()`，
`gui` 里加了 `window_crop()` + "重绘读空了就退回屏幕那份"的兜底 + 自检钩子
`SNAPWORD_DEMO=winocr:x,y,w,h`。然后做了**受控对照**（`tmp\win_crop_test.py`）：
同一块区域、同一时刻，两种抓法各裁一次，native 逐个 band 并排比。

**结果：一模一样，逐条逐字相同。**

```
y=390  win=2编辑·F:\SnapWord\src\snapword\gui.py +22 -13
       scr=2编辑·F:\SnapWord\src\snapword\gui.py +22 -13
y=460  win=网思考· Now the note in `_on_job_done' can mention the grab source...
       scr=网思考· Now the note in `_on_job_done' can mention the grab source...
```

**教训（比结论值钱）**：之前那句"重绘像素明显更干净"是**混淆对照**下的错觉 ——
我拿的是**不同时刻、不同内容**的两张图（PrintWindow 那张是几分钟前抓的，
屏幕那张是另一行文字）。要下这种结论，必须**同一块像素两种抓法并排跑**。
既然零收益，功能、兜底、钩子全部删掉，不留复杂度。

顺带一个坑：**`GetWindowRect` 在 `QApplication` 建起来之前返回的是虚拟化坐标**
（同一个 DSH 窗口：建 Qt 之前 `(-7,-7)-(2055,1159)`，建之后 `(-9,-9)-(2569,1449)` ——
差了 1.25 倍缩放）。所以任何抓窗口的代码都必须等 Qt 起来（进程变成 per-monitor DPI aware）再抓。

### 7.7 还没解决的（本轮的硬骨头）

真屏幕那张（`tmp\screen-line1.png`，1984x57，中文 + 灰底 mono 代码胶囊）：
- native **代码标识符大多读对**（`src/30-Wheel.cs:12`、`Wheels.Count+1`、`SateName`）
  但**中文错一半**（`坏目录…`、`日采名是`）；
- system 在这张上几乎全错（0/4 个代码片段）；
- **放大 / 反色 / 灰度 / 补边都救不回来**（逐格实测过）；
- 两种抓法（屏幕合成 / 窗口重绘）也一样烂（7.6）。
- 说明瓶颈在 `PP-OCRv6_tiny` 这个**小模型**对深底浅字、小字号中文的识别能力上，不是预处理。

下一步候选（按性价比排）：
1. 换更大的 PP-OCR 模型（现在是 `PP-OCRv6_tiny_det/rec.onnx`）—— 要新的模型文件。
2. 调 `56-Ocr.cs` / `57-OcrNative.cs` 的检测阈值 —— 动 SnapWheel 源码，改动面大。
3. 攒真实语料：`ocr_bench.py collect` 用鼠标真框（**合成图骗过我们一次，别再拿它下结论**）。
4. 攒真实语料：`ocr_bench.py collect` 用鼠标真框（**合成图骗过我们一次，别再拿它下结论**）。

## 8. 换模型：把 tiny 换成 PP-OCRv5 server（治本，用户 m01921「换模型吧，治本」）

7.7 的结论是"瓶颈在小模型本身"，所以直接换模型。**模型不是写死在代码里的** ——
`57-OcrNative.cs` 的 `PayloadDir()` 按 `*det*.onnx` / `*rec*.onnx` / `*dict*.txt`
三条 glob 在 `ocr_dir`（或它的 `inference\`）里找，**所以换模型 = 换目录**。
payload 的 `README.md` 自己列出了支持的档位：

| 模型 | det / rec | 说明 |
|---|---|---|
| PP-OCRv6 tiny | 1.8 / 4.5 MB | 原来的默认，快、小字拉丁会切错框 |
| PP-OCRv6 small | 9.8 / 21 MB | V6 高容量档 |
| PP-OCRv5 mobile | 4.8 / 16.6 / cls 1 MB | 带方向分类 |
| **PP-OCRv5 server** | **88 / 84 MB** | 最大、最准（本文选的） |

### 8.1 怎么下：`tools\fetch_ocr_model.py`（走 ModelScope）

GitHub 的 release 包里只有 tiny（zip 才 17MB），大模型得另找。**ModelScope 上的
`RapidAI/RapidOCR` 齐全而且快**（实测 **~5 MB/s**，165MB 半分钟下完；GitHub 那边
github.com 只 ~50 KB/s）。脚本会：

1. 先请求 `.../repo/files?Revision=master&Root=<目录>` 拿上游的 **Size 和 Sha256**；
2. 下 `onnx/PP-OCRv5/det/ch_PP-OCRv5_det_server.onnx`、
   `onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_server.onnx`、
   `paddle/PP-OCRv5/rec/ch_PP-OCRv5_rec_server/ppocrv5_dict.txt`（**识别模型必须配同版本词典**）；
3. **校验大小 + sha256**，不对就删掉 `.part` 报错（别把半截模型留给 OCR）；
4. 从 tiny 那份 payload 拷一个 `lw.OpenCVDNN.PPOCR.dll` 过来（`PayloadDir` 要求目录里
   既要有 DLL 又要有模型）；
5. 落到**独立目录** `F:\ppocr\models\ppocrv5-server\`，**不覆盖** tiny —— 这样才能 A/B，不行就改回去。

### 8.2 A/B（`tmp\model_ab.py`，16 条语料，都走 native）

| 模型 | 通过 | 平均耗时 |
|---|---|---|
| tiny（PP-OCRv6） | 10/16 | **94 ms** |
| PP-OCRv5 server | **15/16** | 1182 ms |

v5 server 把 tiny 全部读错的格子都读对了：`serendipity` @10/11/13/20px
（`serendipit` / `serer⏎idipity` / `seren⏎ndipity` / `idipity⏎C` → 全对）、
`the quick brown fox` @20px（`th⏎juick brown fox` → 对）。
**唯一还挂的**是 `dsh-line1-宽留白` 的 `grep→qrep` —— v5 也是 `qrep`，
这种单个字母的混淆两个模型都没救。

### 8.3 代价与折中：阶梯「便宜在前」

1.2 秒太慢，不能当默认。慢的根因是 **helper 一个进程一次调用**，每次都要从磁盘
重新加载 165MB 的 ONNX（模型越大越慢）。

`ocr._ladder()` 的规则：`native+tiny` →（分数 < `STRONG`）→ `native+v5server` → `system`。
拿到 `STRONG` 就收工，所以：

- 普通词（tiny 读得准）**~60-90 ms**；
- tiny 读得可疑时才花那 1.2 秒：实测 `serendipity`@10px **465 ms**、
  `the quick brown fox`@20px **551 ms**（图小，大模型也没到 1.2s）；
- `system` 放在最后：它永远可用且最便宜，但当大模型在场时基本轮不到它。

配置项 `ocr_dir`（tiny）+ `ocr_dir_strong`（v5 server），清空后者 = 关掉大模型。
bench 里 `auto` 走的就是这条阶梯，所以 **auto 15/16**（native 单跑 10/16、system 9/16）。

### 8.4 下一步（可选）

payload 里自带 `lw.OpenCVDNN.PPOCR.HttpServer.exe`。改成**常驻服务**就不用每次重新加载
模型，能把大模型那 1.2s 压到一两百毫秒 —— 那是个架构改动（进程生命周期、端口、崩溃重启），
现在没动。

## 9. 还没做 / 下一步（早先记下的）

1. **两个引擎同时认糊就没救**。放大 2 倍已经试过（第 7.3 节：合成/宽条图上没差别，
   真屏幕那张也没差别）—— 现在最看好的是第 7.6 节的「按窗口 `PrintWindow` 重绘再裁」；
   另一个方向是把两个引擎的结果按行对照合并（某一行只有一个引擎认成真词就用那一行）。
2. **纯中文（没有拉丁词）判不了分**，只能靠"先跑 native"兜着。如果要认真做，需要一张
   中文词频表（ECDICT 只有英文）。
3. **专有名词被当成"不认识"** → 每次都要多跑一个引擎（约 +60ms）。第 7.4 节已经把
   "以中文为主的文本"里的英文改成轻罚了，纯英文场景还是按 -1000 重罚。
4. 语料现在 16 条（14 合成 + 2 真实屏幕），**真框选的还很不够**（`collect` 要用鼠标框，
   得用户来）。攒够真实用例后，第 6 节的表才有说服力。
5. PP-OCR 的切框问题可以试 helper 侧调参（`56-Ocr.cs` / `57-OcrNative.cs` 里的检测阈值），
   但那是改 SnapWheel 的源码，改动面大，先不动。
