# SnapWord 视觉规范（先定死，再照着写 QSS）

用户要求"UI 需要进一步美化、很多地方需要动画过渡，去 GitHub 搜资料自学敲定风格"。
这一份就是敲定的结果，`gui.py` 顶部的 `T` 令牌表和 `QSS` 都必须从这里取数，**不要再随手写颜色**。

## 参考来源

- [VoltAgent/awesome-design-md · raycast/DESIGN.md](https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/raycast/DESIGN.md)
  —— 暗色 command-palette 系统的完整令牌表（surface 阶梯、hairline、圆角尺度、字号阶梯）。
  选它的理由：SnapWord 的卡片本质上就是"屏幕上的一个 command palette"，
  和 Raycast 自己的产品界面是同一类东西，风格天然合适。
- 取数途中 `raw.githubusercontent.com` / `api.github.com` / `cdn.jsdelivr.net` 用系统的
  `web_fetch` 都报 "resolves to a non-public IP"（本机 DNS 被拦），**用 `curl.exe` 走
  `cdn.jsdelivr.net/gh/...` 能拿**。原文件存在 `tmp/design/raycast.md`（41 KB）。

## 令牌表（`gui.py` 里的 `T`）

| 用途 | 令牌 | 值 | 说明 |
| --- | --- | --- | --- |
| 窗口底 | `canvas` | `#07080a` | 近黑，卡片和面板的最底色 |
| 面板 | `surface` | `#0d0d0d` | 比 canvas 亮一档 |
| 输入框/次级按钮 | `elevated` | `#101111` | 再亮一档 |
| 悬停/选中 | `card` | `#121212` | 最亮一档（surface 阶梯的第四级） |
| 描边 | `hairline` | `#242728` | **每个卡片边缘都用它**，1px |
| 强描边 | `hairline-strong` | `#33373d` | **分隔线、hover 描边**。2026-09-28 修正：它**不再**用于聚焦态 —— 对 `surface` 只有 1.62:1，够不上 WCAG 非文本对比 3:1；聚焦环改用 `blue`（9.70:1） |
| 主文字 | `ink` | `#f4f4f6` | 词头、标题（**不是纯白**） |
| 正文 | `body` | `#cdcdcd` | 释义 |
| 次要 | `mute` | `#9c9c9d` | 音标、元信息、备注 |
| 最弱 | `ash` | `#6a6b6c` | 禁用态 |
| 主强调 | `blue` | `#57c1ff` | 主按钮、来源徽章、rail 文字 |
| 强调底 | `blue-soft` | `rgba(87,193,255,0.15)` | 胶囊底色 |
| 成功/警告/错误 | `green` `#59d499` / `yellow` `#ffc533` / `red` `#ff6161` | | 只在真需要语义时用 |
| 交互态（派生档） | `blue-hover` `#7bd0ff` / `blue-pressed` `#3aa9ea` / `handle-hover` `#4a5058` | | 2026-09-28 新增（`T` 里的键名是下划线：`blue_hover` / `blue_pressed` / `handle_hover`）。本来这三个值直接写在 QSS 里，为了守"QSS 里不出现任何令牌表外色值"这条，收进表里。它们只用于主按钮 hover/pressed 和滚动条滑块 hover，是 `blue` / `ash` 的明暗派生档，不做通用色 |

圆角：`4 / 6 / 8 px`。**输入框、按钮、卡片、面板统一 8**（2026-09-29 从"卡片 10"改齐：
Windows 11 Fluent 的浮层/对话框圆角就是 8px，自创一个 10 只会加重"不是系统的一部分"的观感），
细胶囊 4，整圆胶囊按控件半高取值（见下）。**卡片不允许 0 圆角。**
**整圆胶囊没有 9999 这个档**（2026-09-28 修正）：Qt 的圆角超过控件半高时**不会**像 CSS 那样
钳成半圆，而是**静默画成直角**（9999/16/12/8 在 14px 高的控件上实测全是直角）。
整圆必须按控件半高取值：版本徽章（高 ~17px）写 8px（`#chip`），词胶囊（高 24px）写 12px（`#pill`）。

**两套尺度，别混用**（2026-09-28 划清）：

- **布局间距**（外边距、元素间距）严格走基底 8：允许 `2 / 4 / 8 / 12 / 16 / 24`。
  卡片内边距 12~14，卡片间距 8，面板内边距 14，控件高度 32~36。
- **控件内边距**允许 `6 / 7 / 8 / 12 / 16`：这两个值是从参考设计系统的
  `command-palette-row`（6px 10px）和 `text-input`（8px 12px）派生来的，控件体量比布局小一档；
  其中 `7px` 是**为了把输入框和下拉框凑到 32px 控件高度**才允许存在的例外，别拿去当布局间距用。
- **聚焦环**（新增规则）：`QPushButton / QLineEdit / QComboBox / QCheckBox::indicator` 的 `:focus`
  一律把描边换成 `blue`，不用 `hairline-strong`（理由见令牌表第 2 行）。
- **空态**（新增规则）：`#empty` —— 查不到释义时那一行降成 `mute` / 13px / 上下 8px 内边距，
  和正常释义（15px `ink`）明确区分，别让用户以为那是一行正常内容。

字号（Windows 上没有 Inter，退到 `Segoe UI` / `Microsoft YaHei`，字距按表里的正值加一点点）：
词头 24/600、正文 15/400（行高 1.6）、元信息 13、备注与徽章 11~12、按钮 12~13。

## 一条**故意偏离**参考表的决定

参考表说"整个系统没有投影，深度只靠 surface 阶梯"。**这一条不照搬**：Raycast 那个是网页，
背后永远是自己的画布；SnapWord 的卡片和面板是**浮动在任意桌面内容之上**的窗口，
纯靠颜色阶梯会和背后的白色网页糊在一起。所以保留**一层**很淡的投影
（`QGraphicsDropShadowEffect`：blur 24、`rgba(0,0,0,120)`、y 偏移 6），
其余一切照抄。**不许再加第二层投影、不许发光。**

## 动效（2026-10-02 重写：令牌来自外部规范，不再自己拍）

时长令牌 `DUR = {"instant": 90, "fast": 150, "base": 240, "slow": 380}`，全部有出处：
即时反馈 50–100ms（M3 short3 / IBM Carbon / Shopify Polaris）、状态确认默认 150ms（跨设计系统
共识）、进入 200–300ms、退出 = 进入的 60~70%、stagger 30–60ms。缓动用 `QEasingCurve.BezierSpline`
拼外部规范的贝塞尔（`_EASE_PTS`）：进入 M3 emphasized-decelerate、退出 emphasized-accelerate、
位移 Apple overshoot、小状态变化 M3 standard。

| 场景 | 时长/曲线 | 做法 |
| --- | --- | --- |
| 按钮 hover/按下/聚焦 | 进入 90ms、离开 150ms（**不对称**：进入要即时，离开要柔和）；按下 50ms 下沉、松开 90ms 回弹 | `SmoothButton` 自绘（QSS 没有 transition，一帧硬切就是"简陋"的来源）：三个 0..1 过渡量 `hov/prs/foc` 走属性动画，每帧按量插值画底/边框/文字；按下时文字下移 1px |
| 输入框/下拉框聚焦 | 150ms | `SmoothLineEdit` / `SmoothComboBox`：焦点蓝环是自绘叠层淡入，QSS 里**故意没有** `:focus` 规则（会打架） |
| 面板展开 / 收起 | 240ms，OutQuint/InCubic | **窗口宽度固定**（2026-10-03 起 = 记词板 300 + 面板宽 + 缝 6 + 细边 26，默认 652），只滑 panel；收起时 `setMask` 把可点区裁成 rail 那一条（透明区不挡鼠标）。**不许再 resize 窗口**——半透明窗口 resize 会被 Windows 拿旧 buffer 拉伸一帧（"左侧一闪"的根源），见坑清单 |
| 记词板开 / 关 | 240ms，OutCubic / InCubic | 记词板是**掀盖（mask 揭示）**，不是整块滑。抽屉在窗口里的位置**恒定 `wbpanel.move(0,0)`**，动的是一个数：`Dock._wb_reveal`（0 = 全关，`WB_W` = 全开），`toggle_wordbook()` 用 `QVariantAnimation` 推它，`valueChanged → _set_wb_reveal()` 只做 `_apply_mask()`。`_apply_mask()` 里 **铰链在靠主面板那侧**（`x=WB_W`），露出的是 `[WB_W-r, WB_W)` 这一条 —— 所以贴着铰链的像素先能点、远端最后出现，内容一动不动地被门缝照亮。用户报过两轮：先是"从左弹到右而不是从右弹到左"（旧实现把关闭位放在屏幕左缘外了），再是"它是整块滑出来的，不像「活板门」"（旧实现是 `wbpanel.pos` 从 `WB_W` 滑到 0，整块跟着动）。开的时候要 `panel.raise_()/rail.raise_()/grip.raise_()`（`setVisible(True)` 会把抽屉顶到最上层，不压回去就成了"盖着面板滑"）。**不 resize 窗口**，理由同上 |
| 卡片改宽度 / 改高度 | 拖动中实时跟手（不跑动画） | 抓**四条 12px 透明子控件**（`Card.grip_l/grip_r` 宽 `GRIP_W=12`；`grip_t/grip_b` 高 `GRIP_H=12`，上下两条左右各让开 `GRIP_W`）触发，光标分别是 ↔ / ↕。宽度：`setFixedWidth` + 重排（`_fit_height`）。高度：**绝不能直接 `resize`**（外层布局一激活就按 sizeHint 顶回去，实测会被顶到 1100px）—— 正确路径是改那块滚动区（详解/聊天）的 `setFixedHeight`，再让 `_fit_height()` 重新量一遍；短卡没有可伸缩内容时，多出来的一截进 `_v_stretch` 弹簧（按钮行因此仍贴底边），`show_brief` 里复位。两者都**没有过渡动画**——动画会让被拖的边滞后于光标。**别改回按坐标判边**：卡片最外 8px 是投影留白，描边在 x=8，按坐标判（x≤6）用户按描边时事件落在 `frame` 上、进不了 `Card.mousePressEvent`，会变成"拖不动/在挪窗口" |
| 卡片弹出 | 240ms + stagger 45ms | 滑入（从细边方向，x+24）+ 元素 stagger 淡入；**不走 windowOpacity 动画**（半透明无边框窗口上不可靠，见坑清单）；**`_stagger_prepare()` 必须在 `show()` 之前把内容静态钉 0**，stagger **必须等 pos 动画 done 再启动**。少了 prepare 就是"卡片带全部文字滑进来（已经能读）→ 滑完一起变 0 → 再淡回来"，用户的原话是"弹出的动画头尾反了"（实测 t≈0-200ms 全 vis、t≈240ms 一起 0.00） |
| 卡片关闭 | 150ms，InCubic | 向下 16px 滑出再 hide；**不走 windowOpacity 淡出**（同上，结尾会闪黑） |
| 卡片长高 | 240ms，OutCubic | `_resize_keep_place`：`SetNoConstraint` 布局 + 只量不设 + 钉 frame 高 + 动画期间钉窗口最大高度 + resizeEvent 自愈（缺一不可，见坑清单） |
| 内容揭示（详解/聊天） | 长高 240ms → 淡入 150ms | `_graceful_reveal`：长高期间把内容 opacity **静态钉 0**，长高结束才淡入。**绝不在窗口长高期间跑 opacity 动画**（残影 bug 的根源），静态 0 不发动画、不触发 |
| 复制成功 | 150ms | `flash_success()` 绿色脉冲（边框+字一起亮一下回落）；1.2s 后文案还原 |
| 钉住 | 150ms | `set_active()` 激活态淡入/淡出（蓝边框），不是一帧换皮 |
| 设置窗出场/退场 | 240ms / 120ms | show 淡入；`done()` 先 120ms InCubic 淡出再真正关闭，`_closing` 闸门防重入 |
| 减少动效 | 全部 1ms | `REDUCE["on"]`（设置窗里有开关）：所有动画经 `_motion()` 收敛，位移不再发生 |

规矩：
- 动画对象必须留引用（Qt 里被 GC 掉就等于"没动画"），统一走 `_anim()` 助手（内部存进
  模块级 `_ANIMS`，`finished` 时再移掉；同一对象同一属性先掐旧的再起新的）。
- 只动 `geometry` / `pos` / `windowOpacity` / 自绘过渡量，**不动布局属性**（动 min/max 尺寸会让 QSS 重算，抖）。
- 动画期间不许抢焦点：窗口的 `WA_ShowWithoutActivating` 一直保持（聊天区是唯一例外：用户主动点「问 AI」才 `activateWindow`）。
- 关闭类动画一律**先动画后 hide**，不要 hide 了再动画（会闪）。
- 合并连发：`Card._resize_keep_place()` 有 `_resizing` 闸门 + "高度差 < 20px 就跳过"，
  `hide_card()` 有 `_closing` 闸门 —— 动画跑一半被同一个属性上的第二次动画打断会闪。
- **内容自适应高度**：详解/聊天的高度跟内容走（`document().size()` 按**当前卡片宽**排版 ——
  `viewport().width()` 拿不到时才退回 `width()-54`，夹在 96/110 下限与屏高 45% 上限之间，
  超了自己滚）。排版宽度必须扣掉 QSS 的水平 padding（少扣一行就会把末行裁掉，实测）。
  不给固定 minimum 260——短内容底下空出一大块很简陋。
- **卡片宽可变、高随内容**（2026-10-03 定）：宽度 360~430 自适应（屏宽的 45%，上限 430），
  可拖到 `max(360, min(屏宽*0.45, 720))`；拖动后写回 `cfg["card_width"]`，下次弹卡沿用。
  高度永远由 `_fit_height()` 量出来，**不给卡片固定高度**。
- **动作按钮行能换行**：底部按钮行是 `FlowLayout`（详见 `_build` 的注释）。430 宽下
  5 个按钮（详解/问 AI/复制/钉住/✕）= 351px + 间距，可用 376px，正好一行；拖到 360 那档
  会换成两行而不是把按钮压扁。**「存词」不在这行**——它是词头右上角的 ☆/★。

## 落实现状（`gui.py`）

| 表里的东西 | 在哪 |
| --- | --- |
| 令牌 `T` / 时长 `DUR` | `gui.py` 顶部，`QSS = Template(...).substitute(T)` |
| 投影 `_shadow(w)` | 只给 `Card.frame`（`outer` 留 8/8/8/12 的边距让它画出来）；**Dock 面板不加**——贴屏幕边会被切 |
| 面板展开/收起 | `Dock._place()` + `_settle()`：窗口宽度**不变**（= 记词板区 + `_pan_w` + 缝 6 + 细边 26），只滑 panel；抽屉（wbpanel）**位置恒为 `(0,0)`**，开合只改 `Dock._wb_reveal` 再 `_apply_mask()`（铰链在 `x=WB_W`，见动效表"记词板开/关"）；`_settle()` 是唯一的收尾口（`_place(animate=False)` 也只调它）；rail 全程贴住窗口右缘（不能用 layout：窗口一变 layout 会把 rail 留在旧坐标上、跑到窗口外，见 `Dock._build` 的注释）。可点区 = rail ∪ panel（展开时）∪ 露出的那一条 wbpanel（左边 300px 透明区必须始终裁掉，否则挡住底下的窗口）；抽屉必须比 panel/rail 低一层（`panel.raise_()` 三次调用见 `toggle_wordbook`） |
| 记词板（存词） | 词头 ☆/★ → `Card.save_requested` → `App._on_save` → `Wordbook.add/remove` → `Dock.refresh_wordbook()`；导出 md/csv 在 `Wordbook.export_*`，`Dock._wb_export` 只负责选路径。**一键复制**：每行 `⧉` → `Dock._wb_copy(word, btn)`，底行「复制全部」→ `Dock._wb_copy_all()`（`"\n".join(词)` 进剪贴板，空板只给提示不写剪贴板），两者都 `flash_success()` + `set_hint()` 回执 |
| 面板宽度可拖 | `Dock.eventFilter` 里的 `self.grip` 分支（面板内侧 6px 抓边），夹在 `PANEL_W_MIN..PANEL_W_MAX`，松手 `_save()` 写回 `dock.panel_w` |
| 卡片尺寸可拖 | `Card.eventFilter` 四条抓边分支 → `_begin_edge(edge, gpos)` / `_drag_width()` / `_drag_height()` / `_end_edge()`；宽度边界 360..min(屏宽×45%,720) 写回 `card_width`，高度边界 = 详解 96 / 聊天 110 .. 一块屏装得下（`_box_limit`）。`_end_edge` 拖矮过就把滚动区的 `maximumHeight` 还原成 `_scroll_cap`（否则以后每次详解都被压在那个小高度上） |
| 卡片弹出 / 关闭 / 长高 | `Card.show_brief()` / `hide_card()+_really_hide()` / `_resize_keep_place()` |
| 托盘菜单三项 | `Tray.attach_dock()`（显示常驻面板 / 展开收起 / 面板挂在哪块屏） |
| 面板展开/收起 + 卡片动画 | `Dock._place()` + `_settle()` / `Card.show_brief()`、`_resize_keep_place()`；2026-09-28 起统一由 `tmp/ui_audit.py` 实跑断言（0 项未过） |
| 设置对话框出场 | 2026-09-28 补：`Settings.done()` 先跑 120ms InCubic 淡出、动画完回调里才 `super().done(r)`；`_closing` 闸门防重入（保存/取消/Esc 三条路都汇经 `done`） |
| 托盘菜单进场 | 2026-09-28 补：`Tray.eventFilter` 在 `Show` 时把菜单 `windowOpacity` 从 0 动到 1。**只做进不做出** —— QMenu 的收起时机拿不稳，硬做淡出会闪，违反"先动画后 hide"的规矩 |

### 写 QSS 时踩过的坑

- **QSS 里不能写 `#` 开头的注释**。Qt 的样式表只认 `/* */`，`#` 会被当成 ID 选择器的开头，
  写在一行规则末尾会让**它后面所有规则失效**（2026-09-28 实测：`QComboBox` 那行末尾加了句
  `# 7px：…` 注释，结果 `#chip`、`#src` 全部掉样式，`ui_audit.py` 立刻报胶囊高度回落到 32px）。
  要在 QSS 里注解只能用 `/* */`，或者干脆写在 `T` 的 Python 注释里。
- **圆角超过控件半高＝直角**。见上面圆角一节的修正：Qt 不钳制，静默失效。
- **QSS 不支持 `#a, #b {}` 逗号分组选择器**（2026-09-28 实测：写了 `#chip, #pill {}`，
  `#pill` 只拿到自己单独那条规则里的属性，分组块里的背景/边框/内边距全丢）。
  共用样式只能各写各的，或者用"基础规则 + 覆盖规则"两段写。
- **QLabel 的 wordWrap 救不了无空格长词**。45 个字母的单词没有断点，QTextOption 按词换行
  根本不断，最后是被布局压扁、连省略号都没有（实测词头被压到 255px）。超长无空格文本的
  常规做法是 **elide + tooltip**：显示宽度设上限，超出 `fontMetrics.elidedText`，全文进 tooltip。
- **换行布局必须自己接 `heightForWidth`**。QWidget 不会自动把内部布局的 heightForWidth 报给
  外层，只换 FlowLayout 不够，容器要写成 `hasHeightForWidth→True` 的子类（`FlowBox`），
  且布局项要取控件**实时** `sizeHint()`——布局项缓存的是样式生效前的尺寸，按缓存排会算出
  "一行放得下"、实际放不下（实测容器只给了应许高度的 1/3）。

## 长内容与极端数据（2026-09-29 定）

| 场景 | 处理 | 在哪 |
| --- | --- | --- |
| 超长单词（无空格） | 词头显示宽度上限 `max(200, 卡宽-170)`（430 宽时就是原来的 260px，卡拖宽后跟着放宽），超出省略号，完整词进 tooltip | `Card._word_cap()` + `#word` |
| 长短语的词胶囊 | FlowLayout 换行排，绝不挤压变形；数量上限 12 | `FlowLayout` / `FlowBox` |
| 超长详解 / 对话 | `detail` / `chat_log` 最大高度 = 可用屏高的 45%，超出自己滚；拖卡片上下边可以把这块滚动区拉大（上限 = 一块屏装得下，下限详解 96 / 聊天 110），拖完松手会还原自然上限 | `Card._build` / `Card._drag_height` |
| 卡片比可用区域还高 | `_place` 里上下都夹在屏幕内，不给内容掉出屏幕的机会 | `Card._place` |
| 表单太长 | 设置窗按"基本 / 屏幕识别 / 问答模型 / 进阶"分四组（轻量标题 `#sechead`，不加边框） | `Settings` |
| 设置窗比屏幕还高 | 装进 `QScrollArea` + 高度夹在可用屏高的 **80%**（2026-10-03 从 90% 收下来，理由见下一行）；`QScrollArea` 的 sizeHint 不等于内容高，**首次显示要按内容量一次**（否则缩成一小块） | `Settings.showEvent` |
| 设置窗滚不动（2026-10-03 修） | 两个原因：①**保存/取消按钮在滚动区里面**，一滚就跟着走 —— 已挪到滚动区外当固定页脚（`outer.addWidget(foot)`）；②`showEvent` 按内容量一次高度时顶到 90% 上限，矮屏"刚好装下"、滚动条永远不出现 —— 改成夹 `min(内容高, 可用屏高*0.8)`，并 `setSizeGripEnabled(True)` 让用户还能自己缩；滚轮落在下拉框上会被 QComboBox 吃掉（有焦点时去改选项），用 `eventFilter` 转发给 `_scroll.viewport()` | `Settings.showEvent` / `Settings.eventFilter` |
| 设置窗落在屏幕外 + 拖上下边"极其灵敏、上下两边同时在动"（2026-10-03 修） | 根因是**什么都没定位、交给 Windows 摆**：实测它摆到 `y=357`，而客户区高 921、窗框底 327+951=**1278 > 屏高 1152**，于是尾巴垂到屏幕外 126px —— 之后拖上/下边，系统一边把窗拉回屏内一边缩，手感就是"两边一起动"。现在 `showEvent` 把窗**居中夹进"常驻面板所在那块屏"**的可用区（`dock_panel._screen()` → 自己那块 → 主屏），上限也按那块屏重算：`h = min(内容高, 屏高*0.8, maximumHeight)`。**坑：`move()` 摆的是窗框、`geometry()/height()` 是客户区**，原生标题栏在 dpr1.25 上 30px，所以居中/夹取都得用 `frameGeometry().height()` 算，否则往下多探出标题栏那条 | `Settings.showEvent` |

## 引导块（onboarding，2026-09-29 定）

设置窗里凡是"用户不知道怎么弄"的可选配置（第一个是 DeepSeek），配一块 `#tip` 引导条，
固定三段式：

1. **先解除焦虑**：不接也能用，默认路径是什么（DeepSeek 这条是"本地 9B 免费"）；
2. **给可执行的步骤**：编号 ①②③，每步一个动作，不写"详见 README"；
3. **给确定答案**：一个「测一下」按钮 + 绿/红状态字（`providers.deepseek_up`，
   12 秒短超时、1 个 token 的请求，401/402/422 翻译成人话）。

引导块里的按钮用 ghost，不跟真正的设置项抢权重；填了 Key 之后三步收起，只留状态行。

## 八维度自检（2026-10-01，对标 Awwwards / Webby / FWA）

以获奖网站的评审维度逐条自检过 SnapWord（桌面浮层工具，"响应式"按 DPI/屏高/副屏转译）。
每条都附证据；"不再改"的也要写清判据，别拿"够好了"搪塞。

| 维度 | 本轮改动 | 证据 | 判"到顶"的依据 |
| --- | --- | --- | --- |
| 排版 | 释义/英文释义走富文本行高 1.65（`_para`）；词头字距 -0.4、音标/徽章 +0.3（字距随字号反向） | 同一 3 行文本 51px → 90px（实测） | QLabel 落不了 line-height 之外没有别的欠账；数字等宽对中文界面无感 |
| 留白 | 组内 4/8、组间 12~16；分隔线上下各让 4px | `test_a4_spacing_on_scale` 全刻度 | 组间距 > 组内间距已成立；再大会让浮层失去紧凑感 |
| 视觉层级 | 释义左侧 2px 渐隐竖线（`#cnbar`）；原句 12px `mute`（曾违规用 ash 承载正文，对比度只有 3.64） | `test_a5_ash_only_for_disabled`（按色值扫描） | 四段字号阶梯有断言；竖线+分组+语义色已够 |
| 色彩 | 备注行语义色圆点：绿=省了 token、蓝=联网来源、红=错误；来源与对照去重 | 渲染图；`test_a4_no_color_outside_tokens` | 语义色只在承载信息时用；危险色不给退出按钮（低频操作不值得夸大） |
| 动效 | 卡片元素 stagger 进场（45ms 间隔，顺序 = 布局上下顺序：词头→释义→对照→英文→备注）；**`_stagger_prepare()` 在 `show()` 前把内容钉 0**，且**必须等 pos 动画 done 再启动**——QGraphicsOpacityEffect 在窗口移动期间重绘不跟随，真机上把释义画到词头上（实测坑） | `test_b5_*` | 进/出/编排全齐；再叠就是炫技 |
| 微交互 | 按下时内容下压 1px（padding 7/5 对调，总高不变）；复制"✓ 已复制"式反馈早已有 | QSS pressed 规则 + `test_a6_button_states_defined` | 四态 + 反馈 + 光边生长已闭环 |
| 响应式 | 卡片宽 clamp 到 屏宽×45%（下限 360），高由内容定、可拖到"一块屏装得下"为止；设置窗滚动 + 最多 80% 屏高 | `test_a2_card_shows_without_crash`、`test_a3_settings_scrollable` | Qt 逻辑像素天然 DPI 无关；Dock 按屏钉死 |
| 原创性 | 卡片从细边方向滑出（空间叙事：卡片是从面板里"抽"出来的）；"省 token"用绿点可视化——把成本做成信息 | `test_b1_*`、`test_b2_*` | 光边/字标/monogram/滑出方向已构成完整识别系统 |

### 本轮新踩的坑

- **QGraphicsOpacityEffect 用完必须摘（`setGraphicsEffect(None)`）**。第一版以为只是"窗口
  pos 动画期间"会残影，实际是**effect 常驻期间任何几何变化都会坏**——拖动卡片、点按钮让
  卡片长高，文字就被画到旧位置，用户看到的是"框选后点一下就不能正常显示了"。
  实测：挂着 effect 时词头文字像素为 **0**（整个没画出来），摘掉后恢复正常。
  规则：opacity 效果只允许在入场那 200ms 存在，动画 `done` 回调里必须摘；
  `tests/test_ui.py` 的 `test_b5_stagger_cleans_up` 守着这条。
- **QGraphicsOpacityEffect + 窗口 pos 动画 = 残影**。效果的重绘不跟随窗口移动，
  offscreen 上看不出来，真机上释义直接叠到词头上。
- **按字样扫描令牌会漏**。QSS 里写的是 `$ash`，substitute 之后是色值；一致性测试必须扫
  substitute 后的最终样式表（第一版按 "ash" 字样扫，`#sd` 用 ash 承载正文漏过了）。
- **掐长高动画必须连高度上限一起放开**（2026-10-02）。长高期间窗口的 `maximumHeight`
  被钉在当前动画值上（挡那次延后的布局请求），动画正常结束由 `_resized()` 放开；但被
  `stop` 打断时 `finished` 不发，上限就永久留着（实测卡在 261px）。之后复用这张卡弹一个
  长卡片，窗口被钳成 261，底部复制/钉住按钮落在 y=558 ——**在窗口外，看不见也点不到**。
  所以一律走 `Card._stop_grow()`（掐动画 + 放开上限 + 复位闸门），`show_brief` 里再兜一次。
  `test_b5_grow_interrupt_releases_height_cap` 守着（已证伪：缺陷版报 244）。
- **卡片高度不能直接 `resize`**（2026-10-03）。外层布局一被激活就按 sizeHint 把窗口顶回去
  （实测想让 672，量出来窗口 1100、详解 871）—— 卡片高度从来不是独立变量，它永远等于
  "内容需要的高度"。所以"拖上下边改高度"走的是：改那块滚动区（详解/聊天）的
  `setFixedHeight`，再让 `_fit_height()` 重新量一遍（和 `_drag_width` 同一条路）。
  另外 `setFixedHeight` 会把 `minimumHeight` 一起抬上去，所以"还能缩多少"必须用设计下限
  （`_box_floor`：详解 96 / 聊天 110），**不能读 `minimumHeight()`**（拖过一次之后它恒等于当前高度）。
- **"活板门"不能靠挪控件实现**（2026-10-03）。第一版把抽屉的关闭位定在面板背后
  （`wbpanel.pos = WB_W`）、开合动画改 `pos`——用户看到的仍是"整块滑出来"。真正的
  揭示要让**被揭示的内容一动不动**，露多少由窗口 `setMask()` 给：`Dock._wb_reveal` 0..WB_W
  作为唯一变量，`_apply_mask()` 在**铰链侧**（靠面板那侧）长出 `[WB_W-r, WB_W)` 这一条。
  判据是"贴着铰链的像素先能点、远端最后出现"（`tmp/wb2_probe.py` 每 25ms 采 `mask().contains`）。
  同理，别用 `QVariantAnimation` 之外的东西记进度：直接把 `wbpanel.pos()` 当进度量，回不去。
- **`move()` 摆的是窗框，`geometry()/height()` 是客户区**（2026-10-03）。带原生边框的
  对话框上，标题栏在 dpr1.25 的屏上是 30px（实测 `frameGeometry=(717,100,613,951)` vs
  `geometry=(717,130,613,921)`，左右/下边框 0）。所以"居中/夹进可用屏"要用
  `frameGeometry().height()` 算，用客户区高会把窗子往下多推标题栏那一条。
  推论：**窗子绝不能有部分探出屏幕**——系统会自己把它拉回来、顺便缩尺寸，
  用户拖上下边时看到的就是"极其灵敏、上下两边同时在动"。
- **量"拖动改尺寸"别用 QTest**（2026-10-03）。`_drag_height` 会移动窗口（上边拖动时下边钉住、
  还要 `_clamp_pos`），而 QTest 把 local 坐标映射成 global 时跟着那个移动漂 —— 实测同一个
  向下的拖动被读成 -131 / -180，来回拖还会 ±96 振荡。要量就用自己造的 FakeEv，把**绝对全局
  坐标**喂给 `_begin_edge`/`_drag_height`（`tmp/h_probe.py` 就是这么量出 1:1 的）。
  同理，`childAt(点)` 才是"用户按的到底是哪个控件"的判据（QTest 不做命中测试，投给谁就发给谁）。
- **有道查不到时会把原词原样回显**（2026-10-02）。`_brief_youdao` 拿到的 `explains` 可能
  就是查询词本身（实测 `zzqxyzzy` → `['zzqxyzzy']`）。那不是释义，拿它当结果就是
  "识别出来的解释是英文"，而且会按顺序把后面的百度翻译、本地 9B 初稿全挡住。
  规则：释义与原词完全相同时视为未命中，继续往下兜底。
- **opacity 动画的父对象是 effect，裸删 effect 会留悬垂引用**（2026-10-02）。
  `_anim(eff, b"opacity", ...)` 把动画挂在 effect 上；清 stagger 时若裸调
  `setGraphicsEffect(None)`，effect 被删、动画跟着被删，但 `_ANIMS` 里的引用还在——
  下次遍历 `_ANIMS`（`_stop_anims`/`_anim` 都会遍历）直接 RuntimeError 崩溃。
  **触发条件极常见：查完一个词 700ms 内再查下一个**。规则：清 effect 必须走
  `_clear_stagger()`（先 stop + 摘列表再摘 effect）；换 effect 前先掐掉旧动画
  （`queue_reveal` 里就是这么做的）。`test_b5_stagger_reuse_no_dangling` 守着。
- **QLabel 的 heightForWidth 对富文本系统性低估**（2026-10-02）。322 字、line-height 1.55
  的正文，QLabel 报 273px，QTextDocument 实排要 470px——它连 line-height 都不算。
  后果是"先量后定"的新路径把正文裁掉小半截（旧代码是靠 show 后一次延后布局撞对高度）。
  修法：`RichLabel` 覆写 `heightForWidth/sizeHint` 用 QTextDocument 实排；**字体必须度量时
  同步**（setText 时机上 QSS 还没 polish，实测差 1.6 倍行高）；**度量前先
  `ensurePolished()`**；嵌套布局对 hfw 的传递不可靠（hfw 报 464 布局只分 299），所以
  `_fit_height` 在宽度定下后**手动把标签高度按 heightForWidth 钉进去**再量。
- **grab() 的像素是设备像素，DPR>1 的真机上逻辑坐标采样会采错位置**（2026-10-02）。
  本机 DPR=1.25，按逻辑坐标采主按钮渐变采到背景色。测试统一走 `grab_px()`（换算 DPR）；
  1px 边框在 DPR>1 下每个设备行都是混色（#2b2e34 vs 纯色 #33373d），断言按"接近 + 更亮"。
- **UI 测试套件默认跑真机平台，不跑 offscreen**（2026-10-02）。本项目已两次踩到
  "offscreen 完全复现不出、只有真机会"的缺陷（残影、悬垂引用崩溃），offscreen 套件给的是
  虚假安全感。需要无头跑时给 `SNAPWORD_TEST_OFFSCREEN=1`。
- **内容自适应高度要扣内边距**（2026-10-02）。`document().setTextWidth()` 给的是**文字区**宽，
  必须扣掉 QSS 的水平 padding（12×2）和边框——按 376 算出来 5 行，实际 352 排出 6 行，
  末行被裁掉还出了滚动条；垂直方向同理要留 padding+边框的余量。
- **异步补写的控件别"啪"出来**（2026-10-02）。详解、聊天区、有道对照行都是"等网络回来才
  show"的：长高期间把 opacity 静态钉 0，长高结束再淡入（`_graceful_reveal`）。之前详解是
  满透明度跟着长高一起出现，视觉上就是弹出来；而"跑着 opacity 动画跟长高重叠"又会触发
  残影 bug——静态 0 是两全：看不见（不闪）也不发动画（不残影）。
- **空的状态不许画壳**（2026-10-02）。来源徽章 `#src` 有边框有底，source 为空时只 setText("")
  会留一个空框浮在右上角——空的时候连框一起藏（`setVisible(bool(text))`）。

## 识别符号：一道光边（2026-09-28 定）

全应用只有一个识别符号：**一道蓝光的边**。它出现在三个地方，方向随表面走——

| 表面 | 光边长什么样 | 什么时候出现 |
| --- | --- | --- |
| 细边（Rail） | 左内缘 2px × 40px 的竖直蓝条，高度用 180ms 动画从 0 长出来 | 悬停时 |
| 面板（Dock） | 头部下方 1px 的水平蓝线，从左往右渐隐（`#streak`，`blue` → 透明） | 常驻 |
| 卡片（Card） | 顶沿 1px 描边用 `hairline-strong`（"顶沿受光"），底色走 `card → surface` 竖向渐变 | 常驻 |

规矩：光边是**唯一的**品牌强调，不许再给按钮、标题加第二处发光/渐变描边；
渐变的两个色标必须都来自令牌表（透明不算颜色）。主按钮的 `blue-hover → blue` 竖向渐变
是这条规矩的唯一例外（两端仍是令牌）。

细边（Rail）现在是整条自绘的（`class Rail`）：顶部 monogram 方块（blue-soft 底 + blue 的 S）、
中间把 "SnapWord" 整词旋转成竖排（字距连续，不再一个字母一行）、底部箭头随展开/收起翻向。
**别再往 Rail 里塞子控件**——鼠标事件会被子控件吃掉，"点细边展开"就断了（原实现靠
`WA_TransparentForMouseEvents` 补救，自绘后这个问题不存在了）。

**投影是实测过的**，不是"写了就算"：`grab()` 出来的卡片 PNG 左边缘外 x=2 是
`rgba(0,0,0,13)`、x=5 是 `rgba(0,0,0,24)`（渐隐），底部因为 y 偏移 6 更重（α=48）。
`grab()` 会把 `QGraphicsDropShadowEffect` 一起渲染，所以截图能当证据。

### 动效断言的判据必须"不靠采样序号"（2026-10-02）

`test_b5_grow_is_animated` 原来是 `max(seq[:4]) <= h0 + 20`——意思是"前 4 个采样的高度
不许已经推进"。这在机器忙的时候会**误报**：实测一帧的间隔能从 20ms 拖到 30ms+，
第 4 个采样点已经跑到 t≈100ms，OutCubic 本来就该走掉 40% 了，于是判成"一开局就跳"。
诊断数据：h0=186，t=0/0/28/48/69ms 都是 186，t=100ms 才 271（终点 385）——动画是健康的。

正确判据是 **`_resize_keep_place()` 返回后立刻读一次高度**（动画刚建好、还没推进的那一帧）：
它必须还在起点。老 bug（窗口在动画开始前就被布局撑到终点）会在这个值上现形，
而它跟采样节奏无关。证伪过：把"长高一步到位"装回去，这个值从 186 变成 386，断言立刻报。

### 执行环境可能根本没有桌面（2026-10-02）

在会话 0（服务会话）里跑测试，窗口能建、`grab()` 也有像素，但 **SendInput 一律返回 0 +
ERROR_ACCESS_DENIED(5)**——那里没有可交互桌面。判据：进程令牌的 `TokenSessionId`，
0 就是会话 0。

所以 `test_core.py` 里加了 `Skip` 状态：环境不具备时打印 `[SKIP]`，**既不通过也不失败**，
并且 `run_all.py` 会把它单独列进"未验证项"。规矩：跳过不许算通过（通过了是"验过没问题"，
跳过是"根本没验"），也不许算失败（ failures 会让人以为代码有 bug）。

**结论：这套门控请在自己的桌面终端里跑**（`python tests\run_all.py`），
会话 0 下 SendInput 那条必然是 `[SKIP]`。帧时序也可能跟真机不同——
"闪一下"这类观感问题最终得肉眼确认。

### 闸门跑完退不掉（2026-10-02）

`tests\run_all.py` 曾经**跑完不退**：UI 断言 36s + test_core 3s 就全跑完了，进程却挂到
560 秒还得手动 kill。最小复现：只 `import PySide6` 建个窗口再 `os._exit(0)`，退出要 26 秒+；
不碰 Qt 的同款脚本 2.2 秒就退。原因是 `os._exit` → `ExitProcess` 会跑一遍各 DLL 的
`DLL_PROCESS_DETACH`，Qt 的卸载钩子卡在里面。

解法是 `TerminateProcess`（不做那层卸载，实测 2.7 秒退出），见 `run_all.hard_exit()`。
两个细节：**`argtypes`/`restype` 必须声明**，否则 64 位句柄被当 `c_int` 截断，调用静默失败
（第一次就是这样，白等 26 秒）；退出前自己 `flush()`，硬终止不会再帮你刷缓冲区。
另外 `subprocess.run` 也补了 `timeout=180`，test_core 挂住时闸门不再干等。

### 展开面板的"闪"查到什么程度（2026-10-02，后已被"固定窗口宽度"根治）

逐帧量过：t=0 窗口已经从 26 撑到 352，面板静止在 x=346，**46ms 后第一帧直接跳到 31%**，
之后平滑到位。两条怀疑都排除了：

- 窗口变宽那 346px **不是**背景色块——`grab()` 出来是 `rgba(0,0,0,0)`，全透明，看不见。
- 细边（Rail）不移位：展开前后它的屏幕坐标都是 2022。

后来用户在真机上确认**真的闪**——根子不在 Qt 绘制层（所以 grab 抓不到），而在
Windows 合成器对**半透明窗口 resize** 的处理：先拿旧 buffer 拉伸显示一帧，rail 不透明，
那帧被拉长的 rail 就画在窗口左侧新增的 326px 里——正是"左侧一闪"。**根治：窗口固定
352 宽，展开/收起只滑 panel**（透明区用 `setMask` 让鼠标穿过去）。顺带把"resize 重排吃掉
动画头几帧（首帧 46ms 已 31%）"也一并消掉了——那 46ms 本来就是 resize 造成的。

### 半透明无边框窗口：windowOpacity 动画不可依赖（2026-10-02）

`WA_TranslucentBackground + FramelessWindowHint` 的顶层窗口在 Windows 上，windowOpacity
动画是 Qt 官方 bug 库里的老问题：要么直接跳变（QTBUG-33025）、要么结尾闪黑
（QTBUG-29010）、要么重绘错乱（QTBUG-28531）。用户在真机上看到的"弹出没有过渡、
闪动、虚影"三连就是它——我们在无桌面环境里量 windowOpacity **属性值**明明在平滑变化
（0→1），真机上却根本不可见。这种"属性在变、画面不变"的断层，只有真机合成器能暴露，
测试环境里量属性值是量不出来的。

**规矩：这类窗口的进出场过渡，只用位置动画（`pos`/`geometry`）+ 控件级
`QGraphicsOpacityEffect`（stagger），不用 windowOpacity。** QMenu/对话框那种普通 popup
不受影响（托盘菜单的淡入保留）。

