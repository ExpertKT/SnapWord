"""界面与交互的裸断言测试（headless，无网络）。

和 test_core.py 同一套风格：直接 `python tests\\test_ui.py`，全过打印 OK。
函数名前缀就是评分维度（tests\\run_all.py 按前缀算分）：

    a2 冷启动    a3 极端数据   a4 令牌一致性  a5 可访问性
    a6 状态完整  a7 键盘与习惯
    b1 光边      b2 自绘品牌   b3 质感       b4 入门引导   b5 动效

不依赖 ECDICT 真库，不联网，只要求装了 PySide6。
"""
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# **默认跑真机平台，不跑 offscreen。** 这不是随手选的：本项目已经两次踩到
# "offscreen 完全复现不出来、只有真机会"的缺陷——
#   ① QGraphicsOpacityEffect 在窗口几何变化期间重绘不跟随（文字画到旧位置）；
#   ② opacity 动画挂在 effect 上、裸删 effect 会留悬垂引用，遍历时 RuntimeError 崩溃。
# 两者在 offscreen 下都测不出来（合成器不同），套件会给出虚假的安全感。
# 需要无头跑（CI / 远程桌面没有会话）时给 SNAPWORD_TEST_OFFSCREEN=1。
if os.environ.get("SNAPWORD_TEST_OFFSCREEN") == "1":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 沙箱：Dock._save() 会写 $SNAPWORD_HOME/config.json，绝不能写到项目根
os.environ["SNAPWORD_HOME"] = str(ROOT / "tests" / "_sandbox")
os.makedirs(os.environ["SNAPWORD_HOME"], exist_ok=True)

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from snapword import gui  # noqa: E402

_APP = None
RADIUS_SCALE = {4, 6, 8, 10, 16}
# 整圆胶囊按控件半高取值，不落在通用刻度上（Qt 超过半高会静默变成直角，见 UI-STYLE.md）
PILL_RADIUS = {8, 12}                     # #chip（高 ~17）/ #pill（高 24）
SPACING_SCALE = {0, 2, 4, 8, 12, 14, 16, 24}   # 14 = 卡片/面板内边距，规范里单列的一档
# OCR demo 会画一张"白纸黑字"的合成图喂给识别引擎，那两个颜色不是界面颜色
DEMO_COLORS = {"#ffffff", "#000000"}
TOKEN_HEX = set()


def app():
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def wait(ms):
    """真等墙钟时间：QPropertyAnimation 按实际时间推进，光 processEvents 不够。"""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app().processEvents()
        time.sleep(0.005)


def grab_px(w, local_x, local_y):
    """在 w 所在顶层窗口的 grab 图上取一个像素。

    注意坐标是**设备像素**：真机 DPR=1.25 时 grab 图比逻辑坐标大，直接用逻辑坐标
    采样会采到别的位置（实测把主按钮的渐变采成了背景 #0f0f0f）。这里统一换算。
    """
    img = w.window().grab().toImage()
    dpr = img.devicePixelRatio() or 1.0
    p = w.mapTo(w.window(), QtCore.QPoint(int(local_x), int(local_y)))
    return img.pixelColor(int(p.x() * dpr), int(p.y() * dpr))


def lum(c):
    return 0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()


def contrast(a, b):
    la, lb = lum(QtGui.QColor(a)), lum(QtGui.QColor(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def card_of(cn=None, **kw):
    app()                      # 必须先有 QApplication：QWidget 早于它创建会被 Qt 直接 abort
    c = gui.Card()
    brief = {"query": "x", "lexeme": "x", "kind": "word", "cn": cn or ["释义"], "notes": []}
    brief.update(kw)
    c.show_brief(brief)
    wait(260)
    return c


# ---------------------------------------------------------------- A2 冷启动
def test_a2_dock_geometry_and_edge():
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    d = gui.Dock(cfg, lambda: None, lambda t: None, lambda: None, lambda: None, lambda: None)
    d.show()
    wait(300)
    g0 = d.geometry()
    scr = QtGui.QGuiApplication.primaryScreen().availableGeometry()
    # 窗口**固定宽度**（resize 半透明窗口会被 Windows 拿旧 buffer 拉伸一帧 ——
    # 用户看到的"展开时左侧一闪"就是它）= 记词板宽 + 主面板宽 + 缝 + 细边。
    # 记词板收起时那 300px 是透明的，靠 mask 裁掉，不然会挡住背后的应用。
    want_w = d.WB_W + d._pan_w + 6 + d.RAIL_W
    assert d.width() == want_w, "窗口应固定 %d 宽，实际 %d" % (want_w, d.width())
    assert g0.x() + g0.width() == scr.x() + scr.width(), "收起时没贴住屏幕右缘"
    assert not d.mask().isEmpty(), "收起时没设 mask（透明区会挡住背后的应用）"
    rail_screen_x = g0.x() + d.rail.geometry().x()
    assert not d.panel.isVisible(), "收起时 panel 应该藏着"
    d.toggle()
    wait(400)
    g1 = d.geometry()
    assert d.width() == want_w, "展开后仍应为 %d（不许再 resize），实际 %d" % (want_w, d.width())
    assert g1.x() + g1.width() == scr.x() + scr.width(), "展开后没贴住屏幕右缘"
    assert g1.x() + d.rail.geometry().x() == rail_screen_x, \
        "展开后 rail 的屏幕坐标变了（%d → %d）——细边移位" % (
            rail_screen_x, g1.x() + d.rail.geometry().x())
    # mask 现在永远是"按状态逐块裁出来的并集"（展开 + 记词板开着时不能 clearMask，
    # 窗口左边那 300px 透明区照样吃点击）。所以验的是"面板这一块在可点区里"。
    px = d.panel.geometry().center().x()
    assert d.mask().contains(QtCore.QPoint(px, 5)), "展开后 panel 被 mask 裁掉了（点不动）"
    assert d.panel.isVisible() and d.panel.pos().x() == d.WB_W, "展开后 panel 没到位"
    d.close()


def test_a2_card_shows_without_crash():
    c = card_of()
    # 响应式：430 是主尺寸，窄屏收到 屏宽*0.45（下限 360）
    scr = QtGui.QGuiApplication.primaryScreen().availableGeometry().width()
    want = max(360, min(430, int(scr * 0.45)))
    assert c.isVisible() and c.width() == want, "卡片宽度不对：%d（应为 %d）" % (c.width(), want)
    assert c.windowOpacity() >= 0.99, "淡入没跑完：%.2f" % c.windowOpacity()
    c.close()


def test_a2_no_focus_steal():
    """不抢焦点是这个工具的立身之本：卡片浮在别人正在用的程序上面，不能把键盘抢走。"""
    c = card_of()
    assert bool(c.testAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)), \
        "卡片没有 WA_ShowWithoutActivating"
    src = open(gui.__file__, encoding="utf-8").read()
    body = src.split("def show_brief")[1].split("\n    def ")[0]
    assert "activateWindow" not in body, "show_brief 里调了 activateWindow，会抢焦点"
    c.close()


# -------------------------------------------------------------- A3 极端数据
LONG_WORD = "pneumonoultramicroscopicsilicovolcanoconiosis"
LONG_CN = "释义" + "非常长的解释文本" * 40


def test_a3_long_word_elided():
    c = card_of(lexeme=LONG_WORD, query=LONG_WORD)
    assert c.lb_word.text() != LONG_WORD and c.lb_word.text().endswith("…"), \
        "超长词既没省略也没换行，会被压扁：%r" % c.lb_word.text()
    assert c.lb_word.toolTip() == LONG_WORD, "完整词没进 tooltip"
    assert c.lb_word.height() <= 40, "词头被撑成多行了（高 %d）" % c.lb_word.height()
    c.close()


def test_a3_chips_wrap_not_squeezed():
    c = gui.Card()
    c.show_brief({"query": "x", "lexeme": "x", "kind": "phrase", "cn": ["x"], "notes": []},
                 text=" ".join(["antidisestablishmentarianism"] * 14))
    wait(300)
    chips = [c.chips.itemAt(i).widget() for i in range(c.chips.count())]
    assert len(chips) >= 2, "没切出胶囊"
    assert min(w.width() for w in chips) >= 40, "胶囊被挤扁了：最窄 %dpx" % min(w.width() for w in chips)
    bottom = max(w.y() + w.height() for w in chips)
    assert bottom <= c.chips_box.height() + 1, \
        "换行后容器没跟着变高，内容被裁：内容底 %d / 容器 %d" % (bottom, c.chips_box.height())
    c.close()


def test_a3_long_content_capped():
    scr = QtGui.QGuiApplication.primaryScreen().availableGeometry()
    c = card_of(cn=[LONG_CN])
    c.detail.setPlainText("详解\n" + LONG_CN * 3)
    c.detail.show()
    c.frame.adjustSize()
    c.adjustSize()
    wait(300)
    assert c.height() <= scr.height(), "卡片比屏幕还高：%d / %d" % (c.height(), scr.height())
    assert c.detail.maximumHeight() > 0, "详没有设高度上限，超长内容会顶出屏幕"
    c.close()


def test_a3_card_clamped_in_screen():
    scr = QtGui.QGuiApplication.primaryScreen().availableGeometry()
    c = gui.Card()
    c.show_brief({"query": "x", "lexeme": "x", "kind": "word", "cn": [LONG_CN], "notes": []},
                 at=QtCore.QPoint(scr.x() + scr.width() - 20, scr.y() + scr.height() - 5))
    wait(300)
    assert c.y() + c.height() <= scr.y() + scr.height(), \
        "锚点在屏幕底部时卡片掉出去了：底边 %d / 屏底 %d" % (c.y() + c.height(), scr.y() + scr.height())
    c.close()


def test_a3_settings_scrollable():
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True}, "hotkey_selection": "ctrl+alt+S",
           "providers": {"deepseek": {"url": "https://x/v1/chat/completions", "model": "m", "key": ""},
                         "ollama": {"model": "q"}}}
    s = gui.Settings(cfg)
    s.show()
    wait(400)
    scr = QtGui.QGuiApplication.primaryScreen().availableGeometry()
    assert s.height() <= scr.height(), "设置窗比屏幕还高：%d / %d" % (s.height(), scr.height())
    assert s.height() >= 400, "设置窗被压扁了：%d" % s.height()
    assert getattr(s, "_scroll", None) is not None, "没装滚动区，矮屏上会有一截点不到"
    s.close()


# ---------------------------------------------------------- A4 令牌一致性
def _qss():
    return gui.QSS


def test_a4_no_color_outside_tokens():
    """QSS / 绘图代码里出现的颜色必须能在令牌表 T 里找到（透明不算颜色）。"""
    src = open(gui.__file__, encoding="utf-8").read()
    allowed = set(gui.T.values())
    bad = []
    for hexv in re.findall(r"#[0-9a-fA-F]{6}\b", _qss()):
        if hexv.lower() not in {v.lower() for v in allowed}:
            bad.append("QSS:" + hexv)
    for hexv in re.findall(r'QColor\("#([0-9a-fA-F]{6})"\)', src):
        if "#" + hexv.lower() in DEMO_COLORS:
            continue                      # 合成测试图，不是界面颜色
        if hexv.lower() not in {v.lower() for v in allowed}:
            bad.append("code:#" + hexv)
    assert not bad, "有令牌表外的颜色：%s" % bad


def test_a4_radius_on_scale():
    radii = [int(x) for x in re.findall(r"border-radius:\s*(\d+)px", _qss())]
    off = sorted({r for r in radii if r not in RADIUS_SCALE and r not in PILL_RADIUS})
    assert not off, "圆角不在刻度 4/6/8/10/16 上：%s" % off
    # 自绘的细边也用同一档
    src = open(gui.__file__, encoding="utf-8").read()
    assert "path.moveTo(8, 0)" in src or "path.moveTo(10, 0)" in src, "Rail 的圆角没跟 QSS 对齐"


def test_a4_spacing_on_scale():
    src = open(gui.__file__, encoding="utf-8").read()
    bad = []
    for val in re.findall(r"setSpacing\((\d+)\)", src):
        if int(val) not in SPACING_SCALE:
            bad.append("setSpacing(%s)" % val)
    for val in re.findall(r"setContentsMargins\(([^)]*)\)", src):
        nums = [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]
        if any(n not in SPACING_SCALE for n in nums):
            bad.append("margins(%s)" % val)
    assert not bad, "间距/内边距不在刻度上：%s" % bad


def test_a4_font_scale():
    """排版阶梯要真的递减：词头 > 释义 > 元信息 ≥ 备注。"""
    qss = _qss()
    sizes = {}
    for sel in ("#word", "#cn", "#phon", "#note"):
        m = re.search(r"%s\s*\{[^}]*font-size:\s*(\d+)px" % sel, qss)
        assert m, "%s 没定义字号" % sel
        sizes[sel] = int(m.group(1))
    assert sizes["#word"] > sizes["#cn"] > sizes["#phon"] >= sizes["#note"], \
        "字号阶梯不成立：%s" % sizes


def test_a4_pill_is_round():
    """整圆胶囊：Qt 的圆角超过半高会静默变直角，这条守住这个回归（左缘描边行数会暴增）。"""
    c = gui.Card()
    c.show_brief({"query": "x", "lexeme": "x", "kind": "phrase", "cn": ["x"], "notes": []},
                 text="the quick brown fox")
    wait(300)
    chips = [c.chips.itemAt(i).widget() for i in range(c.chips.count())]
    assert chips, "没切出胶囊"
    border = lum(QtGui.QColor(gui.T["hairline"]))
    img = c.grab().toImage()
    rows = 0
    for yy in range(chips[0].height()):
        q = img.pixelColor(*(chips[0].mapTo(c, QtCore.QPoint(0, yy)).toTuple()))
        if abs(lum(q) - border) <= 8:
            rows += 1
    assert rows <= 4, "胶囊不是整圆（左缘描边 %d 行，直角会是 %d 行）" % (rows, chips[0].height())
    c.close()


# ------------------------------------------------------------ A5 可访问性
def test_a5_body_contrast():
    """正文、次要文字对卡片底色的对比度要达到 WCAG AA（4.5:1）。"""
    bg = gui.T["surface"]
    for name, tok in (("正文 body", "body"), ("次要 mute", "mute"), ("强调 blue", "blue")):
        got = contrast(gui.T[tok], bg)
        assert got >= 4.5, "%s 对底色只有 %.2f:1（要 ≥4.5）" % (name, got)


def test_a5_focus_ring_contrast():
    """焦点环属于非文本元素，要 ≥3:1；hairline-strong 不够（1.62:1），所以统一用 blue。

    按钮已自绘（SmoothButton），焦点环在 paintEvent 里画——这里做**行为断言**：
    真的把焦点打上去、渲染出来、量边框像素的颜色，而不是扫样式表字样。"""
    got = contrast(gui.T["blue"], gui.T["surface"])
    assert got >= 3.0, "焦点环对比度 %.2f:1（要 ≥3）" % got
    # 输入框/下拉框的焦点环是自绘叠层（SmoothLineEdit/SmoothComboBox），行为断言在
    # test_b5_lineedit_focus_ring；QSS 里故意没有它们的 :focus 规则（会和淡入的环打架）
    qss = _qss()
    assert "QLineEdit:focus" not in qss and "QComboBox:focus" not in qss, \
        "QSS 里还留着一帧切换的焦点边框，会和自绘淡入环打架"
    b = gui.SmoothButton("焦点", "normal")
    b.resize(90, 32)
    b.show()
    # offscreen 下 setFocus 不一定投递 focusInEvent（窗口不会激活），直接驱动事件——
    # 断的是「过渡动画 + 自绘边框」这条路径，不是 Qt 的焦点投递
    b.focusInEvent(QtGui.QFocusEvent(QtCore.QEvent.Type.FocusIn))
    wait(250)                                  # focus 过渡跑完（fast = 150ms）
    img = b.grab().toImage()
    edge = img.pixelColor(0, b.height() // 2)  # 边框画在 0.5px 中心线上，落在第 0 列像素
    blue = QtGui.QColor(gui.T["blue"])
    dist = abs(edge.red() - blue.red()) + abs(edge.green() - blue.green()) + abs(edge.blue() - blue.blue())
    assert dist < 90, "聚焦后边框不是 blue（取到 #%02x%02x%02x，离 blue 差 %d）" % (
        edge.red(), edge.green(), edge.blue(), dist)
    b.close()


def test_a5_ash_only_for_disabled():
    """ash 只有 3.64:1，只能出现在禁用态，不能承载正文。

    注意要按**色值**认（substitute 之后的 #6a6b6c），不能按 "ash" 字样——
    QSS 里写的是 $ash 变量，按字样扫会漏（第一版就漏过了 #sd）。
    """
    ash = gui.T["ash"].lower()
    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", _qss()):
        sel = sel.strip()
        low = body.lower()
        if ("#" + ash.lstrip("#")) in low or "$ash" in body:
            if ":disabled" not in sel:
                raise AssertionError("ash 用在了非禁用态：%s" % sel)


# ------------------------------------------------------------ A6 状态完整
def test_a6_button_states_defined():
    """按钮的全部状态在 BTN_PAL / SmoothButton 里有定义——不再是扫 QSS 字样，
    而是核对真实参与渲染的数据结构与行为钩子。"""
    # 每种按钮都要能表达：默认、悬停、按下、禁用、聚焦
    for kind, pal in gui.BTN_PAL.items():
        assert "hover" in pal, "%s 缺悬停态" % kind
        assert "text" in pal, "%s 缺文字色" % kind
        if not pal.get("grad"):               # 非渐变按钮要显式给按下色（渐变按钮按下在 paintEvent 里压暗）
            assert "press" in pal, "%s 缺按下态" % kind
    b = gui.SmoothButton("x", "normal")
    for prop in ("hov", "prs", "foc"):
        assert hasattr(b, prop), "SmoothButton 缺可动画的 %s 过渡量" % prop
    # 禁用态：paintEvent 里用 ash 画字、且 _go 在禁用时不动画
    src = Path(gui.__file__).read_text(encoding="utf-8")
    assert 'T["ash"]' in src, "禁用态没有落到 ash 令牌"
    b.setEnabled(False)
    b.enterEvent(QtGui.QEnterEvent(QtCore.QPointF(1, 1), QtCore.QPointF(1, 1), QtCore.QPointF(1, 1)))
    wait(120)
    assert b._hov < 0.01, "禁用按钮还在跑 hover 动画（disabled means disabled）"
    b.close()


def test_a6_settings_apply_writes_back():
    """改了设置要点保存才生效——这条路径坏了，用户会以为软件没记住。"""
    cfg = {"hotkey": "ctrl+shift+D", "hotkey_selection": "ctrl+alt+S", "ocr_engine": "auto",
           "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "https://x/v1/chat/completions", "model": "m", "key": ""},
                         "ollama": {"model": "q"}}}
    s = gui.Settings(cfg)
    s.hotkey.setText("ctrl+alt+Z")
    s.key.setText("sk-abc")
    s.apply()
    assert cfg["hotkey"] == "ctrl+alt+Z", "热键没写回 config：%r" % cfg["hotkey"]
    assert cfg["providers"]["deepseek"]["key"] == "sk-abc", "Key 没写回 config"
    s.close()


def test_a6_chip_states_defined():
    """胶囊（版本徽章 chip / 词胶囊 pill）现在是 SmoothButton 的 kind——
    状态在 BTN_PAL 里，圆角按控件半高取（Qt 超过半高会静默画成直角，见 UI-STYLE.md）。"""
    for kind, half in (("chip", 8.5), ("pill", 12)):
        pal = gui.BTN_PAL.get(kind)
        assert pal, "BTN_PAL 缺 %s" % kind
        assert "hover" in pal and "press" in pal, "%s 缺悬停/按下态" % kind
        assert pal["radius"] <= half, "%s 圆角 %d 超过半高 %.1f（会被画成直角）" % (
            kind, pal["radius"], half)


# ---------------------------------------------------------- A7 键盘与习惯
def test_a7_escape_closes_card():
    c = card_of()
    c.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Escape,
                                    QtCore.Qt.KeyboardModifier.NoModifier))
    wait(220)
    assert not c.isVisible(), "按 Esc 没关掉卡片"
    c.close()


def test_a7_copy_feedback():
    c = card_of()
    assert c.btn_copy.text() == "复制", "复制按钮初始文案不对：%r" % c.btn_copy.text()
    c._copy()
    assert c.btn_copy.text() == "已复制", "点了复制没有反馈"
    c.close()


def test_a7_enter_sends_chat():
    c = card_of()
    c._toggle_chat()
    wait(120)
    assert c.chat_row.isVisible(), "「问 AI」没有展开提问区"
    assert "▴" in c.btn_chat.text(), "展开后按钮没有展开指示：%r" % c.btn_chat.text()
    got = []
    c.ask_requested.connect(lambda *a: got.append(a))
    c.chat_in.setText("区别是什么")
    c.chat_in.returnPressed.emit()
    assert got and got[0][1] == "区别是什么", "回车没把问题发出去"
    c.close()


def test_a7_tray_menu_items():
    """托盘右键是这个工具的主入口之一：项要齐，而且要都是中文。"""

    class _App:
        def start_pick(self):
            pass

        def lookup_text(self, t):
            pass

    t = gui.Tray(_App(), "ctrl+shift+D", lambda: None, lambda: None)
    texts = [a.text() for a in t._menu.actions() if not a.isSeparator()]
    assert len(texts) >= 4, "托盘菜单项太少：%s" % texts
    assert all(re.search(r"[\u4e00-\u9fff]", x) for x in texts), "有菜单项不是中文：%s" % texts
    assert any("设置" in x for x in texts), "缺设置：%s" % texts
    assert any("退出" in x for x in texts), "缺退出：%s" % texts


# ---------------------------------------------------------------- B1 光边
def test_b1_rail_light_bar_animates():
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    d = gui.Dock(cfg, lambda: None, lambda t: None, lambda: None, lambda: None, lambda: None)
    d.show()
    wait(250)
    r = d.rail
    assert hasattr(r, "barH"), "细边没有光边属性"
    r.enterEvent(QtCore.QEvent(QtCore.QEvent.Type.Enter))
    wait(280)
    on = r.barH
    r.leaveEvent(QtCore.QEvent(QtCore.QEvent.Type.Leave))
    wait(220)
    assert on >= 36, "悬停时光边没长出来：%d" % on
    assert r.barH == 0, "移开后光边没收回去：%d" % r.barH
    d.close()


def test_b1_streak_present():
    assert "#streak" in _qss(), "面板头下那道渐隐蓝线没了"
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    d = gui.Dock(cfg, lambda: None, lambda t: None, lambda: None, lambda: None, lambda: None)
    d.show()
    d.toggle()
    wait(400)
    names = {w.objectName() for w in d.panel.findChildren(QtWidgets.QFrame)}
    assert "streak" in names, "面板里没有 streak 控件"
    d.close()


def test_b1_card_top_highlight():
    c = card_of()
    # DPR>1 时 1px 边框落在亚像素上，每个设备行都是混色（实测 #2b2e34 vs 纯色 #33373d），
    # 精确比对永远不中。按意图断言：顶沿最亮像素接近 hairline_strong，且明显亮于下方表面。
    best, best_l = None, -1.0
    for y in range(0, 4):
        q = grab_px(c.frame, c.frame.width() // 2, y)
        l = lum(q)
        if l > best_l:
            best, best_l = q, l
    want = QtGui.QColor(gui.T["hairline_strong"])
    dist = abs(best.red() - want.red()) + abs(best.green() - want.green()) + abs(best.blue() - want.blue())
    below = grab_px(c.frame, c.frame.width() // 2, 8)
    assert dist <= 32, "卡片顶沿不是受光边（差 %d）：%s" % (dist, best.name())
    assert best_l > lum(below) + 0.05, "顶沿没有比卡面亮（%.2f vs %.2f）" % (best_l, lum(below))
    c.close()


# ------------------------------------------------------------ B2 自绘品牌
def test_b2_rail_painted_no_children():
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    d = gui.Dock(cfg, lambda: None, lambda t: None, lambda: None, lambda: None, lambda: None)
    d.show()
    wait(250)
    assert isinstance(d.rail, gui.Rail), "细边不是自绘的 Rail"
    assert len(d.rail.findChildren(QtWidgets.QLabel)) == 0, \
        "细边里又有子标签了（会把鼠标事件吃掉，点不开面板）"
    d.close()


def test_b2_rail_monogram_and_wordmark():
    src = open(gui.__file__, encoding="utf-8").read()
    assert '"S"' in src and "monogram" in src, "细边没有 monogram 方块"
    assert "SnapWord" in src and "rotate(-90)" in src, "竖排字标不是整词旋转画的"


# -------------------------------------------------------------- B3 质感
def test_b3_primary_button_gradient():
    c = card_of()
    b = c.btn_detail
    top = grab_px(b, b.width() // 2, 3)
    bot = grab_px(b, b.width() // 2, b.height() - 4)
    assert top != bot, "主按钮是纯色块，没有渐变"
    blueish = lambda q: 80 <= q.red() <= 130 and 185 <= q.green() <= 215 and q.blue() >= 250
    assert blueish(top) and blueish(bot), "渐变两端都应在 blue 家族内：%s / %s" % (top.name(), bot.name())
    c.close()


def test_b3_card_gradient():
    c = card_of()
    img = c.grab().toImage()
    up = img.pixelColor(c.frame.x() + 4, c.frame.y() + int(c.frame.height() * 0.25))
    dn = img.pixelColor(c.frame.x() + 4, c.frame.y() + int(c.frame.height() * 0.85))
    assert lum(up) > lum(dn), "卡片不是上亮下暗的竖向渐变"
    lo, hi = lum(QtGui.QColor(gui.T["surface"])), lum(QtGui.QColor(gui.T["card"]))
    assert lo - 1 <= lum(dn) and lum(up) <= hi + 1, "渐变超出了 card/surface 两档"
    c.close()


# ---------------------------------------------------------- B4 入门引导
def test_b4_ds_guide_states():
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "https://api.deepseek.com/v1/chat/completions",
                                      "model": "deepseek-chat", "key": ""},
                         "ollama": {"model": "q"}}}
    s = gui.Settings(cfg)
    s.show()
    wait(300)
    assert s.ds_steps.isVisibleTo(s), "没填 Key 时不显示三步引导"
    assert not s.btn_ds_test.isEnabled(), "没填 Key 却让「测一下」可点"
    s.key.setText("sk-test")
    app().processEvents()
    assert not s.ds_steps.isVisibleTo(s), "填了 Key 还占着三步的位置"
    assert s.btn_ds_test.isEnabled(), "填了 Key 还不能测"
    assert "已填" in s.lb_ds_state.text(), "状态字没跟着变：%r" % s.lb_ds_state.text()
    s.close()


# ---------------------------------------------------------------- B5 动效
def test_b5_dock_toggle_animation():
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": False},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    d = gui.Dock(cfg, lambda: None, lambda t: None, lambda: None, lambda: None, lambda: None)
    d.show()
    wait(250)
    d.toggle()
    wait(40)
    mid = d.panel.pos().x()
    wait(400)
    assert mid != d.WB_W, "面板是瞬间出现的，没有滑出动画"
    assert d.panel.pos().x() == d.WB_W, "动画结束后面板没到位：x=%d" % d.panel.pos().x()
    d.close()


def test_b5_card_fade_and_hide():
    c = card_of()
    c._closing = False
    c.hide_card()
    wait(60)
    assert c.isVisible(), "关闭动画期间窗口就不见了（会闪）"
    wait(220)
    assert not c.isVisible(), "关闭动画结束后没有 hide"
    c.close()


def test_b5_grow_is_animated():
    """卡片长高必须逐帧推进，不能"啪"一下到终点。

    这个 bug 的成因值得记：`detail.show()` 会让**顶层窗口**的布局立刻把窗口撑到新高度，
    等 `_resize_keep_place()` 再去量，old 已经等于 new，动画根本没被创建。
    所以这里测的是"有没有中间帧"，而不是"动画对象在不在"。
    """
    c = card_of()
    wait(900)
    h0 = c.height()
    c.detail.setPlainText("详解\n" + "内容" * 150)
    c.detail.show()
    c._resize_keep_place()
    # 动画刚建好、还没推进的那一帧。老 bug 就死在这里：detail.show() 让布局立刻把窗口撑到
    # 终点，等去量时 old 已经等于 new，动画压根没被创建 —— 判定必须看这一帧，而不是
    # "前几个采样值"：采样节奏一慢（机器忙的时候一帧能到 30ms），前几帧本来就该推进了，
    # 按采样序号判会误报。
    h_at_start = c.height()
    seq = []
    end = time.monotonic() + 0.34
    while time.monotonic() < end:
        seq.append(c.height())
        app().processEvents()
        time.sleep(0.02)
    final = c.height()
    mid = sorted({h for h in seq if h0 + 20 < h < final - 20})
    assert final > h0 + 100, "高度根本没变：%d → %d" % (h0, final)
    assert h_at_start <= h0 + 20, \
        "动画还没跑，窗口已经被撑到 %d（起点 %d、终点 %d）—— 就是那个'啪一下'" % (
            h_at_start, h0, final)
    assert len(mid) >= 2, "长高没有中间帧（瞬间跳变）：%s" % seq[:8]
    c.close()


def test_b5_detail_reveals_gracefully():
    """点「详细解释」后详解**必须淡入**，不能随长高"啪"出来；长高结束把 effect 摘掉。

    做法见 _graceful_reveal：长高期间把详解 opacity 钉 0，长高结束再淡入。检验两件事：
    (1) 长高刚开始时详解是透明的（抓"啪一下"这个老毛病）；
    (2) 收尾后没有残留的 QGraphicsOpacityEffect（否则窗口一变几何就画到旧位置）。
    """
    c = card_of()
    wait(900)
    c.set_detail("详解内容 " * 60)
    # 长高刚开始（动画跑起来之前）：
    wait(20)
    eff = c.detail.graphicsEffect()
    assert eff is not None, "长高期间详解没被钉在透明（会随长高啪出来）"
    assert abs(eff.opacity()) < 0.05, "长高期间详解不是透明的：opacity=%.2f" % eff.opacity()
    wait(500)                                  # 长高(240) + 淡入(150) 都跑完
    assert c.detail.isVisible(), "详解淡入后没显示"
    assert c.detail.graphicsEffect() is None, "详解揭示完还挂着 opacity effect（残留->残影风险）"
    c.close()


def test_b5_stagger_reuse_no_dangling():
    """复用卡片查第二个词时，不能把 opacity 动画的悬垂引用留在 `_ANIMS` 里。

    opacity 动画的父对象是 effect（`_anim(eff, b"opacity", ...)`）。清上一轮 stagger 时
    如果裸调 `setGraphicsEffect(None)`，effect 被删 → 动画对象跟着被删，但 `_ANIMS` 还
    留着它的引用。下一次遍历 `_ANIMS`（`_stop_anims` / `_anim` 都会遍历）就抛
    RuntimeError「Internal C++ object already deleted」——真机上就是查第二个词时卡死/闪。
    修法是清 stagger 必须走 `_clear_stagger()`（先 stop + 摘列表，再摘 effect）。
    """
    c = card_of()
    # 轮询找 stagger **真正在飞**的时刻（入场 240ms 后才启动，各元素间隔 45ms）：
    # 写死等待会随时序抖动失效，扫到为止才稳。
    flying = False
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline:
        if [w for w in c._staggered if w.graphicsEffect() is not None]:
            flying = True
            break
        app().processEvents()
        time.sleep(0.01)
    assert flying, "整段都没扫到 stagger 在跑（这条断言测不到东西，时序变了）"
    c.show_brief({"query": "ephemeral", "lexeme": "ephemeral", "kind": "word",
                  "cn": ["短暂的"], "notes": []})
    # 全程高频遍历 _ANIMS：修复前这里会抛 RuntimeError
    end = time.monotonic() + 0.9
    while time.monotonic() < end:
        for a in list(gui._ANIMS):             # 不 try：崩了就该让测试红
            a.targetObject()
            a.propertyName()
            a.state()
        app().processEvents()
        time.sleep(0.008)
    wait(400)
    assert not [w for w in c._staggered if w.graphicsEffect() is not None], \
        "复用后还有控件挂着 opacity effect（残影风险）"
    assert c.lb_word.text(), "复用后词头是空的"
    c.close()


def test_b5_reveal_never_stuck():
    """揭示**绝不能永久透明**：哪怕长高闸门卡住，内容也必须出现。

    `_after_grow()` 会等长高动画 done 才淡入。这依赖 `_resizing` 最终被清掉；一旦有
    哪条路径把它卡在 True（比如某处 stop 了 geometry 动画但没发 finished），无限重排
    就等于"点了详解却什么都没有"——用户视角是"不能正常使用"。所以重排必须有上限。
    这里人为把闸门焊死，验证有界重排后内容照样淡入。
    """
    c = card_of()
    wait(900)
    c._resizing = True                  # 焊死：模拟长高动画永远不结束
    c.set_detail("闸门卡死时的详解内容 " * 40)
    wait(1200)                          # 4 轮重排 × 150ms + 淡入，足够触发上限
    assert c.detail.isVisible(), "闸门卡死时详解没显示出来"
    e = c.detail.graphicsEffect()
    op = e.opacity() if e is not None else 1.0
    assert op > 0.9, "闸门卡死时详解停在半透明 opacity=%.2f（内容等于看不见）" % op
    c._resizing = False
    c.close()


def test_b5_dock_double_toggle():
    """展开动画没跑完就再点一次（连点细边），面板不能卡在半路。"""
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    d = gui.Dock(cfg, lambda: None, lambda t: None, lambda: None, lambda: None, lambda: None)
    d.show()
    wait(200)
    d.toggle()                 # 展开
    wait(60)
    d.toggle()                 # 动画中途收起
    wait(700)
    assert not d.expanded, "状态没回到收起"
    assert not d.panel.isVisible(), "面板没收起来"
    assert not d.mask().isEmpty(), "收起后 mask 没恢复（透明区会挡鼠标）"
    d.close()


def test_b5_grow_interrupt_releases_height_cap():
    """长高动画被打断后，窗口的高度上限必须放开。

    长高期间窗口 maximumHeight 被钉在当前动画值上挡那次延后的布局请求；动画正常结束由
    `_resized()` 放开，但被 stop 打断时 `finished` 不发，上限就永久留着（实测卡在 261）。
    之后复用这张卡弹一个长卡片，窗口被钳成 261，底部的复制/钉住按钮落在 y=558 ——
    **在窗口外，既看不见也点不到**（用户报的"详解生成时不能复制不能钉住"）。
    """
    c = card_of()
    wait(700)
    c.detail.show()
    c.detail.setPlainText("详解内容 " * 120)
    c._resize_keep_place()
    wait(60)                                    # 长高动画进行中
    c.hide_card()                               # 中途打断（Esc / 点 ✕ / 查新词）
    wait(400)
    assert c.maximumHeight() > 100000, \
        "打断后窗口高度上限没放开：%d（卡片以后会被钳住，按钮点不到）" % c.maximumHeight()
    # 复用这张卡弹一个长卡片，按钮必须还在窗口里
    c._closing = False
    c.show_brief({"query": "y", "lexeme": "y", "kind": "word", "cn": [LONG_CN], "notes": []})
    wait(600)
    btn_y = c.btn_copy.mapTo(c, QtCore.QPoint(0, 0)).y()
    assert btn_y + c.btn_copy.height() <= c.height(), \
        "复制按钮掉到窗口外了（y=%d + 高 %d > 窗口 %d）" % (
            btn_y, c.btn_copy.height(), c.height())
    c.close()


def test_b5_rapid_hover():
    """快速划过细边：来回触发光边动画，最终必须收敛（不卡在中间值）。"""
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    d = gui.Dock(cfg, lambda: None, lambda t: None, lambda: None, lambda: None, lambda: None)
    d.show()
    wait(200)
    r = d.rail
    r.enterEvent(QtCore.QEvent(QtCore.QEvent.Type.Enter))
    wait(30)
    r.leaveEvent(QtCore.QEvent(QtCore.QEvent.Type.Leave))
    wait(30)
    r.enterEvent(QtCore.QEvent(QtCore.QEvent.Type.Enter))
    wait(400)
    assert r.barH == 40, "来回划过后光边没到满值：%d" % r.barH
    r.leaveEvent(QtCore.QEvent(QtCore.QEvent.Type.Leave))
    wait(400)
    assert r.barH == 0, "移开后光边没收干净：%d" % r.barH
    d.close()


def test_b5_resize_during_entrance():
    """入场滑入还没跑完内容就变了（有道回来）：pos 与 geometry 两个动画会每帧互相覆盖。

    修法：长高动画接管前先把滑入掐掉。这里验证最终尺寸稳定、没有动画挂着。
    """
    c = card_of()
    wait(40)
    c.detail.setPlainText("详解\n" + "内容" * 200)
    c.detail.show()
    c._resize_keep_place()
    wait(700)
    c.frame.adjustSize()
    c.adjustSize()
    wait(60)
    assert abs(c.height() - c.frame.height() - 20) < 40, \
        "卡片尺寸没稳定：卡片 %d / frame %d" % (c.height(), c.frame.height())
    left = [a for a in gui._ANIMS if a.targetObject() is c]
    assert not left, "还有 %d 个动画没跑完" % len(left)
    c.close()


def test_b5_stagger_cleans_up():
    """stagger 用完必须把 effect 摘掉。

    挂着 QGraphicsOpacityEffect 的控件在窗口几何变化（拖动、卡片长高）期间重绘不跟随，
    真机上的症状就是"点一下就不能正常显示了"——文字被画到旧位置。
    """
    c = card_of()
    wait(1200)          # 等 stagger 全部跑完（入场 180 + 4×45 + 200）
    left = [w for w in getattr(c, "_staggered", []) if w.graphicsEffect() is not None]
    assert not left, "还有 %d 个控件挂着 opacity 效果（会残影）" % len(left)
    c.close()


def test_b5_button_hover_transitions():
    """hover 是过渡不是跳变，且**离开比进入慢**（外部规范：进入即时、离开柔和）。"""
    b = gui.SmoothButton("悬停", "normal")
    b.resize(90, 32)
    b.show()
    wait(300)                          # show 时 offscreen 会自发派发 enter/focus，先尘埃落定
    # 直接进入：采样驱动（鼠标进入）
    b.set_hov(0.0)                     # 复位到未悬停，模拟"还没指上去"
    b.enterEvent(QtGui.QEnterEvent(QtCore.QPointF(1, 1), QtCore.QPointF(1, 1), QtCore.QPointF(1, 1)))
    seq_in = []
    end = time.monotonic() + 0.15
    while time.monotonic() < end:
        seq_in.append(round(b._hov, 2)); app().processEvents(); time.sleep(0.008)
    mid_in = [v for v in seq_in if 0.05 < v < 0.95]
    b.leaveEvent(QtCore.QEvent(QtCore.QEvent.Type.Leave))
    seq_out = []
    end = time.monotonic() + 0.25
    while time.monotonic() < end:
        seq_out.append(round(b._hov, 2)); app().processEvents(); time.sleep(0.008)
    mid_out = [v for v in seq_out if 0.05 < v < 0.95]
    assert b._hov < 0.01, "离开后没回到 0"
    assert mid_in, "hover 进入没有中间帧（一帧跳变）：%s" % seq_in[:6]
    assert len(mid_out) > len(mid_in), "离开应当比进入慢（中间帧 %d 应多于进入 %d）" % (
        len(mid_out), len(mid_in))
    b.close()


def test_b5_press_shifts_text():
    """按下时文字下沉 1px（规范：scale 0.97~0.98 的按压反馈，Qt 里用位移等效）。"""
    b = gui.SmoothButton("按我", "normal")
    b.resize(90, 32)
    b.show()
    wait(200)
    from PySide6.QtTest import QTest
    QTest.mousePress(b, QtCore.Qt.MouseButton.LeftButton)
    wait(120)
    assert b._prs > 0.5, "按下后 prs 过渡量没起来：%.2f" % b._prs
    QTest.mouseRelease(b, QtCore.Qt.MouseButton.LeftButton)
    wait(200)
    assert b._prs < 0.01, "松开后 prs 没回弹：%.2f" % b._prs
    b.close()


def test_b5_copy_flash_success():
    """复制的绿色脉冲：suc 有过渡地升到 1，停一会儿再落回 0。"""
    c = card_of()
    wait(900)
    c._copy()
    seq = []
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        seq.append(round(c.btn_copy._suc, 2)); app().processEvents(); time.sleep(0.012)
    mid = [v for v in seq if 0.05 < v < 0.95]
    assert mid, "成功脉冲没有中间帧（一帧跳变）：%s" % seq[:6]
    assert c.btn_copy._suc > 0.9, "脉冲没到顶：%.2f" % c.btn_copy._suc
    wait(1600)                         # 900ms 后开始回落
    assert c.btn_copy._suc < 0.05, "脉冲没回落：%.2f" % c.btn_copy._suc
    assert c.btn_copy.text() == "复制", "文案没还原：%r" % c.btn_copy.text()
    c.close()


def test_b5_pin_active_animates():
    """钉住的激活态淡入淡出（iOS toggle 那种连续变化），不是一帧换皮。"""
    c = card_of()
    wait(900)
    c._toggle_pin()
    seq = []
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        seq.append(round(c.btn_pin._act, 2)); app().processEvents(); time.sleep(0.012)
    mid = [v for v in seq if 0.05 < v < 0.95]
    assert mid, "激活态没有中间帧：%s" % seq[:6]
    assert c.btn_pin._act > 0.9, "激活态没到 1：%.2f" % c.btn_pin._act
    c._toggle_pin()
    wait(300)
    assert c.btn_pin._act < 0.05, "取消钉住后激活态没退掉：%.2f" % c.btn_pin._act
    c.close()


def test_b5_lineedit_focus_ring():
    """输入框焦点环淡入淡出，且焦点到位时环真的是蓝色。"""
    le = gui.SmoothLineEdit("hello")
    le.resize(200, 32)
    le.show()
    wait(200)
    le.set_foc(0.0)      # offscreen 下 show() 会自发派发一次真实 focusIn（先尘埃落定再复位）
    le.focusInEvent(QtGui.QFocusEvent(QtCore.QEvent.Type.FocusIn))
    seq = []
    end = time.monotonic() + 0.25
    while time.monotonic() < end:
        seq.append(round(le._foc, 2)); app().processEvents(); time.sleep(0.01)
    mid = [v for v in seq if 0.05 < v < 0.95]
    assert mid, "焦点环没有中间帧：%s" % seq[:6]
    wait(150)
    img = le.grab().toImage()
    edge = img.pixelColor(0, le.height() // 2)
    assert edge.blue() > 150 and edge.blue() > edge.red() + 60, \
        "焦点到位时环不是蓝色：#%02x%02x%02x" % (edge.red(), edge.green(), edge.blue())
    le.focusOutEvent(QtGui.QFocusEvent(QtCore.QEvent.Type.FocusOut))
    wait(250)
    assert le._foc < 0.01, "失焦后环没退掉：%.2f" % le._foc
    le.close()


def test_b5_selector_fades_in():
    """框选遮罩淡入：windowOpacity 从 0 过渡到 1，不是闪出来。"""
    pm = QtGui.QPixmap(200, 100)
    pm.fill(QtGui.QColor("#202020"))
    s = gui.Selector(pm, QtCore.QRect(0, 0, 200, 100), 1.0)
    s.show()
    s.setWindowOpacity(0.0)
    gui._anim(s, b"windowOpacity", 0.0, 1.0,
              gui._motion("standard", "fast")[0], curve=gui.EASE["standard"])
    seq = []
    end = time.monotonic() + 0.25
    while time.monotonic() < end:
        seq.append(round(s.windowOpacity(), 2)); app().processEvents(); time.sleep(0.01)
    mid = [v for v in seq if 0.05 < v < 0.95]
    assert mid, "遮罩没有淡入中间帧：%s" % seq[:6]
    assert s.windowOpacity() > 0.95, "遮罩没淡到 1：%.2f" % s.windowOpacity()
    s.close()


def test_b5_reduce_motion_kills_animation():
    """减少动效一开，所有动画一帧落终点（Apple HIG / WCAG 2.3.3），且 done 回调照常跑。"""
    w = QtWidgets.QWidget()
    w.resize(10, 10)
    gui.REDUCE["on"] = True
    try:
        landed = []
        gui._anim(w, b"pos", QtCore.QPoint(0, 0), QtCore.QPoint(300, 0), 400,
                  done=lambda: landed.append(1))
        wait(120)
        assert w.pos().x() == 300, "减少动效下没到终点：%d" % w.pos().x()
        assert landed, "减少动效下 done 回调没跑（收尾逻辑会断）"
    finally:
        gui.REDUCE["on"] = False
    w.close()


def test_b5_reduce_motion_setting_roundtrip():
    """设置窗的「减少动效」：读 config 初始化、保存写回、立即生效。"""
    cfg = {"hotkey": "ctrl+shift+D", "reduce_motion": True, "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    s = gui.Settings(cfg)
    assert s.reduce_motion.isChecked(), "config 里的 reduce_motion 没回显到复选框"
    s.reduce_motion.setChecked(True)
    s.apply()
    assert cfg["reduce_motion"] is True, "没写回 config"
    assert gui.REDUCE["on"] is True, "保存后没立即生效"
    gui.REDUCE["on"] = False             # 别污染后面的测试
    s.close()


def test_b5_later_survives_dead_widget():
    """定时器触发时控件的 C++ 对象已销毁：静默放弃，不抛 RuntimeError、不跑回调。

    用 shiboken6.delete 强制销毁——deleteLater 在测试 harness 里不一定真删
    （Python 引用还压着），但 RuntimeError 的风险场景就是"C++ 没了、定时器还活着"。
    """
    import shiboken6
    w = QtWidgets.QWidget()
    ran = []
    gui._later(100, w, lambda: ran.append(1))
    shiboken6.delete(w)                  # C++ 对象立刻销毁
    assert not shiboken6.isValid(w), "shiboken6.delete 没生效，这条测试的前提就不成立"
    wait(220)
    assert not ran, "C++ 对象销毁后回调还跑了"


def test_b5_enrich_dedup_and_fade_timing():
    """有道对照的两条纪律：
    ① 对照和词典释义一字不差时**不展示**（同义词出现两遍只会稀释信息，
       而且历史上 if 外还重复 show 了一次空标签）；
    ② 要展示时，淡入不能和卡片长高重叠（effect+几何变化=残影坑）——
       先透明占位，长高跑完才淡入，淡入完 effect 必须摘掉。"""
    # ① 一字不差 → 不展示
    c = card_of()
    wait(900)
    c.set_enrich({"explains": ["释义"]})       # 和 show_brief 的 cn 一模一样
    wait(100)
    assert not c.lb_alt.isVisibleTo(c), "对照和释义相同还展示了（信息重复）"
    c.close()
    # ② 不一样 → 透明占位 → 延迟淡入 → effect 摘干净
    c = card_of()
    wait(900)
    c.set_enrich({"explains": ["另一个角度的解释"]})
    wait(60)
    assert c.lb_alt.isVisibleTo(c), "该展示的对照没展示"
    eff_early = c.lb_alt.graphicsEffect()
    assert eff_early is not None and eff_early.opacity() < 0.05, \
        "长高期间对照行没有透明占位（fade 和 resize 重叠=残影）"
    ok = False                                # 定时器有抖动，轮询等摘 effect（上限 2 秒）
    for _ in range(20):
        wait(100)
        if c.lb_alt.graphicsEffect() is None:
            ok = True
            break
    assert ok, "淡入完了 effect 没摘（以后几何变化会残影）"
    c.close()


def test_b5_menu_and_dialog_animation():
    src = open(gui.__file__, encoding="utf-8").read()
    assert "def eventFilter" in src and "windowOpacity" in src, "托盘菜单没有淡入"
    assert "def done(self, r)" in src, "设置对话框没有淡出"
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    s = gui.Settings(cfg)
    s.show()
    wait(200)
    assert s.windowOpacity() >= 0.99, "设置窗淡入没跑完：%.2f" % s.windowOpacity()
    s.done(1)
    wait(60)
    assert s.isVisible(), "设置窗关闭动画期间就消失了"
    wait(300)
    assert not s.isVisible() and s.result() == 1, "设置窗没关干净"


class _FakeEv:
    """只带 globalPosition() 的假鼠标事件（`_drag_width`/`_drag_height` 只读这一个方法）。

    用它而不是 QTest：`_drag_height` 会移动窗口，而 QTest 把 local 映射成 global 时
    跟着那个移动漂（实测同一个向下拖动被读成 -131）—— 见 UI-STYLE.md 坑清单。
    """

    def __init__(self, x, y):
        self._p = QtCore.QPointF(float(x), float(y))

    def globalPosition(self):
        return self._p


def test_b5_card_drag_height_absorbs_in_scroll_area():
    """拖卡片上下边 = 改那块滚动区（详解/聊天）的高度，卡片高度跟着走，**不是**直接 resize 窗口。

    直接 `resize` 会被外层布局在激活时按 sizeHint 顶回去（实测想设 672、量出来 1100），
    所以正确路径是改滚动区高度再让 `_fit_height()` 重新量。这里钉三件事：
    (1) 鼠标位移和卡片高度接近 1:1；(2) 卡片不会被拖到比屏幕还高；
    (3) 松手后滚动区的下限被还原（不还原的话用户往小拖过一次，以后每次详解都被压在那个高度上）。
    """
    c = card_of()
    wait(900)
    c.set_detail("详解 " * 120)
    wait(700)
    h0, box = c.height(), c._scroll_box()
    assert box is c.detail and box.isVisible(), "带详解的卡应该拿详解当可伸缩区"
    b0 = box.height()
    x, y0 = c.x() + 200, c.y() + h0 - 4
    c._begin_edge("bottom", QtCore.QPoint(x, y0))
    assert c._drag_h is not None, "按下底边没进入拖高度状态"
    c._drag_height(_FakeEv(x, y0 + 120))
    assert abs(c.height() - (h0 + 120)) <= 24, \
        "往下拖 120 卡片只变了 %d（该是 1:1）" % (c.height() - h0)
    assert abs(box.height() - (b0 + 120)) <= 24, "详解没跟着变：%d → %d" % (b0, box.height())
    assert c.height() < c.screen().availableGeometry().height(), "卡片被拖到比屏幕还高"
    c._end_edge()
    assert box.minimumHeight() == c._box_floor(box), \
        "松手后详解的下限没还原（%d）—— 以后每次详解都会被压在这个高度上" % box.minimumHeight()
    c.close()


def test_b5_short_card_drag_height_gives_blank_not_a_dead_edge():
    """短卡（还没点详解/问 AI）拖上下边也该有反应：多出来的一截是空白，按钮行仍然贴着底边。

    没有可伸缩内容时静默不动会被当成"还是没实现"（用户第二次反馈就是这个口气）。
    """
    c = card_of()
    wait(900)
    h0 = c.height()
    assert c._scroll_box() is None, "这张卡不该有可伸缩区"
    x, y0 = c.x() + 200, c.y() + h0 - 4
    c._begin_edge("bottom", QtCore.QPoint(x, y0))
    c._drag_height(_FakeEv(x, y0 + 100))
    assert abs(c.height() - (h0 + 100)) <= 24, "短卡拖不动：%d → %d" % (h0, c.height())
    assert c.btn_close.mapTo(c, QtCore.QPoint(0, c.btn_close.height())).y() <= c.height() - 4, \
        "按钮行被挤出卡片了"
    c._drag_height(_FakeEv(x, y0 - 400))          # 往回拖过头：只能收回到原高度
    assert abs(c.height() - h0) <= 24, "空白收不回去：%d（该回到 %d）" % (c.height(), h0)
    c._end_edge()
    c.show_brief({"query": "y", "lexeme": "y", "kind": "word", "cn": ["释义"], "notes": []})
    wait(300)
    assert abs(c.height() - h0) <= 24, "换词后拖出来的空白没收掉：%d（该回到 %d）" % (c.height(), h0)
    c.close()


def test_b5_wordbook_door_slides_out_from_behind_the_panel():
    """记词板是"从常驻面板背后往左抽出来"，不是"从窗口外面滑进来"。

    用户原话："弹出主界面是从左弹到右而不是从右弹到左"。旧实现的关闭位在窗口左缘外
    （x=-WB_W），于是动画成了从左往右进入视野；现在的关闭位是 x=+WB_W（正被面板盖住），
    所以第一帧必须在右边、全程不超过 WB_W，且滑过面板那一段里抽屉要在面板**下面**。
    """
    cfg = {"hotkey": "ctrl+shift+D", "dock": {"enabled": True},
           "providers": {"deepseek": {"url": "", "model": "", "key": ""}, "ollama": {"model": ""}}}
    d = gui.Dock(cfg, lambda: None, lambda t: None, lambda: None, lambda: None, lambda: None)
    d.show()
    wait(300)
    if not d.expanded:                      # 抽屉要盖在面板上验 z 序，得先让面板在位
        d.toggle()
        wait(400)
        assert d.expanded and d.panel.pos().x() == d.WB_W, "面板没展开，验不了'面板盖住抽屉'"
    assert d.wbpanel.pos().x() == d.WB_W and not d.wbpanel.isVisible(), \
        "关着的抽屉该停在面板正后方（x=%d）：%d" % (d.WB_W, d.wbpanel.pos().x())
    d.toggle_wordbook()
    xs, behind = [], False
    end = time.monotonic() + 0.42
    while time.monotonic() < end:
        xs.append(d.wbpanel.pos().x())
        if d.childAt(QtCore.QPoint(320, 150)) is d.panel:
            behind = True
        app().processEvents()
        time.sleep(0.01)
    wait(300)
    assert xs and xs[0] >= d.WB_W - 40, \
        "第一帧就在左边了（x=%s）—— 那不是从面板背后出来，是从窗口外面滑进来" % (xs[0] if xs else None)
    assert all(x <= d.WB_W for x in xs), "中途跑到面板右边去了：%s" % xs[:6]
    assert d.wbpanel.pos().x() == 0, "开完没停在 0：%d" % d.wbpanel.pos().x()
    assert behind, "滑动过程中抽屉没被面板盖住（压在上面就是'盖着面板滑'）"
    assert d.mask().contains(QtCore.QPoint(d.WB_W // 2, 5)), "开着的时候可点区里没有记词板"
    d.toggle_wordbook()                                   # 关：该滑回面板背后
    wait(500)
    assert d.wbpanel.pos().x() == d.WB_W and not d.wbpanel.isVisible(), \
        "关完没回到面板背后：%d" % d.wbpanel.pos().x()
    assert not d.mask().contains(QtCore.QPoint(d.WB_W // 2, 5)), "关了还能点到记词板那块"
    d.close()


TESTS = [(n, f) for n, f in sorted(globals().items())
         if n.startswith("test_") and callable(f)]


def main():
    app()          # 先建 QApplication，否则第一个建控件的测试会被 Qt 硬 abort（没有 traceback）
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
