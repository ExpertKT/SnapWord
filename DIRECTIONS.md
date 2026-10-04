# DIRECTIONS · SnapWord

给下一个会话（或者三个月后的自己）看的东西。**别把这里当成现状**，动代码前核一遍文件。

## 一句话

屏幕上看到生词 → 框一下 → 卡片出来。挂着常驻、不抢焦点、释义以词典为准、LLM 只做展开。

## 用户拍过的板（别再问一遍）

1. **技术栈**：Python + **PySide6(Qt)**。用户明确否了"复用 SnapWheel 的 C#/WinForms"，
   要换新栈。所以 SnapWheel 那边只复用了 OCR 的 **C# 源码**（编成 exe 当子进程用），
   UI / 查词管线全部是新写的 Python。
2. **释义权威源**：离线词典(ECDICT) + 有道 为主，LLM 只负责展开。
   原话是"1、2、3 都需要，以 2 为主"，追问后确认为"释义以词典/有道为准，LLM 负责展开"。
3. **平台**：只做 Win10/11 x64。Win7/32 位"后面再考虑兼容"——也就是不做。
4. **位置**：`F:\SnapWord`，暂不发 GitHub。
5. **小 AI 答疑**：卡片里内嵌。"默认本地 9B 答（免费、不抢焦点），答不准时一键转 DeepSeek"。
   实现上是**按住 Shift 回车**才走 DeepSeek（见 `gui.py` 的 `Card._send`）。
   **2026-10-05 改**：默认改成免费云端 pollinations（见文末「公开仓库的默认值」），
   本地 9B 降级成"装了才用"。
6. **常驻面板**：User said (m00717): "我需要他能默认挂在屏幕右侧，可以展开ui，可以不依赖快捷键"。
   → `gui.Dock`，默认开（`config.dock.enabled`）。这一条是硬需求：**热键一条都注册不上时，
   全部功能仍然可用**，所以凡是新增功能都要在 dock 面板上留一个入口。
7. **面板四连修 + 答疑可用性 + 美化**：User said (m00866): "侧边栏不置顶，不贴边，会展不开，
   关闭侧边栏也跟着一起消失，如果别人用这个没有本地模型，问ai没有流失输出不太行，能不能接百度
   翻译那些的api。还有那个ui不是很合理，需要美化"。（"流失输出" = 流式输出。）
   → ① `EDGE_GAP=0` 贴边、② 钉在启动时鼠标那块屏、③ 细边子 label 不吃鼠标事件、
   ④ `setQuitOnLastWindowClosed(False)`；⑤ 问答流式 + 本地没在跑自动转 DeepSeek；
   ⑥ 接百度翻译；⑦ QSS 重做。
   **注意**：他发的截图上是 DSH Desktop 的界面，"侧边栏"也可能指 DSH 自己的会话栏 —— 已问过，
   按 SnapWord 的 dock 理解并修（每条都有可复现的机理）。
8. **UI 风格 / 动效 / 换屏 / 托盘开关**：User said (m01201): "现在侧边栏跑我副屏上面去了，
   而且打开收起没有动画，并且没有设置选项，托盘图标右键也不能选择是否开启侧边栏，
   而且ui需要进一步美化。很多地方需要动画过渡。ui设计等审美相关问题需要你去github搜索
   相关资料自我学习敲定风格然后执行"。
   → ① dock 默认**主屏**（去掉 `screenAt(QCursor.pos())` 这个默认）+ 设置/托盘里能选屏；
   ② 展开收起 180ms 动画、卡片进出场/长高都加动画；③ 设置里加"面板挂在哪块屏"下拉；
   ④ 托盘右键加三项；⑤ 视觉风格**自己查 GitHub 定死**在 `docs\UI-STYLE.md`
   （参考 Raycast 的 design tokens），`gui.py` 顶部 `T`/`DUR` 就是那张表的代码版。
   **"审美问题自己定"这条是用户明确授权的**，别再拿配色去问。
9. **框选识字不准 + 要自迭代测试**：User said (m01495): "框选识字有时候只能识别出一个字母
   什么的，很奇怪，识别这一块还是有问题，不能给人确认感，我希望你可以建立一个识别能力自我迭代
   的测试，他大概是这样的：每次测试框选了一块区域，截了图，识别出来的字母标记起来，存到一个
   地方，然后开始自查反思原因，进行迭代"。
   → ① 根因是旧 `auto` "第一个非空就收工"，native 吐的半截词成了答案 → 改成**词典打分择优**
   （`lookup.score_ocr`，见本文「两个引擎」节）；② 卡片备注加「识别自 xx（xx 毫秒）」给确认感；
   ③ 落地 `tools\ocr_bench.py`（collect/run/report）+ `docs\OCR-NOTES.md` 证据档；
   ④ **改 OCR 必须跑 `run` 看通过率，不涨就撤**（这是用户要求的"迭代"的硬标准）。

## 这台机器上的硬事实（不然会踩）

- **C 盘只剩 ~13 GB**。venv 在 `F:\SnapWord\.venv`，pip 一律
  `--no-cache-dir` + `TEMP=F:\SnapWord\tmp`，别让 pip 往
  `%LOCALAPPDATA%\pip\Cache` 里灌（灌过一次 155 MB）。
- **Python 3.14.7**。PySide6 要到 **6.11** 才支持 3.14（`<3.15,>=3.10`）；
  6.9.x 是 `<3.14`，装不上。**已装 PySide6-Essentials 6.11.2 + shiboken6 6.11.2**
  （`cp310-abi3-win_amd64`，76.9 MB wheel）。只装了 Essentials，没装 Addons。
- **pip 的源要挑**：`files.pythonhosted.org` 慢到不能用；**阿里云
  `https://mirrors.aliyun.com/pypi/simple` 826 kB/s**，1 分 34 秒下完。清华/中科大/腾讯也有。
  （曾经误判"清华只有 6.9.3"——那是我的正则只匹配了 cp39-abi3 的 wheel 名，
  6.11.2 实际是 cp310-abi3，别再用那个正则下结论。）
- **全局热键是稀缺资源**。实测 `ctrl+alt+D` 和 `ctrl+alt+W` 在这台机器上
  **已经被别的软件占了**（`RegisterHotKey` 返回 0，错误码 **1409
  ERROR_HOTKEY_ALREADY_REGISTERED**）；`ctrl+alt+Q`、`ctrl+alt+S`、`ctrl+shift+D`、
  `alt+shift+D`、`ctrl+alt+F9` 都能注册上。
  所以 `winput.HotkeyThread` 的 mapping 值可以是**候选列表**，注册上的记在
  `hk.registered` 里，`gui.run` 的 `after_hotkeys()` 用托盘气泡告诉用户换成了哪个。
  **不要再用模态 QMessageBox 报热键错误**——那会抢焦点，违反第一条硬要求。
- **某个窗口类的名字**要用来判断能不能发 Ctrl+C：`winput.CONSOLE_CLASSES`。
- **Ollama**（**可选**：`providers.ollama.enabled` 默认 false，本机装了才开）
  `http://127.0.0.1:11434/v1`，唯一模型 `qwen3.5:9b`。
  **必须发 `reasoning_effort: "none"`**，否则思考模型回空 content
  （`providers.chat` 检测到空内容会直接报错，就是为了把这个坑喊出来，别把那段删了）。
- 托盘图标是代码画的（`gui.Tray`），**没有图标文件**，别去找 `.ico`。

## 数据从哪儿来（这一条省了 40 分钟，别重走）

**GitHub 在国内没法用**：`github.com` 直连 ~50 KB/s；`raw.githubusercontent.com`
直连 20~200 KB/s 乱飘；`gh-proxy.com` 爆发能到 370~580 KB/s 但**下大文件会截断**
（下 ecdict.csv 只给到 22.6 MB / 196221 行就宣布"完成"，真文件是 65.9 MB / 77 万行）；
`ghproxy.net` ~17 KB/s；`raw.gitmirror.com` 已死；jsDelivr 对 >20 MB 文件直接 403。

**走 npm 镜像**：`registry.npmmirror.com` 实测 **41 MB/s**。npm 上的
`node-ecdict-sqlite-lastest` 包里就是完整的 `package/db/ecdict.sqlite`（191 MB，
解出来 182 MB，表 `stardict`，77 万行）。`tools\fetch_dict.py` 就是干这个的，1.5 秒下完。

表结构（和 `Ecdict` 对齐，别改字段名）：
`id, word, sw, phonetic, definition, translation, pos, collins, oxford, tag, bnc, frq, exchange, detail, audio`。
`word` 上有索引，查一次 **0.1 ms**，所以"快"这条是稳的。

## 踩过的坑（改代码时注意）

- **sqlite3 连接不能跨线程**。查词全在 `Job(QThread)` 里跑，所以
  `Cache` 和 `Ecdict` 都必须 `sqlite3.connect(..., check_same_thread=False)` **加一把
  `threading.Lock`**。这个坑先后炸了两次（先是缓存、后是词典），
  `tests\test_core.py::test_dict_and_cache_are_usable_from_a_worker_thread` 就是钉它的。
  有个更隐蔽的地方：卡片吃**缓存命中的词**时根本不会碰 `Ecdict`，所以第一次测试全都过 ——
  验证一定要用**没查过的生词**。
- **热键回调跑在 `winput.HotkeyThread` 自己的线程里，绝不能在那里面碰 Qt**。
  `App.on_hotkey` → `start_pick()` 要创建并 `show()` 遮罩窗口，在非 GUI 线程干这个是
  未定义行为，实测**一按热键进程就崩**（用户报的「崩溃了」就是这个）。
  现在 `App` 上有个 `hotkey_fired = QtCore.Signal(object)`，热键线程只 `emit`，信号跨线程
  自动排队回主线程。`tmp\hotkey_test.py`：真注册热键 + 真注入 Ctrl+Alt+Q，遮罩在
  主线程开出来、进程活着，就是钉这一条的。
- **`SendInput` 的 `cbSize` 必须等于真正的 `sizeof(INPUT)`（x64 = 40）**。`winput._INPUT`
  的 union 里如果只声明 `_KEYBDINPUT`，整个结构只有 32 字节，`SendInput` 就**一个事件都不发**
  （返回 0、`GetLastError()` = 87，不报错也不崩）—— 划词取词静默失效，最难查的那种。
  union 里必须再放一个跟 MOUSEINPUT **一样大**的成员（32/24，按指针宽度）；
  放"差值"没用，union 取的是最大的那个。`tests\test_core.py` 有断言钉着。
- **`.cmd` / `.ps1` 里一律只写 ASCII**。cmd.exe 按 OEM 代码页（这里 936/GBK）读文件字节，
  所以 UTF-8 的中文注释/`echo` 会被解码成乱码**当命令执行**，报出
  `'nv锛岀劧鍚庤窇' 不是内部或外部命令` 这种看不懂的错。
  `启动 SnapWord.cmd` 和 `helper\build-ocr.ps1` 都吃过这个亏，现在两个文件都是纯 ASCII
  （想写中文提示就 `chcp 65001`，但不如直接用英文）。文件名里带中文反而没事 ——
  双击走的是 `CreateProcess` 的 UTF-16 文件名，不经过代码页；**只有从 cmd 命令行里
  手敲中文路径**才会找不着文件。
- **取词临时文件写 `F:\SnapWord\tmp`**，别写 `%TEMP%`（那在 C 盘）。
- **`config.json` 会被 BOM 搞死**。PowerShell 5.1 的 `Set-Content -Encoding utf8` 和记事本
  存 UTF-8 **都会加 BOM**，然后 `json.load` 报
  `Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)`，三个 demo 全挂。
  根治：`config.py` 用 `PATH.read_text(encoding="utf-8-sig")`；手工写这个文件用
  `[System.IO.File]::WriteAllText($p, $s, (New-Object System.Text.UTF8Encoding($false)))`。
- OCR helper 读图必须 `File.ReadAllBytes` + MemoryStream，**直接 `new Bitmap(path)` 会锁住
  用户文件**（SnapWheel 的 `20-ImageIO.cs` 里踩过）。

## OCR 这条线

`helper\ocr-helper.exe` 是 **SnapWheel 的 C# 源码编出来的**（源码已 vendored 到 `helper\src\`）：

- `helper\build-ocr.ps1` 直接编译 `helper\src\` 里的 `56-Ocr.cs` + `57-OcrNative.cs` + `92-OcrTall.cs`
  \+ 本目录的 `Stubs.cs`（补 `Err.Log/Note/Notify`、`Lang.T`）+ `Program.cs`。
  `-SrcDir <路径>` 可以改去编别处的副本。
- 用的编译器是 `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`（框架自带，不用装 VS）。
- **SnapWheel 那边改了 OCR 源码**，把文件再拷一遍进 `helper\src\` 重跑一次 `build-ocr.ps1` 就同步。

### 两个引擎（互补，`auto` 的"谁来挑"是 Python 写的）
- `native` = 本地 PP-OCRv6，在 **`F:\ppocr\x64\lw.OpenCVDNN.PPOCR-v1.2.1.0-win7-x64`**
  （`lw.OpenCVDNN.PPOCR.dll` 14.5 MB + `inference\PP-OCRv6_tiny_{det,rec}.onnx` + `_rec_dict.txt`）。
  `57-OcrNative.cs` 的 `PayloadDir()` 只按顺序找 **环境变量 `SNAPWHEEL_OCR_DIR`** →
  `exe 旁边\ocr\` → `exe 旁边`，helper 在 `F:\SnapWord\helper\` 旁边两样都没有，
  所以**以前它一直是坏的**（报「没有本地取字组件」）。现在 `config.json` 的 `ocr_dir`
  指向它，`ocr.py` 转成环境变量透传。
- **两个引擎互补，别再说"native 更准"这种一句话结论**：native **中文强、小字/细长拉丁弱**
  （它会把文字检测框切错：`serendipity` → `sereridipity` / `seren⏎ndipity` / `idipity⏎C`），
  system **拉丁强、小字中文弱**（`屏幕查词` → `屏墓查词`）。完整矩阵见 `docs\OCR-NOTES.md`。
- **`auto` 的规则（阶梯：便宜的在前 + 离线词典择优，别改回"第一个非空就收工"）**：
  `ocr._ladder()` = `native+tiny` →（分数 < `OCR_STRONG=10`）→ `native+v5server` → `system`，
  **谁分高用谁**。普通词只花 tiny 的 ~60-90ms，tiny 读得可疑才动用那个 165MB 的大模型。
  打分：词典认识 +10、不认识的"单词" **-1000**（狠是故意的：有一段认糊了就必须掉到阈值以下）、
  单个字母 -2、**最长行 ≤2 个字直接 -60**、以中文为主的文本里夹的英文按 -3 轻罚
  （中文句子里夹 `DSH`/`SnapWheel` 是产品名不是认糊）。**不能比长度** —— `sereridipity`
  比正确答案还长。
- **两套模型、两个目录（用户 m01921 要求"换模型治本"）**：模型不是写死在代码里的 ——
  `57-OcrNative.cs` 的 `PayloadDir()` 按 `*det*.onnx` / `*rec*.onnx` / `*dict*.txt` 三条 glob
  在 `ocr_dir`（或它的 `inference\`）里找，**所以换模型 = 换目录**。
  - `ocr_dir` = PP-OCRv6 tiny（快，94ms，语料 10/16）。
  - `ocr_dir_strong` = **PP-OCRv5 server**（`tools\fetch_ocr_model.py` 下的，165MB，
    语料 **15/16**，一次 1.2s —— 根因是 helper **一个进程一次调用**，每次都要重新加载
    165MB ONNX）。清空这一项 = 关掉大模型。
  - 下大模型走 **ModelScope 的 `RapidAI/RapidOCR`**（实测 ~5 MB/s，GitHub release 里只有 tiny）：
    det/rec 在 `onnx/PP-OCRv5/...`，词典在 `paddle/PP-OCRv5/rec/ch_PP-OCRv5_rec_server/ppocrv5_dict.txt`
    （**识别模型必须配同版本词典**）。脚本会校验上游 **Sha256**，还要从 tiny 那份拷一个
    DLL 过去（`PayloadDir` 要求目录里既要有 DLL 又要有模型），落到独立目录不覆盖 tiny。
  - 想再快就上 payload 自带的 `lw.OpenCVDNN.PPOCR.HttpServer.exe` 做常驻服务（架构改动，没做）。
- **旧 bug（用户报的"只能识别出一个字母"）**：原来是 `native → system` 逐个试、
  **谁认出非空文本就用谁**，native 吐的半截词是非空的，于是 system 永远没机会上场。
- 中文查不了词典，两个引擎都只读出中文时分数几乎一样，`_pick` 保留先跑的那个（native）。
- native 那个 DLL 初始化时会往 stdout 打两行作者横幅（`lw.OpenCVDNN.PPOCR 1.2.1.0` 和
  `作者：天天代码码天天，QQ：819069052`），**在 JSON 前面**，所以 `ocr._run` 是从下往上找
  第一个 `{` 开头的行来解析的 —— 别改回 `json.loads(整个 stdout)`。
- helper 自己的开关：`SNAPWHEEL_OCR=native|system` 能强制引擎（`ocr_engine` 走的是 `--engine`）。
- **helper 会先补边再识别（别删）**：`Program.cs` 的 `PadRatio = 6.0` + `PadForOcr()` ——
  PP-OCR 的文字检测在极端长宽比上失效（整窗 2578x1458 读对整段，`1690x46` 一条只吐 1~2 个字，
  正是用户报的"只能识别出一个字母"）。JSON 里的 `iw/ih/pw/ph` 是补边前后的尺寸，别当噪声删掉。
  实测对照见 `docs\OCR-NOTES.md` 第 7 节。
- **`score_ocr` 的两个补丁**：最长行 ≤ 2 个字 → `-60.0`（只吐一两个字肯定没读出内容）；
  不认识英文的罚分按 `1000.0 if latin > cjk * 2 else 3.0`（中文句子里夹的 `DSH`/`SnapWheel`
  是产品名不是认糊，重罚会让 auto 挑中一个字）。
- **"让窗口自己重画一遍（PrintWindow）会更干净"是个错觉，别再试第二次**：受控对照
  （同一块像素、同一时刻、两种抓法并排跑，`tmp\win_crop_test.py`）结果是**逐条逐字完全相同**。
  之前那句结论是拿**不同时刻不同内容**的两张图比出来的。实现过又删掉了（winput 的
  `window_at/grab_window`、gui 的 `window_crop`、`SNAPWORD_DEMO=winocr:`），零收益不留复杂度。
  顺带：**`GetWindowRect` 必须在 `QApplication` 建起来之后调** —— 之前拿到的是虚拟化坐标
  （DSH 窗口 `(-7,-7)-(2055,1159)` vs 建 Qt 后 `(-9,-9)-(2569,1449)`，差 1.25 倍）。
  剩下的硬骨头（深底浅字小字号中文）瓶颈在 `PP-OCRv6_tiny` 小模型，记在 OCR-NOTES 7.7。
- **"确认感"**：卡片备注会写 `识别自 本地 PP-OCR（124 毫秒）`（`gui.OCR_ENGINE_CN` +
  `self._ocr_info`），认错了用户至少知道该怪谁。
- **`recognize_png` 的 `SNAPWORD_KEEP=<目录>`**：把每次真正喂给 OCR 的那张图留一份，
  这是测试台攒语料的来源。改这个钩子前先看 `tools\ocr_bench.py` 的 `collect`。

### 识别能力自迭代测试台 `tools\ocr_bench.py`
用户 m01495 明确要求的：**"每次测试框选了一块区域，截了图，识别出来的字母标记起来，存到一个地方，
然后开始自查反思原因，进行迭代"**。落地成：

- `collect <名字> --expect "..."` 真框选（复用 `gui.Selector`）→ 存**真正喂给 OCR 的那张图**；
  `add <图片> <名字> --expect` 加现成图片；`run [--engine ...] [--case ...]` 跑 + 判定 + 归类 +
  追加 `history.jsonl` + 重写 `REPORT.md`；`report` 看通过率随时间。
- 归类只看"错成什么样"（空 / 只有一个字 / 太短 / 截断 / 丢开头 / 中文错字 / 空格差 / 字符替换），
  每类都映射到"大概是什么原因 + 下一步试什么"，报告把出现最多的一类标成最该先修。
- **合成图会骗人**：一开始拿合成图自测全过，用户一框就出半截词 —— 所以真语料必须靠 `collect`。
- 首次基准（14 条合成用例）：native 9/14、system 7/14、**auto 14/14**。
- 改了 OCR 挑选/预处理/打分，**必须**跑 `run` 看通过率是不是真的涨了；不涨就撤改动。

### 反思那一半 `tools/ocr_audit.py`（让本地 9B 干活）
用户 m02447 明确要求的：**"把本地模型调动起来，并且有反馈"**。上面那半只在"错成什么样 →
该试什么"这一层（**正则**归的类），同一类错不同原因时给的建议是错的 —— 实测：强制 `system`
跑出来的 3 条 `cjk-confusion` 全被建议"去修 system"，而正确动作其实在 `auto` 的阶梯上。
所以失败条目再交给本机 `qwen3.5:9b` 逐条看：

- 事实进、两行出：把「期望/实得、引擎、耗时、图的宽高比、用例来路、正则归类、最近几次通过序列」
  拼成 prompt，要求只答 `病因：…` / `下一步：…`。
- 两个必须写进 prompt 的事实，否则结论会跑偏：①**这次是指定引擎跑的、不是 auto 选的**
  （`--engine X` 的失败不等于 auto 会选 X）；②**图的长宽比**（1690×46 和 132×30 的"截断"不是一个病）。
- 完全本地：`providers.ollama_chat`（内部带 `reasoning_effort:"none"`，不带这个 9B 返回空）。
  跑之前先 `providers.ollama_up()` 探活，没开就提示 `ollama serve` 直接退，不干等 90 秒超时。
- 解析**宽松到底**：标签可能被模型挤在同一行（实测 7 条里 1 条），先按标签强行断行再解析；
  一个标签都没有就把整段当"病因"——**宁可格式可疑，也不把模型说的话丢掉**。
- **它的话是假设不是事实**（实测它会编机制，比如"auto 因耗时 238ms 跳过了 native"）。
  产物 `data\ocr-bench\AUDIT.md` 每条都跟着复现命令 + 该用例历史通过序列，末尾有「历次建议」表：
  **上次照它做有没有用，对着通过序列看**。验证手段还是 `run` 的通过率。
- 实测：7 条失败 12~79 秒（首条冷 20 秒，后面 1~2 秒），7/7 都给出了下一步。
- `AUDIT.md` / `audit.jsonl` **必须 gitignore**：expect 里是屏幕截到的原文（含私人路径/聊天）。

## 常驻面板 `gui.Dock`（用户要的"挂在右边、不依赖热键"）

贴着屏幕右边缘的一个 26px 细边，点一下展开成 320+32 宽的面板。**收起状态就是全部界面**，
不用记住任何热键 —— 用户明确要这个。

- 结构：**不用 layout**。`self.panel`(`QFrame#card`，固定 320x300) 和 `self.rail`
  (`QFrame#dockrail`，26 宽) 都是手动 `move()` 的。原因：窗口收起时只有 26 宽，
  用 layout 的话 layout 会把 rail 留在原来的坐标（跑到窗口外面去），
  手动摆窗口+子控件坐标才对得上。
- rail 里的竖排"SnapWord"是**一个字母一行**的 `QLabel`（`"S\nn\na\np\nW\no\nr\nd"`），
  不是旋转绘制的 —— 别为了好看去写 `QPainter.rotate`，那是没必要的复杂。
- 展开/收起**只动 x，窗口尺寸恒定**：`_geo_open = QRect(右边缘-352, y, 352, h)`、
  `_geo_shut = QRect(右边缘-26, y, 26, h)`。rail 是 HBox 结构里最右边的部分，所以收起时
  正好只露 26px；展开时窗口先跳到 352 宽、把 panel 藏在 `x = 352`（细边后面），
  再 180ms 把 panel 动画到 `x = 0`（面板"从细边后面滑出来"）。收起反过来，
  动画 `done` 回调 `_settle()` 才把窗口缩回 26 宽。**因为宽度恒定，`grab()` 也不用裁剪**。
- 窗口属性跟卡片一样：`Frameless | WindowStaysOnTop | Tool` + `WA_ShowWithoutActivating`
  + `WA_TranslucentBackground`。**收起和卡片弹出都不抢焦点**；只有展开时
  `toggle()` 才 `input.setFocus()`（那是用户自己点的，抢焦点是对的）。
- 位置：`_place()` 用 `availableGeometry()`（不是 `geometry()`，否则会压到任务栏下面），
  `y` 由 `config.dock.y` 记（`None` = 居中）；拖 rail = 只改 y，松手才写回配置。
- `anchor()` 返回卡片该弹出的左上角 —— `Card.show_at` 内部会按 `at.x() - 宽/3` 平移，
  所以 `anchor()` 里要**把这个偏移补回去**，卡片才会正好落在面板左边 12px（见 `gui.py`）。
- `start_pick()` 里要 `dock.hide()`（框选遮罩是全屏的），`_on_cancel()` 里再 `show()` 回来。
- 挂哪块屏：`config.dock.screen` 存**屏名**，`""`/`None` = 主屏。设置里的下拉、托盘菜单的
  「面板挂在哪块屏」子菜单都改它，改完立刻 `set_screen()`。**默认必须是主屏** ——
  以前默认 `screenAt(QCursor.pos())`，鼠标停在副屏上启动就钉到副屏，用户报过
  「侧边栏跑我副屏上面去了」。
- 自检：`SNAPWORD_DEMO=dock`（收起）/ `dock-expanded`（展开）→ `grab()` 成
  `docs\dock-collapsed.png` / `docs\dock-expanded.png`。**几何打印放在 `_shot()` 里**，
  别放在 `_demo_start()` —— 展开/收起是 180ms 动画，早读会拿到动画中途的宽度
  （收起态曾读成 352 宽，假象，骗过一次）。
- **`_place()` 里绝对不要用 `self.screen()`**：它是按窗口当前所在位置推断的，细边点击时
  事件被子控件转发，那一瞬间会解成副屏，窗口被甩到 x=3908（实测过）。改成按
  `config.dock.screen`（屏名）钉死一块屏。
- **`_place()` 也不能读 `self.sizeHint()` 当宽度**：`adjustSize()` 之后它可能还是收起时那份
  （26 宽），展开时会算成 352 宽的面板摆在 x=2022 → 右边 326px 跑到屏幕外。展开/收起直接
  按常量算。
- rail 里那三个 QLabel 要 `WA_TransparentForMouseEvents`，否则点在字母上时事件被 label 吃掉，
  `eventFilter` 的"按下→抬起"配不上对，表现为**点了没反应（展不开）**。
- `run()` 里必须 `app.setQuitOnLastWindowClosed(False)`：托盘常驻程序一旦按默认行为，
  关掉卡片/收起面板会把整个进程（连托盘图标）一起带走。
- ⚠️ **`QAction` / `QActionGroup` 在 `QtGui` 里，不在 `QtWidgets`**（PySide6）：写
  `QtWidgets.QAction` 会 `AttributeError: module 'PySide6.QtWidgets' has no attribute 'QAction'`。
  托盘菜单那三项用它。

## 问 AI（流式、没有本地模型也能用、百度翻译）

- 流式：`providers.chat_stream()`（OpenAI 兼容 SSE，逐段 `yield`）；`Job(stream=True)` 把生成器
  的每段 `progress.emit` 回主线程（`_on_job_progress` → `Card.chat_push`）。**一个字都没吐出来时
  自动退回 `chat()` 一次性结果**，所以调用方不需要区分流式/非流式。
- 谁来答：`lookup.ask_plan()` 的排队是 **Ollama 在跑 → 填了 key 的联网模型 → 免费云端**
  （kilo / pollinations，都不用注册不用 key，`providers.kilo` + `providers.pollinations` 默认都开着，
  按 `providers.FREE_ORDER` 换手）。探活用
  `providers.ollama_up()` 打 `/api/tags`（结果缓存 30s）；三条路都关着才抛一句人话
  （告诉用户去开免费云端、或者怎么 `ollama serve`），绝不干等 180 秒超时。
  `Shift+回车`/`Shift+点发送` = 强制走联网模型。
- 追问带上下文：`App._chat_hist` 只留最近 8 条，换词查（`_show_brief`）就清空。
- 百度翻译：`providers.baidu()`，`sign = md5(appid + q + salt + key)`（**这个拼接顺序不能改**），
  要用户自己去 fanyi-api.baidu.com 免费领 appid+密钥，填在设置的「百度 appid / 百度密钥」。
  释义阶梯因此变成：缓存 → ECDICT → 有道 → 百度；模型那条阶梯是
  带 key 的联网模型 → 本地 9B（在跑）→ 免费云端。
- **`gui.py` 里曾经根本没有 `_esc()`，而 `Card.append_chat/chat_begin` 都在用它** →
  一按「问 AI」就 `NameError`。这说明"问 AI"这条路以前从没在 GUI 里真跑过。
  你往 `QTextBrowser` 塞 `<b>` 富文本时，用户和模型给的字符串**必须先 `html.escape`**，
  否则模型答里一个 `<` 就能把排版吃掉。

## 怎么验证（改完必须跑的那几样）

```bat
:: 1. 纯逻辑（无框架裸断言，秒出，27 个）
F:\SnapWord\.venv\Scripts\python.exe F:\SnapWord\tests\test_core.py

:: 1b. OCR 认得准不准（判定 + 归因 + 通过率；改了 OCR 就必须跑，不涨就撤）
F:\SnapWord\.venv\Scripts\python.exe F:\SnapWord\tools\ocr_bench.py run
F:\SnapWord\.venv\Scripts\python.exe F:\SnapWord\tools\ocr_bench.py report

:: 2. OCR helper 通不通
F:\SnapWord\.venv\Scripts\python.exe -m snapword.cli probe

:: 3. 查词管线（不联网也能跑，要 -X utf8 否则控制台 GBK 报错）
python -X utf8 -m snapword.cli look running --no-net
python -X utf8 -m snapword.cli look serendipity --detail --json
python -X utf8 -m snapword.cli ask serendipity "它和 luck 的区别？"

:: 4. GUI 渲染自检：弹卡片 -> 把卡片自身渲染成 PNG -> 自动退出
set SNAPWORD_DEMO=serendipity & set SNAPWORD_SHOT=F:\SnapWord\tmp\card.png
set SNAPWORD_DEMO=ocr:quixotic & set SNAPWORD_SHOT=F:\SnapWord\tmp\card.png
set SNAPWORD_DEMO=dock-expanded & set SNAPWORD_SHOT=F:\SnapWord\tmp\dock.png
:: 真问一句、真流式回答（要等 14 秒），截图里应该能看到"你："和"AI（免费云端）："
set SNAPWORD_DEMO=chat:serendipity & set SNAPWORD_ASK=它和 luck 有什么区别？ & set SNAPWORD_SHOT=F:\SnapWord\tmp\chat.png
python -m snapword.cli gui
```

`SNAPWORD_DEMO` / `SNAPWORD_SHOT` 是纯自检钩子（`gui.run` 末尾），正常使用不会碰到。
`SNAPWORD_DEMO=ocr:WORD` 走的是**真路径**：画图 → `pixmap_png` → `ocr-helper.exe` → 查词 → 卡片。
`widget.grab()` 渲染而不是抓屏，所以不会拍到桌面上别的东西。

```bat
:: 5. 热键那条线（真注册 + 真注入 Ctrl+Alt+Q，看遮罩是否在主线程开出来）
F:\SnapWord\.venv\Scripts\python.exe -X utf8 F:\SnapWord\tmp\hotkey_test.py
```

**出问题先看 `F:\SnapWord\data\snapword.log`**：pythonw 下 `sys.stdout`/`sys.stderr` 是 `None`，
所以 `install_crash_log()` 把它们引到这个文件，并挂上 `sys.excepthook` + `threading.excepthook`
（QThread 里的异常走后者，不走前者）。**`traceback.print_exc()` 往 `None` 上 write 会当场
AttributeError**，所以这个钩子不只是好看，是防止后台任务把进程搞崩还什么都没留下。

## 发布到 GitHub

仓库已经 `git init` 并提交了第一个 commit（分支 `main`，70 个文件，`.git` 约 5.6 MB ——
`helper\lw.OpenCVDNN.PPOCR.dll` 那 14 MB 压得很好）。**没有推**，因为远端要用户自己建。
用户拿到仓库地址后：

```bat
cd F:\SnapWord
git remote add origin https://github.com/ExpertKT/SnapWord.git
git push -u origin main
```

仓库身份先在本地配了 `user.name=ExpertKT` / `user.email=ExpertKT@users.noreply.github.com`
（`.git/config`，随仓库走；要改名改邮箱用 `git config user.email ...`，别用全局的）。

**提交进仓库的东西**：源码、`helper\`（含 DLL 和它的 LICENSE/THIRD_PARTY_NOTICES）、
`helper\src\`（vendored 的 SnapWheel OCR 源码）、`docs\`（截图 + UI-STYLE + OCR-NOTES）、
`data\ocr-bench\cases\`（OCR 回归语料，几百 KB）、`tools\`、`tests\`、README/DIRECTIONS/LICENSE/.gitignore。

**故意不提交**：`.venv/`、`models/`（165 MB，用 `tools\fetch_ocr_model.py` 下）、
`data\ecdict.db`（182 MB，用 `tools\fetch_dict.py` 下）、`config.json`（每台机器不一样）、
`data\cache.db`、`data\snapword.log`、`tmp/`、
`data\ocr-bench\{history.jsonl,REPORT.md,AUDIT.md,audit.jsonl}`（都是跑出来的；后两个还引用截屏原文）。

发布前要确认的三条：① `git status` 干净；② `config.json` 没被跟踪（里面有用户自己的 key）；
③ README 里那五步在一台干净机器上真的能跑（只有 `python -m venv` + pip 是外部依赖）。

### 发一个版本（release）

```bat
python tools\make_release.py                                :: 打 zip 到桌面（只装 git 跟踪的文件）
git tag -a v0.1.0beta -m "SnapWord v0.1.0beta：屏幕查词"
git push origin v0.1.0beta
gh release create v0.1.0beta "C:\Users\<你>\Desktop\SnapWord-v0.1.0beta.zip" ^
   --title "SnapWord v0.1.0beta · 屏幕查词" --notes-file tmp\release-notes.md
```

- zip 里的 `setup.cmd` / `SnapWord.cmd` 是 `tools\make_release.py` **当场生成的**（不放进仓库，
  免得仓库根目录出现两份启动器）。两个脚本必须纯 ASCII —— cmd.exe 按 OEM 代码页读。
- `gh release create` 不加 `--prerelease` 就是 Latest；想标成预发布加这个参数。
- 打完 tag 之后再改 README 不影响已发布的 zip（zip 是构建出来的，不是 git 导出）——
  所以**先改文档、再打包、最后打 tag** 才是干净的顺序。
- 发布后检查：`gh release view v0.1.0beta --json assets` 里资产在，下载链接点得开。

## 还没做的（按用户优先级排）

- **OCR 语料现在 16 条**（14 合成 + 2 张真实屏幕裁图），真框选的还很不够，得用户用
  `ocr_bench.py collect` 攒；深底浅字小字号中文两种抓法都读不全（见 `docs\OCR-NOTES.md` 7.7）。
- 鼠标停词（悬停 0.5 s 自动取词）—— 简报第 4 节提过，用户没强求。
- 多显示器：`App.start_pick` 只抓鼠标所在那一块屏；dock 现在**默认主屏**、可在设置/托盘里换屏，
  但拖 rail 只能上下挪，没法拖着跨屏。副屏上没实测过（副屏 `QRect(2560,0,1707,1067)`，
  缩放率和主屏不一样，DPR 换算会得到 293x300 这种数 —— 那是 Qt 的缩放，不是布局 bug）。
- 百度翻译的 appid/密钥要用户自己去 fanyi-api.baidu.com 领（代码和设置项都好了，key 还没填）。
- 词形还原是后缀规则拼的，不是 ECDICT 的 lemma 反查。
- 面板展开/收起、卡片进出场、卡片长高都做了动画（见 `docs\UI-STYLE.md`）；还剩
  托盘菜单和设置对话框的进出场没做（那俩是系统 QMenu / QDialog，动画收益小、风险大）。
- Qt 会往 stderr 打一行 `setGeometry` 警告，无害。

## 公开仓库的默认值：不假设别人有本地模型（2026-10-05）

User said (m11860): "把github项目的readme收拾一下。我想纠正一下，别人根本对我本地的9B完全不在意，
不要搞得跟谁都有本地模型一样。真的没有办法能让这个东西直连到什么免费模型上面然后做解释吗"。

之前所有文案（README、设置窗、按钮 tooltip）都把"本地 9B"当默认路径写 —— 那是**我这台机器**的
默认路径，不是别人的。别人 clone 下来：没 Ollama、没 key，于是「详解」一个字都出不来。

- **新增 `providers.pollinations`**（默认 `enabled: true`，`url: https://text.pollinations.ai/openai`，
  `model: openai-fast`）：**零注册、零 key**，`POST /openai` 能回真文本（OpenAI 兼容体）。
  `providers.pollinations_chat/_stream` 就是拿空 key 走 `chat/chat_stream`（`chat()` 在 key 为空时
  **不加 Authorization 头**，正好对上它的匿名档）。
  **模型名是个坑**：它 `/models` 里标 `tier=anonymous` 的只有 `openai-fast`；填 `openai` 那类要 token
  的档匿名一律 **402**。它自己抽风时报 402（限流）或 500（后端满，实测见过 `ENOSPC`），
  所以 `lookup` 给免费通道的 timeout 比别的短（chat 60s / stream 120s）——它挂住时是一个字节都不吐，
  让人对着"正在想…"等 300 秒是最糟的失败方式。
- **`providers.ollama.enabled` 默认改成 `false`**。装了的人自己打开（他本机的 config.json 里本来就是
  显式 true，不受影响）。
- 排队规则（`lookup.ask_plan` / `lookup.detail`）：带 key 的联网模型 → 本地 9B（在跑）→ 免费云端；
  `_detail_offline` / 两条 RuntimeError 都改成"先去开免费云端"的口径。
- **取舍要写在明处**：免费通道意味着**查的词和问题会经过 pollinations 的服务器**，所以 README 里
  明写了这一点，并用 `SOURCE_LABEL` 让卡片上的来源行自报"免费云端…仅供参考"，不冒充精品解释。
- 文案统一：设置窗那三格从「DeepSeek 地址/模型/Key」改成「联网模型 地址/名称/Key」——
  它本来就是任何 OpenAI 兼容服务（智谱 GLM-4-Flash、硅基流动、DeepSeek 官方都行），
  拿 DeepSeek 当填入示例。
- 回归：`tests/test_core.py::test_free_cloud_covers_machines_without_any_local_model_or_key`
  钉住四件事 —— 默认配置开着两家免费通道、关着 ollama；没 key 没本地模型时 `ask_plan` 选 **kilo**；
  **kilo 挂了自动退到 pollinations**（两家免费通道的意义就在这）；只开 pollinations 的老配置仍落到它头上。

### 2026-10-05 追加：免费通道从一条变两条（kilo 排前面）

用户问"真的没有能白嫖的办法了吗" —— 于是把 keyless 的端点挨个实测了一遍（都是**不带任何 key**
真发请求）：

- **`kilo.ai` 网关**（`POST https://api.kilo.ai/api/gateway/chat/completions`，`model: kilo-auto/free`）
  → **200 + 正常中文词典体**，流式也是标准 OpenAI `delta.content`（实测连发两问都对），
  限额 **200 次/小时/IP**，国内直连可用。→ 因此把它排在 pollinations **前面**（`providers.FREE_ORDER`）。
  坑：`kilo-auto/free` 背后是个会先写 `reasoning` 的模型，**给它小的 `max_tokens` 会出现全推理、
  content 空的回包**（我用 `max_tokens=200` 试词典体就中过）；SnapWord 这两条路本来就不传 max_tokens，
  所以没事，但要记着别以后手贱去加。
- `pollinations` → 同一时段 6 次里 5 次 **402/500（ENOSPC）**，保留为备用。
- `OVHcloud` 匿名档 → `/models` 200，但 chat 直接 **429**（全匿名共享 2 次/分钟），太挤，不接。
- `VLM Run` → 现在匿名也要 token（列表里的"no key"已过期）；`uncloseai` → 域名解析不了；
  `Hack Club AI` → 404；**DuckDuckGo Duck.ai**（`x-vqd-4` 那条路）→ 国内连接超时（被墙），
  对一个面向国内用户的 README 没用。
- 结论：**零注册零 key 且国内直连可用的，就只有 kilo 和 pollinations 这两条**（都接了、按顺序换手）；
  再稳一档就必须注册拿 key（智谱 GLM-4-Flash、硅基流动的免费档）。

实现形状：`providers.FREE_ORDER = ("kilo", "pollinations")` + `providers.free_chat/free_chat_stream(name, p, msgs)`
（**按名字现查 dict，所以测试里 monkeypatch `providers.kilo_chat` 仍然生效** —— 别改成模块级 dict 常量，
那样 patching 就失效了）；`lookup._free_list()` 按顺序返回开着的那些，`_brief_llm`/`detail` 逐个试、
第一家成功就用它，全挂才写 `免费云端（名字）没答上：…`。`ask_plan` 取列表第一个。
