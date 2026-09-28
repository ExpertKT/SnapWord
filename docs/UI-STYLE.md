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
| 强描边 | `hairline-strong` | `#33373d` | 聚焦态/分隔线 |
| 主文字 | `ink` | `#f4f4f6` | 词头、标题（**不是纯白**） |
| 正文 | `body` | `#cdcdcd` | 释义 |
| 次要 | `mute` | `#9c9c9d` | 音标、元信息、备注 |
| 最弱 | `ash` | `#6a6b6c` | 禁用态 |
| 主强调 | `blue` | `#57c1ff` | 主按钮、来源徽章、rail 文字 |
| 强调底 | `blue-soft` | `rgba(87,193,255,0.15)` | 胶囊底色 |
| 成功/警告/错误 | `green` `#59d499` / `yellow` `#ffc533` / `red` `#ff6161` | | 只在真需要语义时用 |

圆角：`4 / 6 / 8 / 10 / 16 / 9999px`。**大多数控件用 6~10**，卡片 10，输入框和按钮 8，
细胶囊 4，整圆胶囊 9999。**卡片不允许 0 圆角。**

间距：基底 8px，允许 `2 / 4 / 8 / 12 / 16 / 24`。卡片内边距 12~14，卡片间距 8，
面板内边距 14，控件高度 32~36。

字号（Windows 上没有 Inter，退到 `Segoe UI` / `Microsoft YaHei`，字距按表里的正值加一点点）：
词头 24/600、正文 15/400（行高 1.6）、元信息 13、备注与徽章 11~12、按钮 12~13。

## 一条**故意偏离**参考表的决定

参考表说"整个系统没有投影，深度只靠 surface 阶梯"。**这一条不照搬**：Raycast 那个是网页，
背后永远是自己的画布；SnapWord 的卡片和面板是**浮动在任意桌面内容之上**的窗口，
纯靠颜色阶梯会和背后的白色网页糊在一起。所以保留**一层**很淡的投影
（`QGraphicsDropShadowEffect`：blur 24、`rgba(0,0,0,120)`、y 偏移 6），
其余一切照抄。**不许再加第二层投影、不许发光。**

## 动效（参考表没写动效，这部分按通行做法定）

| 场景 | 时长 | 缓动 | 做法 |
| --- | --- | --- | --- |
| 面板展开 / 收起 | 180ms | OutCubic | 动窗口的 `geometry`：宽度在 26 ↔ 352 两个确定值之间走，panel 同时在窗口内从 x=W 滑到 0（rail 永远贴窗口最右 26px） |
| 卡片弹出 | 180ms | OutCubic | 透明度 0→1 + y 从 `at.y()+10` 滑上来 |
| 卡片关闭 | 120ms | InCubic | 透明度 1→0，结束后才 `hide()` |
| 卡片长高（展开详解/对话） | 160ms | OutCubic | 动 `geometry` 的高度，锚定左上角 |
| 焦点/悬停变色 | — | — | QSS 直接切色（QSS 动画不了），所以**颜色差要够明显但不刺眼** |

规矩：
- 动画对象必须留引用（Qt 里被 GC 掉就等于"没动画"），统一走 `_anim()` 助手（内部存进
  模块级 `_ANIMS`，`finished` 时再移掉）。
- 只动 `geometry` / `pos` / `windowOpacity`，**不动布局属性**（动 min/max 尺寸会让 QSS 重算，抖）。
- 动画期间不许抢焦点：窗口的 `WA_ShowWithoutActivating` 一直保持。
- 关闭类动画一律**先动画后 hide**，不要 hide 了再动画（会闪）。
- 合并连发：`Card._resize_keep_place()` 有 `_resizing` 闸门 + "高度差 < 20px 就跳过"，
  `hide_card()` 有 `_closing` 闸门 —— 动画跑一半被同一个属性上的第二次动画打断会闪。

## 落实现状（`gui.py`）

| 表里的东西 | 在哪 |
| --- | --- |
| 令牌 `T` / 时长 `DUR` | `gui.py` 顶部，`QSS = Template(...).substitute(T)` |
| 投影 `_shadow(w)` | 只给 `Card.frame`（`outer` 留 8/8/8/12 的边距让它画出来）；**Dock 面板不加**——贴屏幕边会被切 |
| 面板展开/收起 | `Dock._place()` + `_settle()`：窗口宽度在 26 ↔ 352 之间走，panel 的 x 同步从 W 滑到 0；rail 全程贴住窗口右缘（不能用 layout：窗口一变窄 layout 会把 rail 留在旧坐标上、跑到窗口外，见 `Dock._build` 的注释） |
| 卡片弹出 / 关闭 / 长高 | `Card.show_brief()` / `hide_card()+_really_hide()` / `_resize_keep_place()` |
| 托盘菜单三项 | `Tray.attach_dock()`（显示常驻面板 / 展开收起 / 面板挂在哪块屏） |
| 还没做 | 托盘 QMenu 与设置 QDialog 的进出场动画、`QGraphicsDropShadowEffect` 实测只有一层 |

**投影是实测过的**，不是"写了就算"：`grab()` 出来的卡片 PNG 左边缘外 x=2 是
`rgba(0,0,0,13)`、x=5 是 `rgba(0,0,0,24)`（渐隐），底部因为 y 偏移 6 更重（α=48）。
`grab()` 会把 `QGraphicsDropShadowEffect` 一起渲染，所以截图能当证据。

