"""悬浮查词卡片（PySide6）。

三条硬要求怎么落实的：
  · 不抢焦点 —— WA_ShowWithoutActivating + Qt.Tool，show() 不调用 activateWindow()；
    只有用户自己点进输入框，键盘才归卡片。
  · 快 —— 热键按下先把整屏抓下来（快路径先出词典结果，联网/LLM 都在后台线程）。
  · 省 token —— 结果按词进 SQLite 缓存，DeepSeek 只在你要"详细解释"或"转 DeepSeek"时才发。
"""
import html
import io
import math
import os
import re
import sys
import threading
import time
import traceback
from string import Template

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__, winput

_LOG = None


def _esc(s):
    """往 QTextBrowser 的富文本里塞用户/模型给的字符串之前先转义（< 会被当标签）。"""
    return html.escape(str(s)).replace("\n", "<br>")


def _para(text, lh=1.65):
    """多行段落要走富文本：QLabel 的**纯文本**落不了 line-height（Qt 的坑）。

    排版上这条是硬需求：释义默认行距只有 ~1.2，两行以上就挤成一坨，读着累。
    """
    esc = html.escape(str(text)).replace("\n", "<br>")
    return '<p style="line-height:%s; margin:0">%s</p>' % (lh, esc)


class RichLabel(QtWidgets.QLabel):
    """按 QTextDocument 实排高度报尺寸的折行富文本标签。

    为什么不用裸 QLabel：QLabel 自带的 `heightForWidth` 对富文本**系统性低估**——
    322 字、line-height 1.55 的正文，QLabel 报 273px，QTextDocument 实排要 470px
    （它压根不算 line-height）。容器信了它，正文就被裁掉一截；旧代码是靠 show 之后
    一次延后的布局请求"撞"回正确高度，先量后定的新路径一上就现形。
    这里覆写 heightForWidth/sizeHint，用 QTextDocument 按真实宽度排版，量得准。
    """

    def __init__(self, text="", objectName=None):
        super().__init__(text)
        if objectName:
            self.setObjectName(objectName)
        self.setWordWrap(True)
        self._doc = QtGui.QTextDocument()
        self._doc.setDefaultFont(self.font())
        self._doc.setDocumentMargin(0.0)
        self._doc.setHtml(text or "")

    # setText 三个口子都拦住（setText / setTextFormat 不需要，只走 setText）
    def setText(self, t):                                   # noqa: N802（Qt 命名）
        super().setText(t or "")
        self._doc.setDefaultFont(self.font())
        self._doc.setHtml(t or "")
        self.updateGeometry()                               # 让布局重新来问尺寸

    def _ideal_h(self, w):
        # 字体必须**度量时**同步，不能 setText 时同步一次了事：show 之前 QSS 还没
        # polish，font() 是构造字体（实测差出 1.6 倍行高，322 字正文 290 vs 470）。
        f = self.font()
        if self._doc.defaultFont() != f:
            self._doc.setDefaultFont(f)
        # QLabel 画富文本时左右各让 ~2px；量的时候留出同样的宽度，行数才一致
        self._doc.setTextWidth(max(20.0, float(w) - 4))
        return self._doc.size().height()

    def heightForWidth(self, w):
        return int(self._ideal_h(w)) + 2                    # +2：富文本上下余量

    def sizeHint(self):
        w = self.width() if self.width() > 40 else 370      # 卡片内容区宽度的常规值
        base = super().sizeHint()
        return QtCore.QSize(base.width(), self.heightForWidth(w))

    def minimumSizeHint(self):
        return QtCore.QSize(1, 1)                           # 高度全交给 heightForWidth


def _shadow(w, blur=24, dy=6, a=120):
    """给浮动窗口一层很淡的投影。

    docs/UI-STYLE.md 里记了这条是**故意偏离**参考设计系统的：Raycast 是网页、背后永远是
    自己的画布，纯靠 surface 阶梯就够；SnapWord 的卡片浮在任意桌面内容上，没有投影会和
    背后的浅色网页糊在一起。只此一层，不加发光、不加第二层。
    """
    e = QtWidgets.QGraphicsDropShadowEffect(w)
    e.setBlurRadius(blur)
    e.setOffset(0, dy)
    e.setColor(QtGui.QColor(0, 0, 0, a))
    w.setGraphicsEffect(e)
    return e


def _stop_anims(obj, prop=None):
    """掐掉挂在 obj 上（某属性 / 全部属性）的动画。

    被 stop() 掉的动画**不会**发 finished，所以要自己从 `_ANIMS` 里摘——否则列表会一直
    攒着，而且它的 done 回调（比如收起动画收尾的 `_settle`）永远不会跑，面板就卡在半路。
    """
    for a in list(_ANIMS):
        try:
            if a.targetObject() is not obj:
                continue
            if prop is not None and bytes(a.propertyName()) != bytes(prop):
                continue
        except RuntimeError:          # 对象已经被删了
            _ANIMS.remove(a)
            continue
        a.stop()
        if a in _ANIMS:
            _ANIMS.remove(a)


def _anim(obj, prop, start, end, ms=180, curve=None, done=None):
    """给 obj 的 prop 做一次属性动画。

    **必须留引用**：PySide6 里动画对象一旦被 GC，属性就停在原地不动（看起来像"没动画"）。

    同一个对象的同一个属性**同时只允许一个动画**：连点细边、快速划过光边、淡入没跑完就
    点关闭，都会叠出第二个动画，两个动画每帧各写一次属性，看到的就是抖动/停在半路。

    REDUCE（减少动效）在这里**一处生效**：全部动画都走这个函数，所以只在这里把时长
    压到 1ms——下一帧直接落终点，finished/done 照常发，所有收尾（摘 effect、面板归位）
    行为不变。Apple HIG / WCAG 2.3.3 的要求就是"开着这个开关，位移类动效不再发生"。
    """
    _stop_anims(obj, prop)
    if REDUCE["on"]:
        ms = 1
    a = QtCore.QPropertyAnimation(obj, prop, obj)
    a.setDuration(int(ms))
    a.setStartValue(start)
    a.setEndValue(end)
    a.setEasingCurve(curve or QtCore.QEasingCurve.Type.OutCubic)
    _ANIMS.append(a)

    def _finish():
        if a in _ANIMS:
            _ANIMS.remove(a)
        if done:
            done()

    a.finished.connect(_finish)
    a.start()
    return a


_ANIMS = []


def _later(ms, w, fn):
    """ms 毫秒后对 w 跑 fn；w 已经死了就静默放弃。

    卡片/设置窗关掉之后定时器还活着的场景到处都是（复制文案还原、成功脉冲回落），
    直接对 C++ 已删的控件调方法会抛 RuntimeError 刷爆日志。
    这个版本的 PySide6 的 QtCore 里没有 QPointer，用 shiboken6.isValid 判活。
    """
    import shiboken6

    def run():
        try:
            if shiboken6.isValid(w):
                fn()
        except RuntimeError:
            pass

    QtCore.QTimer.singleShot(ms, run)


def _fade_in(w, ms=150):
    """控件淡入（透明度 0→1）。给"异步冒出来的小块"用：有道对照行、多词 chips、设置窗。

    用完必须把 effect 摘掉（`setGraphicsEffect(None)`）—— 挂着 `QGraphicsOpacityEffect`
    的控件以后走软件渲染，白拖性能。用完就摘也就不会和 `_shadow()` 抢同一个 effect。
    """
    if not isinstance(w, QtWidgets.QWidget):
        # 布局不是控件，没有 setGraphicsEffect。这里挡一道，别让一次手滑把整张卡片
        # 卡在 show() 之前（hy4 评审第 1 条踩的就是这个：传了 QHBoxLayout）。
        return
    eff = QtWidgets.QGraphicsOpacityEffect(w)
    w.setGraphicsEffect(eff)
    a = QtCore.QPropertyAnimation(eff, b"opacity", w)
    a.setDuration(int(ms))
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
    _ANIMS.append(a)

    def _finish():
        if a in _ANIMS:
            _ANIMS.remove(a)
        try:
            w.setGraphicsEffect(None)
        except RuntimeError:        # 控件已经被删了，忽略
            pass

    a.finished.connect(_finish)
    a.start()
    return a


def _graceful_reveal(w, delay=None, fade=None):
    """（已废弃：揭示应挂长高动画 done 后触发，见 Card.queue_reveal / _after_grow）"""

# ---- 视觉规范见 docs/UI-STYLE.md：令牌只在这里定义一次，QSS 里别随手写颜色 ----
T = {
    "canvas": "#07080a", "surface": "#0d0d0d", "elevated": "#101111", "card": "#121212",
    "hairline": "#242728", "hairline_strong": "#33373d",
    "ink": "#f4f4f6", "body": "#cdcdcd", "mute": "#9c9c9d", "ash": "#6a6b6c",
    "blue": "#57c1ff", "blue_soft": "rgba(87,193,255,0.15)",
    # 交互态色阶（blue / ash 的派生档）：写进令牌表是为了 QSS 里不出现任何表外色值
    "blue_hover": "#7bd0ff", "blue_pressed": "#3aa9ea", "handle_hover": "#4a5058",
    "green": "#59d499", "yellow": "#ffc533", "red": "#ff6161",
}
# ---------------- 动效令牌 v2（数值全部来自外部规范，不是拍的） ----------------
# 时长：跨设计系统（M3 short3 / IBM Carbon moderate-01 / Shopify Polaris / Tailwind）
# 收敛在 150ms；进入 200-300、容器形变 300-500；退出 = 进入的 60~70%。
DUR = {"instant": 90, "fast": 150, "base": 240, "slow": 380}

_EASE_PTS = {
    # 进入/减速：M3 emphasized decelerate
    "out": ((0.05, 0.7), (0.1, 1.0)),
    # 退出/加速：M3 emphasized accelerate
    "in": ((0.3, 0.0), (0.8, 0.15)),
    # 位移/缩放这类"要有物理感"的：Apple 的 overshoot 曲线
    "spring": ((0.34, 1.56), (0.64, 1.0)),
    # 小状态变化（hover、颜色）：M3 standard decelerate 的单调写法
    "standard": ((0.0, 0.0), (0.2, 1.0)),
}


def _curve(kind):
    """按外部规范拼一条三次贝塞尔缓动（Qt 的 BezierSpline 支持，见 QEasingCurve 文档）。

    控制点的 x 必须单调递增，否则 Qt 求值会走形——所以 out/standard 用的是规范里
    单调的那几条，而不是原样抄非单调的 cubic-bezier(0.2, 0, 0, 1)。
    """
    c1, c2 = _EASE_PTS[kind]
    c = QtCore.QEasingCurve(QtCore.QEasingCurve.Type.BezierSpline)
    c.addCubicBezierSegment(QtCore.QPointF(*c1), QtCore.QPointF(*c2),
                            QtCore.QPointF(1.0, 1.0))
    return c


EASE = {k: _curve(k) for k in _EASE_PTS}

# 减少动效：规范（Apple HIG / WCAG 2.3.3 的实践做法）要求位移类动画必须能关掉。
# Qt 读不到系统的"减少动效"设置，所以做成配置开关，在设置窗里给用户。
REDUCE = {"on": False}


def _motion(kind, dur):
    """按"是否减少动效"返回时长/曲线：关掉时动画直接完成，位移不再发生。"""
    if REDUCE["on"]:
        return 1, EASE["standard"]
    return DUR[dur], EASE[kind]


def _lerp(a, b, t):
    """两个令牌色之间插值（t 是 0..1 的过渡量）。QSS 没有 transition，过渡只能自己画。"""
    ca, cb = QtGui.QColor(a if a else "#00000000"), QtGui.QColor(b if b else "#00000000")
    return QtGui.QColor(
        int(ca.red() + (cb.red() - ca.red()) * t),
        int(ca.green() + (cb.green() - ca.green()) * t),
        int(ca.blue() + (cb.blue() - ca.blue()) * t),
        int(ca.alpha() + (cb.alpha() - ca.alpha()) * t))

# 卡片备注里给用户看的引擎名 —— 让人一眼知道"这次是谁认的"，出问题也好反馈
OCR_ENGINE_CN = {"native": "本地 PP-OCR", "system": "系统 OCR", "auto": "自动"}

# 按钮调色板（kind → 各态的令牌色）。文字/边框/背景都按过渡量插值，见 SmoothButton。
BTN_PAL = {
    # 主按钮：蓝色渐变，字用 canvas（对比度 10:1）
    "primary": {"grad": ("blue_hover", "blue"), "hover": "blue_hover", "press": "blue_pressed",
                "text": "canvas", "border": "blue", "radius": 8, "font": 12.5,
                "pad": 12, "high": 32},
    # 次级按钮
    "normal": {"base": "elevated", "hover": "card", "press": "canvas",
               "text": "body", "text_hover": "ink", "border": "hairline",
               "border_hover": "hairline_strong", "radius": 8, "font": 12.5,
               "pad": 12, "high": 32},
    # 幽灵按钮（关闭、次要操作）：没有底、没有框，只有悬停才浮出来；
    # 按下压向 hairline——透明按钮也得有"按下去"的那一下
    "ghost": {"hover": "card", "press": "hairline", "text": "mute", "text_hover": "ink",
              "radius": 8, "font": 12.5, "pad": 12, "high": 32},
    # 细胶囊（版本徽章，高 ~17）与整圆胶囊（词胶囊，高 24）——圆角按控件半高取，
    # 否则 Qt 会静默画成直角（见 UI-STYLE.md 的坑）
    "chip": {"base": "elevated", "hover": "card", "press": "canvas",
             "text": "mute", "text_hover": "ink", "border": "hairline",
             "border_hover": "hairline_strong", "radius": 8, "font": 11,
             "pad": 8, "high": 17},
    "pill": {"base": "elevated", "hover": "card", "press": "canvas",
             "text": "mute", "text_hover": "ink", "border": "hairline",
             "border_hover": "hairline_strong", "radius": 12, "font": 11,
             "pad": 8, "high": 24},
}


class SmoothButton(QtWidgets.QPushButton):
    """会过渡的按钮。

    为什么要自己画：**QSS 没有 transition**，所以 QSS 按钮的 hover/press/focus 都是
    一帧切过去——这是界面"简陋"的源头之一。这里改成程序驱动：三个 0..1 的过渡量
    （hover / press / focus）走 QPropertyAnimation，每帧按它们插值画背景、边框、文字。

    数值按外部规范（不是拍的）：
      · hover **进入 90ms、离开 150ms**（不对称：进入要即时，离开要柔和——emilkowalski 40 rules）
      · 按下 50ms 下沉、松开 90ms 回弹；位移 1px（规范是 scale 0.97~0.98，Qt 里缩放按钮
        会改变尺寸，用内容下移 1px 做等效反馈）
      · 禁用态不参与过渡（规范：disabled means disabled）
      · 减少动效开启时，过渡直接完成，只剩颜色变化
    """

    def __init__(self, text="", kind="normal", parent=None):
        super().__init__(text, parent)
        self.kind = kind if kind in BTN_PAL else "normal"
        self._hov = 0.0
        self._prs = 0.0
        self._foc = 0.0
        self._suc = 0.0                    # 成功脉冲（复制→绿）
        self._act = 0.0                    # 激活态（钉住→蓝），iOS toggle 那种"开着"的样子
        self.setMinimumHeight(BTN_PAL[self.kind]["high"])
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)                 # 交给 paintEvent 画，别让样式再画一层底

    # ---- 三个可动画的过渡量 ----
    def get_hov(self):
        return self._hov

    def set_hov(self, v):
        self._hov = float(v)
        self.update()

    def get_prs(self):
        return self._prs

    def set_prs(self, v):
        self._prs = float(v)
        self.update()

    def get_foc(self):
        return self._foc

    def set_foc(self, v):
        self._foc = float(v)
        self.update()

    def get_suc(self):
        return self._suc

    def set_suc(self, v):
        self._suc = float(v)
        self.update()

    def get_act(self):
        return self._act

    def set_act(self, v):
        self._act = float(v)
        self.update()

    hov = QtCore.Property(float, get_hov, set_hov)
    prs = QtCore.Property(float, get_prs, set_prs)
    foc = QtCore.Property(float, get_foc, set_foc)
    suc = QtCore.Property(float, get_suc, set_suc)
    act = QtCore.Property(float, get_act, set_act)

    # ---- 语义态 ----
    def flash_success(self):
        """复制成功那种绿色脉冲：快速点亮，停一会儿，慢慢回落。"""
        if not self.isEnabled():
            return
        ms1, c1 = _motion("standard", "instant")
        _anim(self, b"suc", self._suc, 1.0, ms1, curve=c1)
        def release():
            if not self.isEnabled():
                return
            ms2, c2 = _motion("standard", "slow")
            _anim(self, b"suc", self._suc, 0.0, ms2, curve=c2)
        _later(900, self, release)              # 900ms 内卡片关了的话，按钮已死，静默放弃

    def set_active(self, on):
        """钉住/开关类按钮的激活态：蓝底淡入淡出，不是一帧切换。"""
        ms, curve = _motion("standard", "fast")
        _anim(self, b"act", self._act, 1.0 if on else 0.0, ms, curve=curve)

    # ---- 触发 ----
    def _go(self, prop, target, dur):
        if not self.isEnabled():
            return
        cur = getattr(self, "_" + prop)
        if abs(cur - target) < 0.001:
            return
        ms, curve = _motion("standard", dur)
        _anim(self, prop.encode(), cur, target, ms, curve=curve)

    def enterEvent(self, e):
        super().enterEvent(e)
        self._go("hov", 1.0, "instant")      # 悬停进入：即时

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._go("hov", 0.0, "fast")         # 悬停离开：150ms，慢一点才不会"啪"

    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.MouseButton.LeftButton:
            self._go("prs", 1.0, "instant")
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._go("prs", 0.0, "instant")
        super().mouseReleaseEvent(e)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self._go("foc", 1.0, "fast")

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._go("foc", 0.0, "fast")

    # ---- 画 ----
    def sizeHint(self):
        pal = BTN_PAL[self.kind]
        f = QtGui.QFont(self.font())
        f.setPointSizeF(pal["font"])
        fm = QtGui.QFontMetrics(f)
        return QtCore.QSize(fm.horizontalAdvance(self.text()) + 2 * pal["pad"] + 2,
                            pal["high"])

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pal = BTN_PAL[self.kind]
        w, h = self.width(), self.height()
        on = self.isEnabled()
        hov = self._hov if on else 0.0
        prs = self._prs if on else 0.0
        foc = self._foc

        # 底：默认 → 悬停 → 按下
        base = pal.get("base")
        if pal.get("grad"):
            g = QtGui.QLinearGradient(0, 0, 0, h)
            top = T[pal["grad"][0]]
            bot = T[pal["grad"][1]]
            g.setColorAt(0.0, _lerp(top, T[pal["hover"]], hov))
            g.setColorAt(1.0, _lerp(bot, T[pal["press"]], prs * 0.6))
            p.setBrush(QtGui.QBrush(g))
        else:
            p.setBrush(_lerp(base, T[pal["hover"]], hov) if base else
                       _lerp("#00000000", T[pal["hover"]], hov))
        if pal.get("press") and not pal.get("grad"):
            p.setBrush(_lerp(_lerp(base, T[pal["hover"]], hov), T[pal["press"]], prs * 0.7))

        # 激活态（钉住）：底掺 15% blue（= blue_soft 叠在底色上的本意），框和字往 blue 靠。
        # 不能直接 lerp 向 blue_soft 令牌——它是 rgba(…,0.15)，alpha 也会被插值，
        # 按钮会褪成半透明洞（实测中心变 #000000）。掺 blue 的 RGB、保住 alpha 才是"叠色"。
        act = self._act if on else 0.0
        if act > 0.0 and not pal.get("grad"):      # 渐变按钮的 brush.color() 无效，只有纯色按钮才叠底
            p.setBrush(_lerp(p.brush().color(), T["blue"], act * 0.15))

        # 边框：默认 → 悬停加强；聚焦时转 blue（非文本对比度 9.7:1，远超 3:1）
        bc = "#00000000"
        if pal.get("border"):
            bc = _lerp(T[pal["border"]], T[pal.get("border_hover") or pal["border"]], hov)
        if act > 0.0:
            bc = _lerp(bc, T["blue"], act)
        if foc > 0.0:
            bc = _lerp(bc, T["blue"], foc)
        suc = self._suc if on else 0.0
        if suc > 0.0:
            bc = _lerp(bc, T["green"], suc)
        p.setPen(QtGui.QPen(QtGui.QColor(bc), 1))     # QPen(str, width) 在 PySide6 不接受
        p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, w - 1, h - 1),
                          pal["radius"], pal["radius"])

        # 字：按下时整体下移 1px（等效 scale 0.97 的按压反馈）
        if on:
            col = _lerp(T[pal["text"]], T[pal.get("text_hover") or pal["text"]], hov)
            if act > 0.0:
                col = _lerp(col, T["blue"], act)
            if suc > 0.0:
                col = _lerp(col, T["green"], suc)
        else:
            col = QtGui.QColor(T["ash"])
        p.setPen(col)
        f = self.font()
        f.setPointSizeF(pal["font"])
        p.setFont(f)
        p.drawText(QtCore.QRectF(0, prs * 1.0, w, h),
                   QtCore.Qt.AlignmentFlag.AlignCenter, self.text())


class SmoothLineEdit(QtWidgets.QLineEdit):
    """焦点环会过渡的输入框。

    QSS 的 `QLineEdit:focus { border-color }` 是一帧切换；这里让 QSS 只负责静态样式
    （底、hairline 边框），焦点环改成自绘叠层：focusIn/Out 驱动一个 0..1 的过渡量，
    蓝色描边淡入淡出（fast=150ms，M3 short3 / Carbon moderate-01 的那一档）。
    文字、光标、选区全部交给父类画，自己只在最上面叠一个圆角环——风险最小。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._foc = 0.0

    def get_foc(self):
        return self._foc

    def set_foc(self, v):
        self._foc = float(v)
        self.update()

    foc = QtCore.Property(float, get_foc, set_foc)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        ms, c = _motion("standard", "fast")
        _anim(self, b"foc", self._foc, 1.0, ms, curve=c)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        ms, c = _motion("standard", "fast")
        _anim(self, b"foc", self._foc, 0.0, ms, curve=c)

    def paintEvent(self, e):
        super().paintEvent(e)               # 文字/光标/选区/底/静态边框都按原样画
        if self._foc <= 0.0:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        c = QtGui.QColor(T["blue"])
        c.setAlphaF(self._foc)              # 过渡量直接映射成环的不透明度
        p.setPen(QtGui.QPen(c, 1))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 8, 8)


class SmoothComboBox(QtWidgets.QComboBox):
    """焦点环会过渡的下拉框——和 SmoothLineEdit 同一套叠层画法（见那边的注释）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._foc = 0.0

    def get_foc(self):
        return self._foc

    def set_foc(self, v):
        self._foc = float(v)
        self.update()

    foc = QtCore.Property(float, get_foc, set_foc)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        ms, c = _motion("standard", "fast")
        _anim(self, b"foc", self._foc, 1.0, ms, curve=c)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        ms, c = _motion("standard", "fast")
        _anim(self, b"foc", self._foc, 0.0, ms, curve=c)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._foc <= 0.0:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        c = QtGui.QColor(T["blue"])
        c.setAlphaF(self._foc)
        p.setPen(QtGui.QPen(c, 1))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 8, 8)


QSS = Template("""
/* 卡片：底色走 card→surface 的竖向渐变（两端都是令牌），顶沿换成 hairline-strong ——
   那道"顶沿受光"的边就是全应用的识别符号（和细边的蓝条、面板的分隔线同一套语言）。 */
#card { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 $card, stop:1 $surface);
        border: 1px solid $hairline; border-top-color: $hairline_strong;
        border-radius: 8px; }
#streak { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 $blue, stop:1 rgba(0,0,0,0));
          border: none; }
/* 常驻面板：和右边那条细边拼成**一整块**，所以右侧两个角不要圆 */
#dockpanel { background: $surface; border: 1px solid $hairline; border-right: none;
             border-top-left-radius: 8px; border-bottom-left-radius: 8px;
             border-top-right-radius: 0; border-bottom-right-radius: 0; }
/* 记词板：跟主面板同一块料，但它是独立的一扇门（从主面板左边滑出来），左边缘的圆角由它出 */
#wbpanel { background: $surface; border: 1px solid $hairline; border-right: none;
           border-top-left-radius: 8px; border-bottom-left-radius: 8px;
           border-top-right-radius: 0; border-bottom-right-radius: 0; }
/* 主面板左边缘的抓条：平时全透明，鼠标移上去才亮一条蓝（"这里能拖"） */
#dockgrip { background: transparent; }
#dockgrip:hover { background: $blue_soft; }
/* 卡片左右两条抓边：透明、只靠光标提示（这条压在最外 8px 的投影留白上，
   亮一块蓝会从圆角外面支出去，所以不给 hover 底色） */
#cardgrip { background: transparent; }
QLabel { color: $body; }
#word { color: $ink; font-size: 24px; font-weight: 600; }
#phon { color: $mute; font-size: 13px; }
#src  { color: $blue; font-size: 11px; background: $blue_soft;
        border: 1px solid $hairline; border-radius: 4px; padding: 1px 6px; }
#note { color: $mute; font-size: 11px; }
/* 表单分组标题：比正文小、比备注重，靠字重和留白分主次，不靠边框 */
#sechead { color: $mute; font-size: 11px; font-weight: 600; padding-top: 10px; letter-spacing: 0.5px; }
/* 引导块（onboarding）：比卡片低一档的分隔面，用底色 + 描边圈出"这是一段说明，不是设置项" */
QScrollArea { background: $canvas; border: none; }
QScrollArea > QWidget > QWidget { background: $canvas; }
#tip { background: $elevated; border: 1px solid $hairline; border-radius: 8px; padding: 8px 12px; }
#tiphead { color: $ink; font-size: 12px; font-weight: 600; }
#tipline { color: $body; font-size: 12px; }
#dsok { color: $green; font-size: 12px; }
#dsbad { color: $red; font-size: 12px; }
#cn   { color: $ink; font-size: 15px; }
/* 空态：查不到释义时那一行不能跟正常释义一个量级，降成安静的一块 */
#empty { color: $mute; font-size: 13px; padding: 8px 0; }
#alt  { color: $mute; font-size: 12px; }
#en   { color: $mute; font-size: 12px; }
/* 原句（短语卡的"题目"）：mute 而不是 ash——它是正文，对比度必须过 4.5:1；
   弱于胶囊靠位置和字号，不靠牺牲可读性 */
#sd   { color: $mute; font-size: 12px; }
#hr   { background: $hairline; border: none; max-height: 1px; }
/* 按钮（含 #go/#ghost/#chip/#pill）全部是 SmoothButton 自绘（见 BTN_PAL）——
   QSS 没有 transition，按钮放这里就只能一帧硬切，所以 QSS 里**不再有** QPushButton 规则。
   各态色值、焦点环（blue，非文本对比 9.7:1）、禁用态（ash）都在 BTN_PAL 里由断言守住。 */
QTextBrowser { background: $elevated; border: 1px solid $hairline; border-radius: 8px;
               color: $body; font-size: 13px; padding: 8px 12px; }
/* 输入框全部是 SmoothLineEdit：QSS 只管静态样式，焦点蓝环是自绘叠层、淡入淡出（见类注释）。
   这里故意不写输入框的聚焦态规则——一帧跳蓝的边框会和淡入的环打架（测试会扫这个模板）。 */
QLineEdit { background: $elevated; border: 1px solid $hairline; border-radius: 8px;
            color: $ink; padding: 7px 10px; font-size: 13px;
            selection-background-color: $blue; selection-color: $canvas; }
QComboBox { background: $elevated; border: 1px solid $hairline; border-radius: 8px;
            padding: 7px 10px; color: $ink; font-size: 12px; }
QComboBox:hover { border-color: $hairline_strong; }
/* 和输入框一样：下拉框也是 Smooth 系（SmoothComboBox），焦点蓝环自绘淡入，
   这里故意不写聚焦态规则——测试会扫这个模板，一帧切换的边框和淡入的环会打架。 */
QComboBox::drop-down { border: none; width: 16px; }
QComboBox QAbstractItemView { background: $elevated; color: $ink; border: 1px solid $hairline;
                              selection-background-color: $card; outline: none; }
QCheckBox { color: $body; font-size: 12px; spacing: 8px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid $hairline_strong;
                       border-radius: 4px; background: $elevated; }
QCheckBox::indicator:checked { background: $blue; border-color: $blue; }
QCheckBox::indicator:focus { border-color: $blue; }
QDialog { background: $canvas; }
QMenu { background: $elevated; color: $body; border: 1px solid $hairline; padding: 4px; }
QMenu::item { padding: 6px 16px; border-radius: 6px; }
QMenu::item:selected { background: $card; color: $ink; }
QMenu::separator { height: 1px; background: $hairline; margin: 4px 6px; }
QToolTip { background: $elevated; color: $ink; border: 1px solid $hairline; padding: 4px 8px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: $hairline_strong; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: $handle_hover; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
#dockrail { background: $surface; border: 1px solid $hairline; border-right: none;
            border-top-right-radius: 0; border-bottom-right-radius: 0;
            border-top-left-radius: 8px; border-bottom-left-radius: 8px; }
/* Rail 现在是自己画的（见 class Rail），这里只留一层兜底底色，别再给它挂子控件样式。
   竖排整词的字距是画出来的，QLabel 那套 "一个字母一行" 已经删了。 */
#dockrail:hover { background: $card; border-color: $hairline_strong; }
/* 释义左侧的层级竖线（从上到下渐隐的蓝，和细边那道光边同一套语言） */
#cnbar { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                     stop:0 $blue, stop:1 rgba(0,0,0,0)); border: none; }
#panelword { color: $ink; font-size: 17px; font-weight: 700; letter-spacing: 0.2px; }
""").substitute(T)


def log_stream(data_dir):
    """日志文件。pythonw 下 sys.stdout/sys.stderr 都是 None，而
    `traceback.print_exc()` 会往 None 上 write —— 一个后台任务的异常就能把整个进程搞崩，
    界面上还什么都不显示（这就是"莫名其妙崩溃"的头号来源）。所以一律落到文件里。"""
    global _LOG
    if _LOG is None:
        try:
            os.makedirs(data_dir, exist_ok=True)
            _LOG = open(os.path.join(data_dir, "snapword.log"), "a", encoding="utf-8",
                        buffering=1)
        except OSError:
            _LOG = io.StringIO()
    return _LOG


def install_crash_log(data_dir):
    f = log_stream(data_dir)
    if sys.stdout is None:
        sys.stdout = f      # print() 在 pythonw 下本来就是丢掉的，接到日志里还能查
    if sys.stderr is None:
        sys.stderr = f

    def hook(etype, value, tb):
        try:
            f.write("\n=== 未捕获异常 %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            traceback.print_exception(etype, value, tb, file=f)
            f.flush()
        except Exception:
            pass
        if sys.stderr not in (None, f):
            traceback.print_exception(etype, value, tb)

    sys.excepthook = hook
    # QThread 里抛的异常走 threading.excepthook，不走 sys.excepthook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    return f

def pixmap_png(pm):
    ba = QtCore.QByteArray()
    buf = QtCore.QBuffer(ba)
    buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    pm.toImage().save(buf, "PNG")
    buf.close()
    return bytes(ba)


class Job(QtCore.QThread):
    """在后台线程跑一个函数，结果用信号回到 UI 线程。

    stream=True 表示 fn 返回的是生成器：每吐一段就 progress.emit，done 拿拼好的全文。
    """

    done = QtCore.Signal(object)
    fail = QtCore.Signal(str)
    progress = QtCore.Signal(object)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.stream = bool(kwargs.pop("stream", False))
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.setObjectName("job:" + getattr(fn, "__name__", "lambda"))

    def run(self):
        try:
            r = self.fn(*self.args, **self.kwargs)
            if self.stream:
                buf = []
                for piece in r:
                    buf.append(str(piece))
                    self.progress.emit(piece)
                r = "".join(buf)
            self.done.emit(r)
        except Exception as ex:
            traceback.print_exc()
            self.fail.emit("%s: %s" % (type(ex).__name__, ex))


class Selector(QtWidgets.QWidget):
    """全屏遮罩上拖一个框。整屏图是热键按下时就抓好的，所以框里不会拍到自己。"""

    picked = QtCore.Signal(QtCore.QRect, QtGui.QPixmap)
    cancelled = QtCore.Signal()

    def __init__(self, pixmap, geometry, dpr):
        super().__init__(None)
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setGeometry(geometry)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.pm = pixmap
        self.dpr = dpr
        self.origin = None
        self.cur = None

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(self.rect(), self.pm)
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 110))
        if self.origin and self.cur:
            r = QtCore.QRect(self.origin, self.cur).normalized()
            if r.width() > 1 and r.height() > 1:
                src = QtCore.QRect(
                    int(r.x() * self.dpr), int(r.y() * self.dpr),
                    int(r.width() * self.dpr), int(r.height() * self.dpr))
                p.drawPixmap(r, self.pm, src)
                pen = QtGui.QPen(QtGui.QColor(T["blue"]), 2)
                p.setPen(pen)
                p.drawRect(r)

    def mousePressEvent(self, e):
        self.origin = e.position().toPoint()
        self.cur = self.origin
        self.update()

    def mouseMoveEvent(self, e):
        self.cur = e.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, e):
        if not (self.origin and self.cur):
            self.cancelled.emit()
            return
        r = QtCore.QRect(self.origin, self.cur).normalized()
        if r.width() < 6 or r.height() < 6:
            self.cancelled.emit()
            return
        src = QtCore.QRect(
            int(r.x() * self.dpr), int(r.y() * self.dpr),
            int(r.width() * self.dpr), int(r.height() * self.dpr))
        self.picked.emit(r, self.pm.copy(src))

    def keyPressEvent(self, e):
        if e.key() == QtCore.Qt.Key.Key_Escape:
            self.cancelled.emit()

    def mouseDoubleClickEvent(self, e):
        self.cancelled.emit()


class FlowLayout(QtWidgets.QLayout):
    """按行排、一行放不下就换行的流式布局（Qt 官方示例的精简版）。

    为什么必须有它：词胶囊原来躺在 QHBoxLayout 里，遇到长短语会一气切出 12 个词，
    一行塞不下就被**压扁** —— 实测最窄被压到 28px，胶囊上的文字直接被裁掉
    （`tmp/stress_audit.py` 有这条断言）。"数量不定的短元素"在平台上的常规做法是换行，
    不是把元素挤变形。
    """

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self._spacing = spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)      # 不参与拉伸，只按内容排

    def hasHeightForWidth(self):
        return True                          # 有了它，adjustSize() 才能算出换行后的高度

    def heightForWidth(self, w):
        return self._flow(QtCore.QRect(0, 0, w, 0), True)

    def minimumSize(self):
        if not self._items:
            return QtCore.QSize(0, 0)
        s = self._items[0].sizeHint()
        for it in self._items[1:]:
            s.setWidth(max(s.width(), it.sizeHint().width()))
        return s

    def sizeHint(self):
        # 宽度必须取真实可用宽度：兜底成 400 会让外层按 400 去问 heightForWidth，
        # 算出 2 行、实际排 3 行，底下那行就被裁掉了（实测过）。
        pw = self.parentWidget()
        w = self.geometry().width() or (pw.width() if pw is not None else 0) or 400
        return QtCore.QSize(w, self.heightForWidth(w))

    def setGeometry(self, r):
        super().setGeometry(r)
        need = self._flow(r, False)
        # 分配到的高度不够（或给多了）→ 让外层按"换行后的真实高度"重算一次。
        # 用 singleShot 绕开"布局过程中改布局"，并用计数器防止来回抖。
        pw = self.parentWidget()
        if pw is not None and abs(need - r.height()) > 1 and getattr(self, "_fixes", 0) < 4:
            self._fixes = getattr(self, "_fixes", 0) + 1
            pw.setMinimumHeight(need)      # 换行后的真实高度作为下限报给外层
            pw.updateGeometry()

    def _flow(self, rect, dry):
        x, y, rowh = rect.x(), rect.y(), 0
        for it in self._items:
            # 取控件的**实时** sizeHint：布局项会缓存首次量到的尺寸，那份缓存是样式生效前
            # 量出来的（偏小），按它排会算出"一行放得下"，实际放不下 → 下面几行被裁。
            w = it.widget()
            sz = w.sizeHint() if w is not None else it.sizeHint()
            if x + sz.width() > rect.right() and x > rect.x():     # 放不下 → 换行
                x = rect.x()
                y += rowh + self._spacing
                rowh = 0
            if not dry:
                it.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), sz))
            x += sz.width() + self._spacing
            rowh = max(rowh, sz.height())
        return y + rowh - rect.y()


class FlowBox(QtWidgets.QWidget):
    """装 FlowLayout 的容器。

    QWidget 自己不会把布局的 `heightForWidth` 报给外层布局，所以只换 FlowLayout 还不够：
    实测 12 个胶囊铺到了 y=332，容器只给了 114px，底下大半被裁掉。这里把
    `hasHeightForWidth / heightForWidth` 转给内部布局，外层才能算出"换行后的真实高度"。
    """

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        lay = self.layout()
        return lay.heightForWidth(w) if lay is not None else super().heightForWidth(w)


class Card(QtWidgets.QWidget):
    """一张卡片。pinned 之后就固定住，再查会新开一张。"""

    closed = QtCore.Signal(object)
    ask_requested = QtCore.Signal(object, str, bool)   # brief, 问题, 是否用 DeepSeek
    detail_requested = QtCore.Signal(object)
    word_clicked = QtCore.Signal(str)
    save_requested = QtCore.Signal(object)             # 存词 / 取消存词（由 App 落库）
    width_changed = QtCore.Signal(int)                 # 用户拖右边改了宽度（由 App 记进配置）

    GRIP_W = 12          # 左右抓边宽度（见 __init__：要盖住 x=8 那条可见描边）
    GRIP_H = 12          # 上下抓边高度（盖住 y=8 / y=height-12 那两条可见描边）

    def __init__(self, font_pt=10, width=0):
        super().__init__(None)
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(QSS)
        # 宽度：配置里存了就用它（用户拖过右边），否则自适应——430 是主尺寸，窄屏上要收
        # （430 在 1024 宽的屏上要吃掉 42% 的横向空间）；下限 360，再窄释义就没法读了。
        scr = QtGui.QGuiApplication.primaryScreen()
        avail_w = scr.availableGeometry().width() if scr is not None else 1920
        self._w = int(width) or max(360, min(430, int(avail_w * 0.45)))
        self.setFixedWidth(self._w)
        self.brief = None
        self.pinned = False
        self._drag = None
        self._drag_w = None             # 拖左右边改宽度时的起点（见 _begin_edge）
        self._drag_h = None             # 拖上下边改高度时的起点（见 _drag_height）
        self.saved = False              # 这个词在不在生词本里（决定「存词」按钮长什么样）
        self._jobs = []
        # 「生成中… N 秒」用：模型冷启动十几秒，只有一句「生成中…」用户分不清是在想还是已经死了
        self._detail_t0 = 0.0
        self._detail_tick = QtCore.QTimer(self)
        self._detail_tick.setInterval(1000)
        self._detail_tick.timeout.connect(self._detail_elapsed)
        self._build()
        self.setFont(QtGui.QFont("Microsoft YaHei UI", font_pt))
        self._typography()      # 必须在 setFont 之后：setFont 会把字距一起冲掉
        # 左右两条抓边 —— 为什么是子控件、而不是在 mousePressEvent 里按 x 判边：
        # 卡片最外 8px 是投影预留的**透明**留白，用户看见的描边在 x=8 / x=width-8。
        # 按坐标判边时（原来 x<=6 / x>=width-6）只在最外面那 6px 生效，用户瞄着描边按下去，
        # 事件落在 frame 子控件上、根本进不了 Card.mousePressEvent，于是变成了"移动卡片"
        # —— 用户的原话就是"拖大小没实现"（实测：430 宽的卡，判边区 [424,430]，描边在 422）。
        # 做成压在描边上的子控件后：Qt 悬停自己换 SizeHorCursor（不用开 mouseTracking，
        # 这也正是原来没任何提示的原因），按下后的隐式抓取又保证 move/release 都送给同一条边。
        self.grip_l = QtWidgets.QFrame(self, objectName="cardgrip")
        self.grip_r = QtWidgets.QFrame(self, objectName="cardgrip")
        # 上下两条同理，只是改的是高度：见 _drag_height（差量交给详解/聊天区吸收）
        self.grip_t = QtWidgets.QFrame(self, objectName="cardgrip")
        self.grip_b = QtWidgets.QFrame(self, objectName="cardgrip")
        for g in (self.grip_l, self.grip_r):
            g.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
            g.setToolTip("拖动改卡片宽度")
            g.installEventFilter(self)
        for g in (self.grip_t, self.grip_b):
            g.setCursor(QtCore.Qt.CursorShape.SizeVerCursor)
            g.setToolTip("拖动改卡片高度")
            g.installEventFilter(self)
        self._place_grips()

    def _place_grips(self):
        """把四条抓边摆到可见描边上（卡片尺寸一变就得重摆，所以 resizeEvent 第一件事就是它）。

        上下那两条左右各让开一个 GRIP_W：角上留给左右抓边（不重叠，省得纠结谁压谁）。
        """
        if not getattr(self, "grip_l", None):
            return
        w, h = self.width(), self.height()
        self.grip_l.setGeometry(0, 0, self.GRIP_W, h)
        self.grip_r.setGeometry(max(0, w - self.GRIP_W), 0, self.GRIP_W, h)
        inner = max(0, w - 2 * self.GRIP_W)
        self.grip_t.setGeometry(self.GRIP_W, 0, inner, self.GRIP_H)
        self.grip_b.setGeometry(self.GRIP_W, max(0, h - self.GRIP_H), inner, self.GRIP_H)
        for g in (self.grip_t, self.grip_b, self.grip_l, self.grip_r):
            g.raise_()

    def _word_cap(self):
        """词头那一行给词留的最大宽度：卡宽 - 170（430 宽时正好是原来的 260）。"""
        return max(200, int(self._w) - 170)

    def _typography(self):
        """排版的最后一公里：字距随字号反向走——大号收紧、小号放开。

        QSS 管不到 letter-spacing（Qt 不支持），只能在这里用 QFont 设；字号仍由 QSS 决定，
        QSS 的 font-size 优先级高于 setFont，所以两者不打架。
        """
        f = self.lb_word.font()
        f.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, -0.4)   # 24px 词头收紧
        self.lb_word.setFont(f)
        f = self.lb_phon.font()
        f.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 0.3)    # 13px 音标放开
        self.lb_phon.setFont(f)
        f = self.lb_src.font()
        f.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 0.3)
        self.lb_src.setFont(f)

    # ---------- 界面 ----------
    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 12)      # 给 frame 外围留出投影的地方
        # 顶层窗口的布局默认会把窗口**立刻**撑到 sizeHint：于是 `detail.show()` 那一瞬间
        # 卡片就跳到新高度，后面的长高动画量到 old == new，直接 return —— 症状是点
        # 「详细解释 / 问 AI」时卡片"啪"一下变大，没有动画。设成 NoConstraint 之后，
        # 高度只由 `_resize_keep_place()` 里的 adjustSize + 动画决定。
        outer.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        self.frame = QtWidgets.QFrame(objectName="card")
        outer.addWidget(self.frame)
        _shadow(self.frame)                        # 卡片浮在别人家网页上，一层淡投影分开
        v = QtWidgets.QVBoxLayout(self.frame)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)          # 6 不在布局间距刻度上（2/4/8/12/16/24）
        self.lb_word = QtWidgets.QLabel("…", objectName="word")
        # 超长词（45 个字母那种）里没有空格，换行救不了它，只会把布局搅乱
        # （实测"整句/词组"被折成两行、徽章被顶飞）。按平台常规处理：显示宽度设上限，
        # 超出用省略号截断，完整词放到 tooltip 里（见 show_brief）。
        self.lb_word.setMaximumWidth(self._word_cap())
        self.lb_phon = QtWidgets.QLabel("", objectName="phon")
        self.lb_phon.setWordWrap(True)
        self.lb_src = QtWidgets.QLabel("", objectName="src")
        # 「存词」放词头这一行（☆/★），不跟底下的动作按钮挤：六个按钮实测 hint 合计
        # 411px + 间距，430 宽的可用只有 376 —— 塞进去必然换行，而且是难看的 5+1。
        # ☆ 是"收藏/存下来"的通用位置（词头右上角），点一下变 ★，再点取消。
        self.btn_save = SmoothButton("☆", "ghost")
        self.btn_save.setFixedWidth(30)
        self.btn_save.setToolTip("存到记词板（再点一次取消）")
        self.btn_save.clicked.connect(self._toggle_save)
        head.addWidget(self.lb_word)
        head.addWidget(self.lb_phon)
        head.addStretch(1)
        head.addWidget(self.btn_save)
        head.addWidget(self.lb_src)
        v.addLayout(head)

        self.lb_text = QtWidgets.QLabel("", objectName="sd")
        self.lb_text.setWordWrap(True)
        self.lb_text.hide()
        v.addWidget(self.lb_text)

        # 词按钮那一行。特意包一层 QWidget：`_fade_in()` 只认控件，直接对 QHBoxLayout
        # 调 setGraphicsEffect 会 AttributeError，而那一炸发生在 `show()` 之前 ——
        # 查短语/整句时整张卡片都不出来（hy4 评审第 1 条，已复现）。
        self.chips_box = FlowBox()            # 换行容器（见 FlowBox / FlowLayout 的说明）
        self.chips = FlowLayout()
        self.chips_box.setLayout(self.chips)
        self.chips_box.setVisible(False)      # 没词就别占那一行
        v.addWidget(self.chips_box)

        # 释义左侧那道 2px 竖线：给"答案"一个明确的起点，也是光边语言的竖着那一版。
        # 没有它时，释义和下面的英文/对照在视觉上是同一坨灰字。
        cn_row = QtWidgets.QHBoxLayout()
        cn_row.setSpacing(12)
        self.cn_bar = QtWidgets.QFrame(objectName="cnbar")
        self.cn_bar.setFixedWidth(2)
        cn_row.addWidget(self.cn_bar)
        self.lb_cn = RichLabel("", objectName="cn")
        self.lb_cn.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        cn_row.addWidget(self.lb_cn, 1)
        v.addLayout(cn_row)

        # 分组：释义上面是"答案"，下面是"补充信息"（英文释义 / 第三方对照 / 备注）。
        # 原来全部平铺成一列，主次只能靠字号差一点点看出，用户得自己找重点。
        # 分隔线上下各让出 4px：组间距要大于组内间距，层次才立得住。
        v.addSpacing(4)
        self.hr_extra = QtWidgets.QFrame(objectName="hr")
        self.hr_extra.setFixedHeight(1)
        v.addWidget(self.hr_extra)
        v.addSpacing(4)

        # 有道/百度的对照行单独一个 label：混在 lb_cn 里会跟词典释义一样白一样大，看不出主次
        self.lb_alt = RichLabel("", objectName="alt")
        self.lb_alt.hide()
        v.addWidget(self.lb_alt)

        self.lb_en = RichLabel("", objectName="en")
        v.addWidget(self.lb_en)

        self.lb_note = QtWidgets.QLabel("", objectName="note")
        self.lb_note.setWordWrap(True)
        v.addWidget(self.lb_note)

        self.detail = QtWidgets.QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setMinimumHeight(96)      # 高度跟内容走（见 set_detail），下限只是兜底
        self.detail.hide()
        v.addWidget(self.detail)

        self.chat_log = QtWidgets.QTextBrowser()
        self.chat_log.setMinimumHeight(110)
        self.chat_log.hide()
        v.addWidget(self.chat_log)

        # 极端内容（超长详解、很长的对话）不能把卡片顶到屏幕外面去：
        # 给这两块设上限，超了由它们自己滚。
        # 预算按「最矮那块屏 - 卡片固定部分 - 两块之间的余量」来分：原来只管到 45%，
        # 详解 + 聊天同时开就是 90%，再加标题/音标/释义/按钮那三百多像素固定部分 ——
        # 1152 高的屏上按钮行直接被顶出屏幕（实测卡片 1035 逻辑像素、顶端 y≈134）。
        screens = QtGui.QGuiApplication.screens()
        scr_h = min(s.availableGeometry().height() for s in screens) if screens else 1080
        cap = max(150, int((scr_h - 420) / 2))
        self.detail.setMaximumHeight(cap)
        self.chat_log.setMaximumHeight(cap)
        self._scroll_cap = cap       # 自然上限（用户手动拖过高度后，_end_edge 会用它还原）

        self.chat_row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(self.chat_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.chat_in = SmoothLineEdit()
        self.chat_in.setPlaceholderText("就这个词问点什么…（回车发送）")
        self.btn_esc = SmoothButton("发送", "primary")
        self.btn_esc.setToolTip("回车也是发送。默认本地 9B 答（免费）；答不满意就按住 Shift 点这里，"
                                "强制转 DeepSeek。")
        h.addWidget(self.chat_in, 1)
        h.addWidget(self.btn_esc)
        self.chat_row.hide()
        v.addWidget(self.chat_row)
        # 手动把卡片拖高时（_drag_height），多出来的那一截如果没被详解/聊天区吃掉，
        # 就落在这个弹簧里 —— 按钮行因此永远贴着底边，不会浮在卡片中间。
        # 正常"高度跟内容走"的时候这一项是 0，没有任何影响。
        # 注意 addStretch() 返回 None（Qt 里是 void），弹簧要自己造才拿得到句柄。
        self._v_stretch = QtWidgets.QSpacerItem(
            0, 0, QtWidgets.QSizePolicy.Policy.Minimum,
            QtWidgets.QSizePolicy.Policy.Expanding)
        v.addSpacerItem(self._v_stretch)

        # 按钮行用 FlowLayout（换行），不用 QHBoxLayout：卡片宽度可以拖（见 mousePressEvent），
        # 拖到 360 那档时这一行必须能换行而不是变形。文案也顺手收短：「详细解释」→「详解」。
        # （「存词」不在这行 —— 它在词头，见上面 btn_save 的注释。）
        btns = FlowLayout(spacing=4)
        hr = QtWidgets.QFrame(objectName="hr")
        hr.setFixedHeight(1)
        v.addWidget(hr)
        # 全部换成会过渡的按钮（QSS 没有 transition，普通按钮的状态切换是一帧硬切）
        self.btn_detail = SmoothButton("详解", "primary")
        self.btn_detail.setToolTip("生成详细解释（词典 + 模型）")
        # "问 AI" 点下去是**展开**一个提问区，不是立刻跳转 —— 加展开指示，别让人以为按错了
        self.btn_chat = SmoothButton("问 AI ▾", "normal")
        self.btn_copy = SmoothButton("复制", "normal")
        self.btn_pin = SmoothButton("钉住", "normal")
        self.btn_close = SmoothButton("✕", "ghost")   # 关闭是低权重操作，别和复制/钉住一个视觉量级
        self.btn_close.setToolTip("关闭（也可以按 Esc）")
        btns.addWidget(self.btn_detail)
        btns.addWidget(self.btn_chat)
        btns.addWidget(self.btn_copy)
        btns.addWidget(self.btn_pin)
        btns.addWidget(self.btn_close)
        v.addLayout(btns)

        self.btn_detail.clicked.connect(lambda: self.detail_requested.emit(self.brief))
        self.btn_chat.clicked.connect(self._toggle_chat)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_pin.clicked.connect(self._toggle_pin)
        self.btn_close.clicked.connect(self.hide_card)
        self.btn_esc.clicked.connect(lambda: self._send(False))
        self.chat_in.returnPressed.connect(lambda: self._send(False))

        # 动效状态槽位。**必须在构造时就建好**，不能只靠 hide_card/_resized 去建：
        # 那两处是"重开一张卡时复位"的路径，首次弹出时根本没跑过，`_reveal_queue`
        # 会不存在 → queue_reveal 直接 AttributeError（实测挂了 3 条断言）。
        self._resizing = False           # 长高动画进行中
        self._resize_again = False       # 长高期间内容又变了，结束后再量一次
        self._anim_h = None              # 长高动画当前高度（布局偶尔把窗口顶到终点，用来拉回）
        self._reveal_queue = []          # 等长高 done 才淡入的控件
        self._reveal_retries = 0         # 见 _after_grow：重排次数上限，防止内容永远透明
        self._staggered = []
        self._stagger_eff = []

    # ---------- 数据 ----------
    def show_brief(self, brief, text=None, at=None):
        self.brief = brief
        self._clear_chips()
        self.detail.hide()
        self.chat_log.hide()
        self.chat_row.hide()
        self.chat_log.clear()
        # 换个词就把上一次手动拖出来的空白收掉：高度回到"内容说了算"。
        self._v_stretch.changeSize(0, 0, QtWidgets.QSizePolicy.Policy.Minimum,
                                   QtWidgets.QSizePolicy.Policy.Expanding)
        # 换词了：先当作"没存过"，App 拿到生词本的真状态后会再校正一次（见 App._show_brief）。
        # 不这么做的话复用同一张卡片查第二个词，「已存」会挂在没存过的词上。
        self.set_saved(False)

        asked = brief.get("query") or ""
        lex = brief.get("lexeme") or asked
        head_word = lex if brief.get("kind") == "word" else "整句/词组"
        fm = self.lb_word.fontMetrics()
        cap = self.lb_word.maximumWidth()
        self.lb_word.setText(
            fm.elidedText(head_word, QtCore.Qt.TextElideMode.ElideRight, cap)
            if fm.horizontalAdvance(head_word) > cap else head_word)
        self.lb_word.setToolTip(head_word)      # 截断了的词在这里看全
        self.lb_phon.setText(("/" + brief["phonetic"] + "/") if brief.get("phonetic") else "")
        tags = " ".join(brief.get("tags") or [])
        src_txt = " · ".join(x for x in [brief.get("source"), tags] if x)
        self.lb_src.setText(src_txt)
        self.lb_src.setVisible(bool(src_txt))   # 空徽章连框都不该画（QSS 给 #src 配了边框和底）

        raw = text or asked
        multi = brief.get("kind") == "phrase" or (raw.strip() and raw.strip().lower() != asked.strip().lower())
        if multi:
            self.lb_text.setText(raw.strip()[:300])
            self.lb_text.show()
            self._fill_chips(raw)
        else:
            self.lb_text.hide()

        cn = brief.get("cn") or []
        # 多行段落走富文本，行高才落得下去（见 _para）
        self.lb_cn.setText(_para("\n".join(cn) or "（没有释义）"))
        # 空态和正常释义不是一个量级：换 objectName 套 #empty。改完必须 unpolish/polish
        # 一遍，否则 QSS 不会重新套到已经显示过的控件上。
        self.lb_cn.setObjectName("empty" if not cn else "cn")
        self.lb_cn.style().unpolish(self.lb_cn)
        self.lb_cn.style().polish(self.lb_cn)
        self.cn_bar.setVisible(bool(cn))       # 没释义就别立那道竖线（会像在强调"没有"）
        self.lb_alt.setText("")
        self.lb_alt.hide()
        self.lb_en.setText(_para(brief.get("en") or "", 1.55))
        self.lb_en.setVisible(bool(brief.get("en")))
        notes = list(brief.get("notes") or [])
        if brief.get("cached"):
            notes.append("来自本地缓存（没再花 token）")
        if brief.get("exchange"):
            notes.append("变形：" + brief["exchange"])
        if notes:
            # 语义色：省下的 token 用绿点标出来，联网来的用蓝点——颜色在这里是信息，不是装饰
            dot = T["green"] if brief.get("cached") else T["blue"]
            self.lb_note.setText('<span style="color:%s">●</span> %s'
                                 % (dot, _esc(" · ".join(notes))))
        else:
            self.lb_note.setText("")
        self.lb_note.setVisible(bool(notes))

        # 高度上限可能是上一轮长高动画被打断时留下的（见 `_stop_grow`），不放开的话
        # 这张卡片会被钳在旧高度上，底部按钮落到窗口外点不到。
        self.setMaximumHeight(16777215)
        # 高度必须**量**出来（走 `_fit_height`），不能用 `self.adjustSize()`：
        # adjustSize 设的是 sizeHint，而内容真实需要的高度要等一次延后的布局请求才落地
        # （实测：长释义 sizeHint 448 / 真实 613）。拿 448 去定位、随后窗口被撑到 613，
        # 卡片就掉出屏幕底部 148px —— 真机 1152 高的屏上必现，offscreen 量不出来。
        self.resize(self.width(), self._fit_height())
        self._place(at)
        self._closing = False
        # 入场过渡走"位置滑动 + 子控件依次淡入（stagger）"，**不走 windowOpacity 动画**：
        # WA_TranslucentBackground + FramelessWindowHint 的顶层窗口在 Windows 上，
        # windowOpacity 动画是出了名的不可靠 —— Qt 官方 bug 库里这类窗口的 opacity 动画
        # 要么直接跳变（QTBUG-33025）、要么结尾闪黑（QTBUG-29010）、要么重绘错乱
        # （QTBUG-28531）。用户报的"弹出没有过渡、闪动、虚影"就是这条路。位置动画和
        # 控件级 QGraphicsOpacityEffect 都不踩它。
        self.setWindowOpacity(1.0)       # 防御：上次可能淡出一半就被打断
        self._stagger_prepare()          # 先钉成全透明，再 show（顺序说明见那个方法）
        self.show()
        self.raise_()
        p = self.pos()
        # 从细边（常驻面板）那一边滑出来：卡片是"从面板里抽出来的"，方向本身就是叙事。
        # 24px 的滑动在 240ms 里足够被看见（旧值 14px 小到几乎注意不到——加上
        # windowOpacity 动画在真机不生效，用户看到的就是"没有任何过渡直接出现"）。
        # stagger 必须等位移动画跑完再开始：QGraphicsOpacityEffect 在窗口移动期间重绘
        # 不跟随，会把子控件画在旧位置上（真机实测：释义叠到词头上）。
        _anim(self, b"pos", QtCore.QPoint(p.x() + 24, p.y() + 10), p, DUR["base"],
              done=lambda *_: self._stagger_in())

    def _stagger_prepare(self):
        """把要依次进场的文字先钉成全透明。**必须在 `show()` 之前调**。

        不然用户看到的是：卡片带着全部文字滑进来（这一瞬间已经能读了），滑完这些文字
        一起"啪"地掉到透明、再一段段淡回来 —— 也就是"弹出的头（出现）和尾（淡入）反了"。
        实测（tmp/edge_probe.py，430 的卡）：
          t≈0-200ms 滑入，word/cn/en 都是 vis（内容全在）
          t≈240ms 滑完，三个一起变 0.00（整张卡一瞬空白）
          t≈280-500ms 才按 词头→释义→英文 依次淡回来
        """
        # 复用同一张卡片时清掉上一轮的效果。**必须走 `_clear_stagger()`，不能裸
        # `setGraphicsEffect(None)`**：opacity 动画是挂在 effect 上的（父对象 = effect），
        # 直接删 effect 会把动画对象一起删掉，但 `_ANIMS` 里还留着它的引用 —— 悬垂指针。
        # 下一次遍历 `_ANIMS` 就是 RuntimeError（实测：上一个词查完 700ms 内再查下一个词
        # 必崩）。`_clear_stagger()` 会先 stop 并从 `_ANIMS` 摘除，再摘 effect。
        self._clear_stagger()
        # 顺序必须跟**布局里的上下顺序**一致（alt 在 en 上面），否则是"下面那行先出现、
        # 上面那行后出现"的倒序感。
        targets = [self.lb_word, self.lb_cn, self.lb_alt, self.lb_en, self.lb_note]
        targets = [w for w in targets if w.isVisibleTo(self) and w.text()]
        self._staggered = list(targets)
        self._stagger_eff = []
        for w in targets:
            eff = QtWidgets.QGraphicsOpacityEffect(w)
            w.setGraphicsEffect(eff)
            eff.setOpacity(0.0)
            self._stagger_eff.append(eff)

    def _stagger_in(self):
        """位移动画跑完之后才开始依次淡入（间隔 45ms）。见 `_stagger_prepare` 的顺序说明。

        只动 opacity（QGraphicsOpacityEffect），**不动位置**：布局管着的控件一动位置就会被
        下一次 layout 冲掉，文档里也明令别动布局属性。
        """
        for i, w in enumerate(self._staggered):
            eff = w.graphicsEffect()
            if eff is None:              # 已经被 _clear_stagger() 掐掉了
                continue

            def play(w=w, eff=eff):
                # 动画一完就把 effect 摘掉：QGraphicsOpacityEffect 在窗口**几何变化**
                # （拖动、卡片长高）期间重绘不跟随，会把文字画到旧位置 —— 真机上的症状是
                # 「点一下就不能正常显示了」。effect 只在入场那 200ms 有意义，之后必须清。
                def clear():
                    try:
                        w.setGraphicsEffect(None)
                    except RuntimeError:      # 卡片已经关掉了
                        pass

                try:
                    _anim(eff, b"opacity", 0.0, 1.0, 200, done=clear)
                except RuntimeError:
                    # 定时器触发时卡片已经关了（测试里尤其常见）——控件和 effect 的
                    # C++ 对象都随窗口删了，这里直接放弃，别往事件循环里抛异常
                    pass

            QtCore.QTimer.singleShot(i * 45, play)

    def _clear_stagger(self):
        """提前结束 stagger：掐掉 opacity 动画并摘掉 effect。

        窗口要变形/关闭时必须调——effect 挂着的时候改几何，文字会被画到旧位置。
        """
        for eff in getattr(self, "_stagger_eff", []):
            _stop_anims(eff, b"opacity")
        for w in getattr(self, "_staggered", []):
            try:
                w.setGraphicsEffect(None)
            except RuntimeError:      # 卡片已经关掉了
                pass
        self._stagger_eff = []
        self._staggered = []

    def set_enrich(self, y):
        """有道回来了就补一行对照（不覆盖词典释义）。"""
        if not y:
            return
        extra = y.get("explains") or []
        if y.get("translation") and not extra:
            extra = [y["translation"]]
        if extra:
            # 来源已经由备注行的语义色圆点标明（● 有道），这里不再重复前缀；
            # 而且如果有道给的对照和词典释义**一字不差**，就不展示——同一句话出现两遍
            # 只会稀释信息（serendipity 这种词两边都给"意外的惊喜"）。
            txt = "；".join(extra[:4])
            cn_now = "\n".join((self.brief or {}).get("cn") or []).strip()
            if txt.strip() != cn_now:
                self.lb_alt.setText(txt)
                self.lb_alt.show()
                # 对照行会让窗口长高；淡入挂进待办队列，等长高 done 才触发（见 queue_reveal），
                # 绝不在几何变化期间跑 opacity 动画（残影坑）。
                self.queue_reveal(self.lb_alt, DUR["base"])
        if not self.lb_phon.text() and y.get("phonetic"):
            self.lb_phon.setText("/" + y["phonetic"] + "/")
            self.queue_reveal(self.lb_phon, DUR["fast"])     # 音标补上同走队列，等 grow 收尾再淡入
        self._resize_keep_place()

    def start_detail_wait(self):
        """「详解」按下去那一刻叫它：正文区开始数秒，让人看得见它在动。

        秒数写在**正文区**而不是按钮上：按钮行是 FlowLayout，文案一变长（「生成中… 13 秒」）
        就会重排、卡片跟着一跳；而且按钮宽度变了看起来像整行在抖。正文区本来就在说
        「正在整理…」，在那里更新秒数最自然。
        """
        self._detail_t0 = time.monotonic()
        self._detail_tick.start()
        self._detail_elapsed()

    def _detail_elapsed(self):
        # 不判 btn_detail.isEnabled()：按下去那一刻就把它禁用了，那样第一个 tick 就自杀。
        # 收尾由 set_detail / _on_job_fail 负责停表；卡片没了这个 QTimer 跟着一起销毁。
        n = int(time.monotonic() - self._detail_t0)
        tip = "正在整理…（本地模型冷启动要十几秒，DeepSeek 快一些）"
        if n >= 3:
            tip = ("正在整理…已经 %d 秒。\n\n词典释义在上面；模型那部分本地 9B 冷启动"
                   "要十几秒，DeepSeek 快一些。" % n)
        self.detail.show()
        self.detail.setPlainText(tip)

    def set_detail(self, md, err=None):
        # 收尾的人负责把按钮还原。以前这里不还原、只有下一次查词才 setEnabled(True)，
        # 于是文案永远停在「生成中…」，下一个词点不动详解（hy4 评审第 4 条）。
        self._detail_tick.stop()
        self.btn_detail.setEnabled(True)
        self.btn_detail.setText("详解")
        self.detail.show()
        if md:
            self.detail.setMarkdown(md)
        else:
            self.detail.setPlainText("没能生成详解：" + (err or "模型没返回内容"))
        # 高度跟内容走：短详解不留一大块空白（minimum 260 那会儿底下空出两百多像素），
        # 长详解夹到上限（超了自己滚）。排版宽度必须是**文字区**的宽 —— 卡片宽度现在
        # 能拖（360~720），所以直接量视口，不能沿用按 430 算出来的 376。
        # 视口宽在第一帧之前是 0，那时按卡片宽度估一个（frame 边距 28 + 边框 2 + padding 24 = 54）。
        vw = self.detail.viewport().width()
        if vw < 200:
            vw = max(200, self.width() - 54)
        self.detail.document().setTextWidth(vw)
        need = int(self.detail.document().size().height()) + 24
        self.detail.setFixedHeight(max(96, min(need, self.detail.maximumHeight())))
        # 揭示：窗口长高期间把详解钉在透明，长高结束再淡入（见 queue_reveal / _after_grow）
        self.queue_reveal(self.detail)
        self._resize_keep_place()

    def append_chat(self, who, text):
        was_hidden = not self.chat_row.isVisible()
        self.chat_log.show()
        self.chat_row.show()
        color = {"你": T["blue"], "AI": T["ink"], "系统": T["mute"]}.get(who, T["ink"])
        self.chat_log.append('<b style="color:%s">%s</b>：%s' % (color, who, _esc(text)))
        # 高度跟内容走（同 set_detail 的算法）：消息多了才长，长到上限自己滚
        vw = self.chat_log.viewport().width()
        if vw < 200:
            vw = max(200, self.width() - 54)
        self.chat_log.document().setTextWidth(vw)
        need = int(self.chat_log.document().size().height()) + 24
        self.chat_log.setFixedHeight(max(110, min(need, self.chat_log.maximumHeight())))
        # 第一次展开聊天区时，让它淡入而不是随长高啪出来
        if was_hidden:
            self.queue_reveal(self.chat_row)
        self._resize_keep_place()

    def chat_begin(self, who):
        """开一个空气泡，之后用 chat_push 一段段往里加（流式回答用）。"""
        self.chat_log.show()
        self.chat_row.show()
        color = {"你": T["blue"], "AI": T["ink"], "系统": T["mute"]}.get(who, T["ink"])
        if self.chat_log.toPlainText().strip():
            self.chat_log.append("")
        self.chat_log.append('<b style="color:%s">%s</b>：' % (color, _esc(who)))
        self.chat_log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def chat_push(self, text):
        self.chat_log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        self.chat_log.insertPlainText(text)
        self.chat_log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        # 每条 token 都重排窗口会抖，隔 0.1 秒长一次就够了
        now = time.time()
        if now - getattr(self, "_last_resize", 0.0) > 0.1:
            self._last_resize = now
            self._resize_keep_place()

    def _fill_chips(self, text):
        toks = [t for t in re.findall(r"[A-Za-z][A-Za-z'\-]{1,}", text)][:12]
        self.chips_box.setVisible(len(toks) >= 2)     # 没有词就整行收掉，不占高度
        if len(toks) < 2:
            return
        for t in toks:
            b = SmoothButton(t, "pill")     # 整圆胶囊（高 24、圆角 12），点一下换查这个词
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setToolTip("查这个单词")
            b.clicked.connect(lambda _=False, w=t: self.word_clicked.emit(w))
            self.chips.addWidget(b)
        _fade_in(self.chips_box, DUR["base"])

    def _clear_chips(self):
        self.chips_box.setMinimumHeight(0)     # 上一张卡片留下的高度下限要清掉
        self.chips._fixes = 0
        while self.chips.count():
            it = self.chips.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    # ---------- 交互 ----------
    def _toggle_chat(self):
        on = not self.chat_row.isVisible()
        self.chat_row.setVisible(on)
        self.btn_chat.setText("问 AI ▴" if on else "问 AI ▾")   # 展开指示跟着翻
        if on:
            self.chat_log.show()
            # 空聊天从紧凑高度起步（110px），随消息一条条长高（见 append_chat）——
            # 一打开就是一大块空盒子很简陋
            self.chat_log.setFixedHeight(110)
            # 揭示：聊天区长高期间透明，长高结束再淡入（见 queue_reveal / _after_grow）
            self.queue_reveal(self.chat_row)
            self._resize_keep_place()
            # 用户主动要问，才把键盘交过去（这就是"不抢焦点"的让步）
            self.activateWindow()
            self.chat_in.setFocus()
        else:
            # 收起：**消息区也得跟着藏，高度还得还回去**。原来只藏了输入行，消息区原地不动、
            # 卡片高度保持展开时的高度，看起来就像"收起没生效、只是被上面的详解挡住了"。
            self.chat_log.hide()
            self._resize_keep_place()

    def _toggle_save(self):
        """存词 / 取消存词。落库由 App 做（它拿着 Wordbook），这里只管按钮长相。"""
        self.save_requested.emit(self.brief)

    def set_saved(self, on):
        self.saved = bool(on)
        # ☆ → ★：一个字符换一个字符，宽度不动（按钮本来就钉了 30px），卡片不会跳。
        # 状态靠 set_active 的蓝底 + 星形一起说。
        self.btn_save.setText("★" if self.saved else "☆")
        self.btn_save.setToolTip("已在记词板里，再点一次取消" if self.saved else "存到记词板")
        self.btn_save.set_active(self.saved)

    def _send(self, _deep):
        q = self.chat_in.text().strip()
        if not q or not self.brief:
            return
        self.chat_in.clear()
        self.append_chat("你", q)
        escalate = QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier
        self.ask_requested.emit(self.brief, q, bool(escalate))

    def _copy(self):
        if not self.brief:
            return
        b = self.brief
        lines = [b.get("lexeme") or b.get("query") or ""]
        if b.get("phonetic"):
            lines.append("/%s/" % b["phonetic"])
        lines += b.get("cn") or []
        if b.get("en"):
            lines.append("EN: " + b["en"])
        if b.get("exchange"):
            lines.append("变形: " + b["exchange"])
        if self.detail.isVisible():
            lines.append("")
            lines.append(self.detail.toPlainText())
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.btn_copy.setText("已复制")
        self.btn_copy.flash_success()               # 绿色脉冲：边框和字一起亮一下再回落
        _later(1200, self.btn_copy, lambda: self.btn_copy.setText("复制"))

    def _toggle_pin(self):
        self.pinned = not self.pinned
        self.btn_pin.setText("已钉住" if self.pinned else "钉住")
        self.btn_pin.set_active(self.pinned)        # 激活态淡入/淡出，不是一帧换皮

    def hide_card(self):
        if getattr(self, "_closing", False):
            return                      # Esc 和 ✕ 一起按、或连按两次 Escape
        self._closing = True
        # 入场滑入 / 长高还没跑完就点关闭：先把它们掐掉，别一边滑一边淡出。
        # 掐掉长高动画要把 `_resizing` 闸门一并复位——被 stop 的动画不会发 finished，
        # 闸门不复位的话这张卡片以后再也不会长高了（高度永久卡住）。
        _stop_anims(self, b"pos")
        self._stop_grow()
        self._reveal_queue = []            # 关掉卡片，排队的揭示淡入作废
        self._clear_stagger()
        if not self.isVisible():
            self._really_hide()
            return
        # 关闭也绕开 windowOpacity（同 show_brief 的注释：这类半透明无边框窗口的
        # opacity 动画在 Windows 上会跳变/闪黑）。向下 16px 滑出去再藏，
        # 150ms 足够看出"收走了"，又不会拖泥带水。
        p = self.pos()
        _anim(self, b"pos", p, QtCore.QPoint(p.x(), p.y() + 16), DUR["fast"],
              curve=QtCore.QEasingCurve.Type.InCubic, done=self._really_hide)

    def _really_hide(self):
        self.hide()
        self.setWindowOpacity(1.0)      # 下次弹出来还得是正常不透明度
        self.closed.emit(self)

    def keyPressEvent(self, e):
        if e.key() == QtCore.Qt.Key.Key_Escape:
            self.hide_card()
        else:
            super().keyPressEvent(e)

    def _place(self, at):
        scr = QtWidgets.QApplication.screenAt(at or QtGui.QCursor.pos()) or QtWidgets.QApplication.primaryScreen()
        g = scr.availableGeometry()
        w, h = self.width(), self.height()
        x = min(max(g.left() + 8, (at.x() if at else QtGui.QCursor.pos().x()) - w // 3), g.right() - w - 8)
        y0 = (at.y() if at else QtGui.QCursor.pos().y()) + 18
        if y0 + h > g.bottom():
            y0 = max(g.top() + 8, (at.y() if at else QtGui.QCursor.pos().y()) - h - 12)
        # 兜底：卡片比可用区域还高（超长详解）时，上下都夹住，别有一截掉到屏幕外
        if h >= g.height():
            y0 = g.top() + 8
        else:
            y0 = min(max(g.top() + 8, y0), g.bottom() - h - 8)
        self.move(int(x), int(y0))

    def _clamp_pos(self, h, x=None, y=None):
        """把 (x, y) 夹进屏幕：给定高度 h，保证整张卡片都在屏幕内。

        `_place()` 只在首次弹出时按锚点定位；之后任何高度变化（详解/追问）都得重新夹一次，
        否则贴着屏幕底部的卡片会"往下长"掉出屏幕。
        """
        scr = (QtWidgets.QApplication.screenAt(self.pos())
               or QtWidgets.QApplication.primaryScreen())
        g = scr.availableGeometry()
        x = self.x() if x is None else x
        y = self.y() if y is None else y
        lo_x = g.left() + 8
        hi_x = max(lo_x, g.right() - self.width() - 8)
        x = min(max(lo_x, x), hi_x)
        if h >= g.height():                     # 卡片比可用区还高，只能顶头放
            y = g.top() + 8
        else:
            y = min(max(g.top() + 8, y), g.bottom() - h - 8)
        return int(x), int(y)

    def _fit_height(self):
        """量出"内容真实需要"的窗口高度，且**不去动窗口**。

        两个关键点：
        1. 旧的写法是 `self.adjustSize()` —— 它会把窗口**立刻**设成新高度，那一帧会被画出来，
           于是长高动画看起来是"先闪到终点、再从头长一遍"。
        2. 量完之后把 frame 的高度钉住：动画期间窗口在变小/变大的过程里，外层布局会把 frame
           压成窗口的高度，里面的文字会跟着重排/被挤扁。钉住之后就是"窗口长高、内容被揭开"。
        """
        self.frame.setMaximumHeight(16777215)      # 先解除上一轮钉住的高度
        # `setFixedHeight` 是 min=max，只把 maximum 放开的话**下限还留着**，`adjustSize()`
        # 就只能往上长、缩不回去 —— 实测收起聊天区 / 换成短词之后卡片高度纹丝不动
        # （621 → 622）。所以测量前必须把下限也松掉。
        self.frame.setMinimumHeight(0)
        # **先 polish 再量**：QSS 的字号/字体要等 polish 才生效，不 polish 就量，
        # 量出来的是"构造字体"的行数（实测 322 字正文 299 vs 470，裁掉小半截）。
        # ensurePolished 会连同子控件一起 polish。
        self.frame.ensurePolished()
        # 宽度是定值，先钉住并激活布局：折行高度取决于宽度，宽度不定则量什么都不准。
        self.frame.setFixedWidth(self.width() - 16)   # 左右外边距 8+8
        self.frame.layout().activate()
        # 折行富文本标签的高度**手动钉**：Qt 嵌套布局对 heightForWidth 的传递不可靠
        # （实测标签 hfw 报 464，布局只分 299，正文被裁掉一截）。宽度此刻已定，直接
        # 按自己的 heightForWidth 钉高，量完放开 min/max，之后的布局仍能正常动它。
        for lab in (self.lb_cn, self.lb_alt, self.lb_en):
            if not lab.isHidden():
                lab.setFixedHeight(lab.heightForWidth(lab.width()))
        self.frame.adjustSize()
        self.frame.setFixedHeight(self.frame.height())
        return self.frame.height() + 20            # + 外边距 8(上) + 12(下)

    def queue_reveal(self, w, fade=None):
        """揭示一个「靠窗口长高才露出来」的控件（详解/聊天/有道对照/音标），但别让它啪出来、
        也别让它在长高期间跑 opacity 动画。

        做法：先把控件 opacity 钉在 0（静态，不发动画），排进待办队列；真正淡入由 `_after_grow()`
        在**长高动画 done 之后**统一触发。绝不在窗口几何变化期间跑 QGraphicsOpacityEffect 的 opacity
        动画——它重绘不跟随几何，真机上就是「点一下就闪/错位」那个老 bug。

        一个 grow 可能同时冒出好几个块（有道对照 + 音标），所以待办是**列表**；固定 `_later`
        兜底：万一调用方没走 `_resize_keep_place`（理论上都会走），slow 之后也把它们淡入，
        避免控件永远透明。
        """
        if fade is None:
            fade = DUR["fast"]
        # 换 effect 之前先掐掉这个控件上还在跑的淡入动画：setGraphicsEffect 会删掉旧
        # effect，而动画的 targetObject 就是它 —— 留着就会变成悬垂引用（见
        # test_b5_stagger_reuse_no_dangling 那条坑）。连续追问时这条路径很常见。
        old = w.graphicsEffect()
        if old is not None:
            _stop_anims(old, b"opacity")
        eff = QtWidgets.QGraphicsOpacityEffect(w)
        eff.setOpacity(0.0)
        w.setGraphicsEffect(eff)
        first = not self._reveal_queue          # 队列空 → 这是第一个，才需要起兜底定时器
        self._reveal_queue.append((w, fade))
        if first:
            _later(DUR["slow"], self, self._after_grow)

    def _stop_grow(self):
        """掐掉长高动画，并把它留下的闸门**一并复位**。

        动画正常结束时这些闸门由 `_resized()` 复位；被 stop 打断时 `finished` 不发，
        就永久留着了 —— 实测窗口被钳在 261px：之后复用这张卡弹一个长卡片，窗口长不高，
        底部的复制/钉住按钮落在 y=558，**在窗口外，既看不见也点不到**（用户报的
        "详解生成时不能复制不能钉住"）。
        所以掐动画必须连 `_resizing` 和 `_anim_h` 一起复位。（当年那个"钳住的高度"是
        窗口的 maximumHeight，现在已经不钉了 —— 改由 frame 跟着动画走，见 `_guard`。）
        """
        _stop_anims(self, b"geometry")
        self._anim_h = None
        self._resizing = False
        self._resize_again = False

    def _after_grow(self):
        """长高动画收尾时调用：把排队的 reveal 控件一起淡入，然后清空队列。

        兜底定时器（slow=380ms）可能落在**链式长高**的中间：内容连续变化时
        `_resize_again` 会再起一轮 240ms 动画，380 > 240 但对 480 来说正好撞上。
        所以这里必须再看一眼 `_resizing`——还在动就重新排队等下一轮 done，
        不能在这个状态下淡入（opacity 动画 × 几何变化 = 真机残影/闪动）。
        """
        # 重排**必须有界**：万一 `_resizing` 因为某条没走 `_resized` 的路径卡在 True，
        # 无限重排就等于"内容永远透明"（用户视角：点了详解但什么都没有 = 不能正常用）。
        # 所以最多让 4 轮，之后强制淡入——宁可牺牲一次淡入的时序，也不能让内容不出现。
        n = getattr(self, "_reveal_retries", 0)
        if getattr(self, "_resizing", False) and n < 4:
            self._reveal_retries = n + 1
            _later(DUR["fast"], self, self._after_grow)
            return
        self._reveal_retries = 0
        q = getattr(self, "_reveal_queue", None)
        if not q:
            return
        self._reveal_queue = []
        for w, fade in q:
            try:
                import shiboken6
                if shiboken6.isValid(w):
                    _fade_in(w, fade)
            except RuntimeError:
                pass

    def _resize_keep_place(self):
        """内容变高变矮时跟着改窗口大小；位置不动，高度动起来别跳。

        追问时文字是一段段往外吐的，每段都起 animation 会互相打架，所以用 `_resizing` 当闸门。
        **但闸门期间的请求不能丢** —— 丢了就会出现"点了详细解释、窗口还是聊天时那个高度，
        详解挤成一团／叠在按钮上"，也就是用户报的那个重叠 bug。所以记一个 `_resize_again`，
        等这轮动画跑完再量一次。
        """
        pos = self.pos()
        old = self.height()
        new = self._fit_height()            # 只量不设：别在动画开始前把窗口先撑到终点
        if getattr(self, "_resizing", False):
            self._resize_again = True       # 别丢：动画结束会再量一次
            return
        if abs(new - old) < 20:
            self.resize(self.width(), new)  # 小变化不值得动画，直接到位
            self.move(*self._clamp_pos(new, pos.x(), pos.y()))
            self._after_grow()               # 长高（哪怕没动画）收尾 → 揭示淡入
            return
        # 内容刚变（有道回来）时入场滑入可能还在跑：pos 和 geometry 两个动画会每帧互相覆盖，
        # 表现为卡片抖动/位置错乱。长高动画接管前先把滑入掐掉。
        _stop_anims(self, b"pos")
        # stagger 的 opacity 效果此刻可能还挂着（它在滑入结束后才开始，会和内容变化撞上）：
        # 窗口要变形了，先把效果摘掉，否则又是"文字画到旧位置"。
        self._clear_stagger()
        self._resizing = True
        self.resize(self.width(), old)
        # 起点先把 frame 压回"当前窗口"该有的高度：窗口长高、内容被揭开。
        # （_fit_height 量完会把 frame 钉在**终点**高度上，那是它量高度用的手段，
        #   动画期间必须由下面每帧的 _guard 抢回来。）
        self.frame.setFixedHeight(max(1, old - 20))
        # 终点 y 按**新高度**重新夹一次：卡片贴着屏幕底部时向下长会掉出屏幕
        # （实测长释义就掉出去 148px）。所以长高的同时把卡片往上带，动画里一起走。
        tx, ty = self._clamp_pos(new, pos.x(), pos.y())
        a = _anim(self, b"geometry",
                  QtCore.QRect(pos.x(), pos.y(), self.width(), old),
                  QtCore.QRect(tx, ty, self.width(), new),
                  DUR["base"], done=self._resized)

        def _guard(v, card=self):
            card._anim_h = int(v.height())
            # 让 frame 的高度**跟着动画走**，而不是把它自己钉在终点。
            #
            # 这里原来钉的是窗口的 maximumHeight，挡不住：窗口的延后布局激活是拿 frame 的
            # 固定高度当 sizeHint 去 resize 窗口的 —— _fit_height 量完就在那一瞬间把终点
            # 告诉了布局，于是动画中途窗口被一帧拽到终点（实测 want=304 时窗口 h=574，
            # 43 条「Unable to set geometry」、16 次回落，整段动画的中间值全被吃掉）。
            # frame 跟着动画走之后，布局想要的**永远接近**当前动画值（实测只超前 ≤75px），
            # 那一下就不存在了；剩下偶尔超前的那一下由下面的 resizeEvent 在同一事件里
            # 拉回，所以真机上既看不到拽到终点、也看不到回落。
            try:
                card.frame.setFixedHeight(max(1, int(v.height()) - 20))
            except RuntimeError:      # 卡片已经关掉了
                pass

        a.valueChanged.connect(_guard)

    def resizeEvent(self, e):
        # 抓边跟着窗口高走（长内容、改宽度都会走这里）
        self._place_grips()
        # 延后布局激活偶尔会把窗口**一次性顶到终点**（实测那一下 48px：先冲高、下一帧再缩回
        # 继续长），一帧的闪动在真机上看得见。这里在同一个事件里立刻拉回动画当前值，
        # 把它压成看不见的一次 setGeometry。
        if getattr(self, "_resizing", False):
            want = getattr(self, "_anim_h", None)
            if want and self.height() - want > 4:
                self.resize(self.width(), want)
                return
        super().resizeEvent(e)

    def _resized(self):
        # 动画收尾：把 frame 钉在终点高度上（动画期间它是跟着窗口走的，终点必须与窗口严丝合缝，
        # 否则内容会被压扁或被多留一条空白）。new == self.height()，所以这就是 _fit_height 量到的值。
        self.frame.setFixedHeight(max(1, self.height() - 20))
        self._anim_h = None
        self._resizing = False
        if getattr(self, "_resize_again", False):
            self._resize_again = False
            self._resize_keep_place()
            return
        self._after_grow()                    # 长高动画收尾 → 揭示淡入（绝不与几何动画重叠）

    # 拖卡片本体挪位置 / 拖四条边改尺寸
    #
    # 四条边的判定都在抓边子控件里（见 __init__：最外 8px 是投影留白、可见描边在 x=8，
    # 按坐标判边的话事件会落在 frame 上，变成"挪窗口"—— 这就是用户说的"拖大小没实现"）。
    def _begin_edge(self, edge, gpos):
        """开始拖某条边。事件来自哪条抓边由调用方判定（见 __init__ 里为什么要做成子控件）。"""
        if edge == "right":
            self._drag_w = (self.width(), gpos.x())
            self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
            return
        if edge == "left":
            # 左边拖动：窗口的右上角钉住不动（宽度变了多少，x 就往回退多少）
            self._drag_w = ("left", self.width(), gpos.x(), self.x() + self.width())
            self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
            return
        # 上下边改高度：能伸缩的只有详解/聊天区（卡片其余部分高度都是内容说了算），
        # 所以拖动实质是"把这块滚动区拉大/压小"，卡片高度跟着它走（见 _drag_height）。
        box = self._scroll_box()
        # 起点高度：可伸缩的那块（详解/聊天）没有时就是弹簧当前的占位高度。
        # **必须在这里读一次存下来**：`QSpacerItem.changeSize()` 会把 sizeHint 改成新值，
        # 拖动过程中再读 sizeHint() 拿到的是"已经长过的值"，每来一个 mouseMove 就再加一次
        # 位移 —— 表现就是"拖一点直接起飞"（实测）。所以和 box 那条一样，用起点 + 绝对位移。
        h_start = box.height() if box is not None else int(self._v_stretch.sizeHint().height())
        self._drag_h = (edge, gpos.y(), self.height(),
                        box, h_start,
                        self.y(), self.y() + self.height())
        self.setCursor(QtCore.Qt.CursorShape.SizeVerCursor)

    def _scroll_box(self):
        """当前可见的那块可伸缩长内容（详解优先，其次聊天区）。都没有就没得缩放。"""
        for box in (self.detail, self.chat_log):
            if box.isVisible():
                return box
        return None

    def _box_floor(self, box):
        """详解/聊天区各自的设计下限（setFixedHeight 会把 minimumHeight 一起抬上去，
        所以不能拿它当"还能缩多少"的依据，见 _drag_height 的算法）。"""
        return 96 if box is self.detail else 110

    def _end_edge(self):
        was_width = self._drag_w is not None
        box = self._drag_h[3] if self._drag_h is not None else None
        self._drag_w = None
        self._drag_h = None
        self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        # 拖高时把这块滚动区的 maximumHeight 一起抬了（否则它撑不到用户要的高度）；
        # 拖矮的情况再把它还原成自然上限 —— 不然用户往小拖一次，以后每次详解都被压在
        # 那个小高度上（set_detail 是拿 maximumHeight() 当上限用的，见 gui.py:1423）。
        if box is not None:
            box.setMinimumHeight(self._box_floor(box))
            if box.height() <= getattr(self, "_scroll_cap", 0):
                box.setMaximumHeight(self._scroll_cap)
        if was_width:
            self.width_changed.emit(int(self.width()))

    def eventFilter(self, obj, ev):
        # 四条抓边上的鼠标事件全由这里接管：抓边走的是子控件，Card.mousePressEvent 收不到。
        edges = {self.grip_l: "left", self.grip_r: "right",
                 self.grip_t: "top", self.grip_b: "bottom"}
        if obj in edges:
            t = ev.type()
            if t == QtCore.QEvent.Type.MouseButtonPress:
                if ev.button() == QtCore.Qt.MouseButton.LeftButton:
                    self._begin_edge(edges[obj], ev.globalPosition().toPoint())
                    return True
            elif t == QtCore.QEvent.Type.MouseMove:
                if self._drag_w is not None:
                    self._drag_width(ev)
                    return True
                if self._drag_h is not None:
                    self._drag_height(ev)
                    return True
            elif t == QtCore.QEvent.Type.MouseButtonRelease:
                if self._drag_w is not None or self._drag_h is not None:
                    self._end_edge()
                    return True
        return super().eventFilter(obj, ev)

    def mousePressEvent(self, e):
        # 四条边都由抓边接管了，这里只剩"拖卡片本体挪位置"。
        if e.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_w is not None:
            self._drag_width(e)
            return
        if self._drag_h is not None:
            self._drag_height(e)
            return
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def _drag_height(self, e):
        """拖上下边改高度：鼠标动的这一截全部转给当前可见的详解/聊天区，
        然后让**既有的"高度跟内容走"**（_fit_height）算出新高度 —— 不自己 resize 窗口。

        为什么要绕这一圈：卡片高度不是独立变量，窗口永远要严丝合缝地等于内容需要的高度
        （外层布局一被激活就会把窗口拉回 sizeHint，实测手动 resize 出来的高度会被
        1100px 那种离奇值顶掉）。所以"改高度"的正确做法是改那块滚动区的高度，
        再让 _fit_height 重新量一遍。详解/聊天区都带滚动条，拉高看更多、压矮留更多屏幕。
        """
        edge, y0, h0, box, box_h0, top0, bot0 = self._drag_h
        gy = e.globalPosition().toPoint().y()
        d = (gy - y0) if edge == "bottom" else (y0 - gy)      # 往下拖 = 变高
        if box is None:
            # 短卡（还没点详解/问 AI）没有可伸缩的内容：拖出来的那一截就是空白，
            # 用下面的弹簧当占位 —— 用户明确要"拖上下边改高度"，默默不动会被当成没实现。
            # 空白落在按钮行上方（弹簧在按钮行前面），所以按钮还是贴着底边。
            # 用 box_h0（拖动开始时的占位高）+ 绝对位移，**不能**用 sizeHint()+d（见 _begin_edge）。
            self._v_stretch.changeSize(
                0, max(0, box_h0 + d),
                QtWidgets.QSizePolicy.Policy.Minimum,
                QtWidgets.QSizePolicy.Policy.Expanding)
        else:
            box_h = int(max(self._box_floor(box),
                            min(box_h0 + d, self._box_limit(h0, box_h0))))
            if box_h != box.height():
                box.setFixedHeight(box_h)
        self._resizing = False              # 手动拖的时候"长高动画"的闸门必须是关的
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        h = int(self._fit_height())
        self.setFixedHeight(h)
        top = top0 if edge == "bottom" else bot0 - h
        self.move(*self._clamp_pos(h, self.x(), top))

    def _box_limit(self, h0, box_h0):
        """这块滚动区最多能长多高：整块屏装得下卡片（卡片里除了它的那部分高度不变）。"""
        scr = (QtWidgets.QApplication.screenAt(self.pos())
               or QtWidgets.QApplication.primaryScreen())
        gh = scr.availableGeometry().height() if scr is not None else 1080
        return box_h0 + max(0, gh - 16 - h0)

    def _drag_width(self, e):
        gx = e.globalPosition().toPoint().x()
        # 下限 360（再窄释义没法读），上限取"当前这块屏的 45%"和 720 里更小的那个：
        # 430 是主尺寸，但大屏上允许拖宽一点（长英文释义少折几行）。
        scr = (QtWidgets.QApplication.screenAt(self.pos())
               or QtWidgets.QApplication.primaryScreen())
        avail = scr.availableGeometry().width() if scr is not None else 1920
        hi = max(360, min(int(avail * 0.45), 720))
        right = None
        if self._drag_w[0] == "left":                  # 抓的是左边：右上角钉住不动
            _, w0, x0, right = self._drag_w
            w = max(360, min(hi, w0 + (x0 - gx)))      # 往左拖 = 变宽
        else:                                          # 抓的是右边：左上角钉住不动
            w0, x0 = self._drag_w
            w = max(360, min(hi, w0 + (gx - x0)))
        if w == self.width():
            return
        self._w = w
        self.setFixedWidth(w)
        self.lb_word.setMaximumWidth(self._word_cap())   # 词头留给词的那一段跟着卡宽走
        self.setMaximumHeight(16777215)     # 变窄后行数变了，重新量高度（_fit_height 会夹回来）
        new = self._fit_height()
        self.setFixedHeight(new)
        if right is not None:
            self.move(right - w, self.y())
        self.move(*self._clamp_pos(new, self.x(), self.y()))
        self.update()

    def mouseReleaseEvent(self, _):
        self._drag = None
        if self._drag_w is not None:
            self._end_edge()

    def wheelEvent(self, e):
        """卡片空白处的滚轮转给"正在显示的那块长内容"。

        实测：详解/聊天区自己都能滚（详解滚动条 max=956），但鼠标停在卡片别处（词头、
        例句、按钮之间的空白）时一样什么都没发生——用户说的"不能滚动"就是这个。
        没有可滚的内容就不接受事件，让它按默认往上冒（避免把滚轮吃在一个静止的卡片上）。
        """
        for box in (self.chat_log, self.detail):
            if box.isVisible() and box.verticalScrollBar().maximum() > 0:
                bar = box.verticalScrollBar()
                bar.setValue(bar.value() - e.angleDelta().y())
                e.accept()
                return
        super().wheelEvent(e)


class Tray(QtWidgets.QSystemTrayIcon):
    def __init__(self, app, hotkey, on_settings, on_quit):
        super().__init__()
        pm = QtGui.QPixmap(64, 64)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setBrush(QtGui.QColor(T["blue"]))      # 托盘圆点也走令牌：原来是令牌表外的 #2d5c8f
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 60, 60)
        p.setPen(QtGui.QColor(T["ink"]))
        f = p.font()
        f.setPointSize(30)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "S")
        p.end()
        self.setIcon(QtGui.QIcon(pm))
        self.setToolTip("SnapWord v%s —— %s 查屏幕上的词" % (__version__, hotkey))
        m = QtWidgets.QMenu()
        self._menu = m
        m.installEventFilter(self)    # 菜单弹出时淡入（见 eventFilter）
        self.pick_action = m.addAction("查屏幕上的词（%s）" % hotkey, lambda: app.start_pick())
        m.addAction("查剪贴板", lambda: app.lookup_text(QtWidgets.QApplication.clipboard().text()))
        m.addSeparator()
        self._tail = m.addSeparator()      # 常驻面板那几项插在这条分隔线上面
        m.addAction("设置…", on_settings)
        m.addAction("退出", on_quit)
        self.setContextMenu(m)
        self.activated.connect(
            lambda r: app.start_pick() if r == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None)

    def attach_dock(self, dock):
        """把常驻面板的开关挂进托盘右键菜单。

        面板可以整个关掉（有人不想要那条边），关掉之后**只能从这里再打开**，所以必须有。
        """
        m = self._menu
        self.dock_show = QtGui.QAction("显示常驻面板", m)
        self.dock_show.setCheckable(True)
        self.dock_show.setChecked(dock.isVisible())
        self.dock_show.toggled.connect(dock.set_enabled)
        m.insertAction(self._tail, self.dock_show)

        self.dock_toggle = QtGui.QAction("展开 / 收起面板", m)
        self.dock_toggle.triggered.connect(lambda: dock.toggle())
        m.insertAction(self._tail, self.dock_toggle)

        self.screen_menu = QtWidgets.QMenu("面板挂在哪块屏", m)
        self.screen_menu.installEventFilter(self)      # 子菜单也淡入，跟主菜单一个节奏
        m.insertMenu(self._tail, self.screen_menu)
        self._scr_grp = QtGui.QActionGroup(self.screen_menu)
        self._scr_grp.setExclusive(True)
        self._fill_screens(dock)
        m.aboutToShow.connect(lambda: self._sync_dock(dock))

    def _fill_screens(self, dock):
        self.screen_menu.clear()
        primary = QtWidgets.QApplication.primaryScreen()
        for s in QtWidgets.QApplication.screens():
            name = "" if s is primary else s.name()      # 空 = 主屏
            g = s.geometry()
            a = self.screen_menu.addAction("%s（%d×%d）" % (s.name(), g.width(), g.height()))
            a.setCheckable(True)
            a.setChecked((dock._screen_name or "") == name)
            a.triggered.connect(lambda _=False, n=name: dock.set_screen(n))
            self._scr_grp.addAction(a)

    def _sync_dock(self, dock):
        self.dock_show.setChecked(dock.isVisible())
        self._fill_screens(dock)      # 屏幕可能刚插上/拔掉

    def eventFilter(self, obj, ev):
        # 托盘菜单弹出的瞬间淡入（docs/UI-STYLE.md 动效表欠的那条）。只做进不做出：
        # QMenu 的收起时机拿不稳，硬做淡出会闪，宁缺。
        if isinstance(obj, QtWidgets.QMenu) and ev.type() == QtCore.QEvent.Type.Show:
            obj.setWindowOpacity(0.0)
            _anim(obj, b"windowOpacity", 0.0, 1.0, DUR["fast"])
        return super().eventFilter(obj, ev)


class Rail(QtWidgets.QFrame):
    """屏幕右缘那条 26px 细边（常驻面板的把手）。

    整条自己画，不摆子控件。原来把 "SnapWord" 拆成一个字母一行（图省事，不写旋转绘制），
    行距把字距拉散，远看就是一串散字母 —— 这是"侧边栏丑"的直接来源。现在：
      · 顶部一个 monogram 小方块（blue-soft 底 + blue 的 S），是块牌子，不是孤零零一个字母；
      · 中间把整词 "SnapWord" **旋转**成竖排，字距连续；
      · 悬停时左内缘亮出一道 2px 蓝色光边（高度用动画走 0→40）—— 全应用统一的那道"光边"，
        出现在细边、面板分隔线、卡片顶沿上，是 SnapWord 的识别符号。
    """

    def __init__(self, parent=None):
        super().__init__(parent, objectName="dockrail")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._bar = 0
        self.hovered = False
        self.expanded = False

    def get_bar(self):
        return self._bar

    def set_bar(self, v):
        self._bar = int(v)
        self.update()          # 重画就行，不动布局

    barH = QtCore.Property(int, get_bar, set_bar)

    def enterEvent(self, ev):
        self.hovered = True
        _anim(self, b"barH", self._bar, 40, DUR["base"])
        self.update()

    def leaveEvent(self, ev):
        self.hovered = False
        _anim(self, b"barH", self._bar, 0, DUR["fast"],
              curve=QtCore.QEasingCurve.Type.InCubic)
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        surface = QtGui.QColor(T["card"] if self.hovered else T["surface"])

        # 左边两角圆、右边两角方（贴屏幕边不能有圆角缺口）
        path = QtGui.QPainterPath()
        path.moveTo(8, 0)
        path.lineTo(w, 0)
        path.lineTo(w, h)
        path.lineTo(8, h)
        path.quadTo(0, h, 0, h - 8)
        path.lineTo(0, 8)
        path.quadTo(0, 0, 8, 0)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(surface)
        p.drawPath(path)

        edge = QtGui.QPainterPath()      # 描边不画右边那条（那边是屏幕边缘）
        edge.moveTo(8, 0.5)
        edge.lineTo(w, 0.5)
        edge.moveTo(w, h - 0.5)
        edge.lineTo(8, h - 0.5)
        edge.quadTo(0.5, h - 0.5, 0.5, h - 8)
        edge.lineTo(0.5, 8)
        edge.quadTo(0.5, 0.5, 8, 0.5)
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QColor(T["hairline_strong" if self.hovered else "hairline"]), 1))
        p.drawPath(edge)

        # monogram 小方块
        tile = QtCore.QRectF((w - 18) / 2.0, 10, 18, 18)
        soft = QtGui.QColor(T["blue"])
        soft.setAlpha(38)                # = blue-soft（rgba(87,193,255,0.15)）
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(soft)
        p.drawRoundedRect(tile, 6, 6)
        f = p.font()
        f.setPointSize(10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QtGui.QColor(T["blue"]))
        p.drawText(tile, QtCore.Qt.AlignmentFlag.AlignCenter, "S")

        # 竖排整词：旋转绘制，字距连续
        f = p.font()
        f.setBold(False)
        f.setPointSize(9)
        f.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 0.8)
        p.setFont(f)
        p.setPen(QtGui.QColor(T["mute"] if self.hovered else T["ash"]))
        p.save()
        p.translate(w / 2.0 + 4, h / 2.0)      # +4：字体基线偏左，补一点才视觉居中
        p.rotate(-90)
        p.drawText(QtCore.QRectF(-(h / 2.0 - 40), -7, h - 74, 14),
                   QtCore.Qt.AlignmentFlag.AlignCenter, "SnapWord")
        p.restore()

        # 底部箭头：跟着展开/收起翻向
        f = p.font()
        f.setPointSize(11)
        f.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 0)
        p.setFont(f)
        p.setPen(QtGui.QColor(T["blue"] if self.hovered else T["mute"]))
        p.drawText(QtCore.QRectF(0, h - 24, w, 16), QtCore.Qt.AlignmentFlag.AlignCenter,
                   "›" if self.expanded else "‹")

        # 悬停光边：一道 2px 蓝，高度由动画推动
        if self._bar > 0:
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(T["blue"]))
            p.drawRoundedRect(QtCore.QRectF(0, (h - self._bar) / 2.0, 2, self._bar), 1, 1)


class _WordbookFold(QtWidgets.QWidget):
    """Render the panel hinge with motion-synced content softening."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._pix = QtGui.QPixmap()
        self._soft = QtGui.QPixmap()
        self._progress = 0.0
        self._delta = 0.0
        self._blur_strength = 0.0

    def start(self, pix, progress):
        w, h = self.parentWidget().WB_W, self.parentWidget().PANEL_H
        # QWidget.grab() can return device pixels on a scaled Windows display.
        # Keep the preview in logical widget coordinates so its last frame is
        # exactly the same size as the real panel.
        image = pix.toImage()
        if image.size() != QtCore.QSize(w, h):
            image = image.scaled(w, h, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                                 QtCore.Qt.TransformationMode.SmoothTransformation)
        image.setDevicePixelRatio(1.0)
        self._pix = QtGui.QPixmap.fromImage(image)
        # Soften the actual screen content, not just the edge silhouette. Build
        # it once per toggle rather than filtering a 300x300 image per frame.
        low = image.scaled(max(1, w // 4), max(1, h // 4),
                           QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                           QtCore.Qt.TransformationMode.SmoothTransformation)
        self._soft = QtGui.QPixmap.fromImage(low.scaled(
            w, h, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation))
        self._progress = max(0.0, min(1.0, float(progress)))
        self._delta = 0.0
        self._blur_strength = 0.0
        self.setGeometry(0, 0, w, h)
        self.show()
        self.update()

    def set_progress(self, progress):
        value = max(0.0, min(1.0, float(progress)))
        self._delta = value - self._progress
        self._progress = value
        # Motion blur is strongest while the hinge is moving and fades naturally
        # at both endpoints, where the live widget takes over pixel-for-pixel.
        self._blur_strength = (min(1.0, abs(self._delta) * 24.0)
                               * math.sin(math.pi * value) if 0.0 < value < 1.0 else 0.0)
        self.update()

    def _draw_frame(self, painter, progress, pix, opacity=1.0):
        w, h = self.width(), self.height()
        if progress >= 1.0:
            painter.setOpacity(opacity)
            painter.drawPixmap(0, 0, pix)
            painter.setOpacity(1.0)
            return
        if progress <= 0.0:
            return
        painter.setOpacity(opacity)
        slices = 24
        # Foreshortening is anchored at the right hinge; its distortion tends
        # continuously to zero at full extension (no last-frame jump).
        spread = progress ** 0.82
        for i in range(slices):
            u0, u1 = i / slices, (i + 1) / slices
            src = QtCore.QRectF(u0 * w, 0, (u1 - u0) * w, h)
            bend = 1.0 + 0.18 * (1.0 - progress)
            d0 = (1.0 - u0) ** bend
            d1 = (1.0 - u1) ** bend
            x0 = w - w * d0 * spread
            x1 = w - w * d1 * spread
            dst = QtCore.QRectF(x0, 0, max(0.01, x1 - x0), h)
            painter.drawPixmap(dst, pix, src)
        painter.setOpacity(1.0)

    def paintEvent(self, _event):
        if self._pix.isNull() or self._progress <= 0.0:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        # Paint the sharp frame first and soften the content on top. SourceOver
        # preserves full opacity where both frames overlap (no grey wash).
        self._draw_frame(painter, self._progress, self._pix)
        if self._blur_strength > 0.01:
            self._draw_frame(painter, self._progress, self._soft,
                             min(0.88, self._blur_strength * 0.88))
        painter.end()


class Dock(QtWidgets.QWidget):
    """常驻屏幕右边缘的小面板：收起时只是一条细边，点一下展开。

    存在的意义是**不依赖热键**：框选、查剪贴板、手动输入、设置、退出全都能点出来，
    热键一个都没注册上（这台机器上 ctrl+alt+D / W 就被别人占了）照样能用。

    - 不抢焦点：`WA_ShowWithoutActivating` + `Qt.Tool`，`show()` 不 `activateWindow()`；
      只有你自己点进输入框，键盘才归它。
    - 点细边 = 展开/收起；按住细边上下拖 = 挪个位置（只贴着右边缘上下动）。
    - 展开状态和竖直位置记在 `config` 的 `dock` 里，下次启动照旧。
    """

    RAIL_W = 26
    PANEL_W = 320
    PANEL_H = 300
    EDGE_GAP = 0            # 贴死屏幕右边缘（留缝会被看成"没贴边"）
    WB_W = 300              # 记词板（挂在主面板左边，自己一扇"活板门"）
    PANEL_W_MIN = 260       # 主面板宽度可拖：下限（再窄按钮会挤破）
    PANEL_W_MAX = 460       # 上限（再宽会盖住屏幕里太多东西）

    def __init__(self, cfg, on_pick, on_lookup, on_clip, on_settings, on_quit,
                 wb=None, on_word=None):
        super().__init__(None)
        self.cfg = cfg
        d = cfg.get("dock") or {}
        self.expanded = bool(d.get("expanded"))
        self.wb_open = bool(d.get("wb"))
        # 记词板露出来多少（0..WB_W）：活板门是"掀开"而不是整块平移，所以
        # 抽屉永远停在 x=0，靠 mask 把"已掀开的那条"裁出来 —— 见 _apply_mask()。
        self._wb_reveal = float(self.WB_W if self.wb_open else 0)
        self._y = d.get("y")
        self._drag = None
        self._moved = False
        self._grip_drag = None
        self._pan_w = int(d.get("panel_w") or 0) or self.PANEL_W
        # 记词板的数据源（可能在测试里是 None：Dock 的 UI 必须还能建起来）
        self.wb = wb
        self.on_word = on_word or (lambda _w: None)
        # 钉死在一台屏幕上。**不能**用 self.screen() —— 它是按窗口当前所在位置推断的，
        # 点在细边的子控件上时事件被转发过来，那一瞬间会解成别的屏幕，窗口直接被甩到副屏
        # （实测 geo 从 QRect(1689,396,352,360) 变成 QRect(3908,353,293,300)）。
        # 也不默认"鼠标所在那块屏"：鼠标在副屏时用户会看到面板跑到副屏上（m01201 就是这么来的）。
        # 空字符串 = 主屏；只有用户在设置/托盘里显式挑过某块屏，才按名字钉死。
        self._screen_name = d.get("screen") or ""
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(QSS)
        self.setFont(QtGui.QFont("Microsoft YaHei UI", 10))
        self._build(on_pick, on_lookup, on_clip, on_settings, on_quit)
        self._apply()

    # ---------- 界面 ----------
    def _build(self, on_pick, on_lookup, on_clip, on_settings, on_quit):
        # 两个子控件都手动摆位置，**不用 layout**：rail 永远贴在窗口最右边 26px（也就是屏幕
        # 右边缘），panel 在窗口坐标 x=W→0 之间滑，配合窗口从 26 宽长到 352 宽，看上去就是
        # "面板从细边后面抽出来"。用 layout 做不到：窗口一变窄，layout 会把 rail 留在旧的
        # 坐标上（跑到窗口外面去），而收起时窗口只有 26 宽。
        self.panel = QtWidgets.QFrame(self, objectName="dockpanel")
        self.panel.setFixedWidth(self._pan_w)
        self.panel.setFixedHeight(self.PANEL_H)
        # 主面板左边缘的抓条：宽度可拖。**必须单独一个控件**：面板里铺满了子控件，
        # 鼠标落在边上多半落在子控件身上，事件到不了 Dock（细边那条靠 eventFilter 也是同理）。
        self.grip = QtWidgets.QFrame(self.panel, objectName="dockgrip")
        self.grip.setFixedWidth(6)
        self.grip.setFixedHeight(self.PANEL_H)
        self.grip.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        self.grip.setToolTip("拖动改面板宽度")
        self.grip.installEventFilter(self)

        v = QtWidgets.QVBoxLayout(self.panel)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(12)            # 10 不在令牌刻度（2/4/8/12/16/24）上

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(QtWidgets.QLabel("SnapWord", objectName="panelword"))
        head.addWidget(QtWidgets.QLabel("v" + __version__, objectName="chip"))
        head.addStretch(1)
        v.addLayout(head)
        # 头下面那道渐隐的蓝线：和细边的光边同一套识别符号，面板因此有"头"而不是一整块灰
        streak = QtWidgets.QFrame(objectName="streak")
        streak.setFixedHeight(1)
        v.addWidget(streak)

        self.input = SmoothLineEdit()
        self.input.setPlaceholderText("输入或粘贴一个词，回车查")
        self.input.setMinimumHeight(32)
        self.input.returnPressed.connect(lambda: on_lookup(self.input.text()))
        v.addWidget(self.input)

        b1 = QtWidgets.QHBoxLayout()
        b1.setSpacing(8)
        go = SmoothButton("框选屏幕", "primary")
        go.clicked.connect(lambda: on_pick())
        clip = SmoothButton("剪贴板", "normal")
        clip.clicked.connect(on_clip)
        b1.addWidget(go, 3)
        b1.addWidget(clip, 2)
        v.addLayout(b1)

        # 记词板：一整行按钮（宽度跟着面板走，不跟别的按钮抢空间）
        self.btn_wb = SmoothButton("记词板", "normal")
        self.btn_wb.setToolTip("存下来的词（活板门从左边展开）")
        self.btn_wb.clicked.connect(self.toggle_wordbook)
        v.addWidget(self.btn_wb)

        self.lb_hint = QtWidgets.QLabel("", objectName="note")
        self.lb_hint.setWordWrap(True)

        v.addStretch(1)
        v.addWidget(self.lb_hint)       # 提示紧贴底部按钮，中间留白才不像没画完
        v.addSpacing(2)

        b2 = QtWidgets.QHBoxLayout()
        b2.setSpacing(4)
        st = SmoothButton("设置", "ghost")
        st.clicked.connect(on_settings)
        qt = SmoothButton("退出", "ghost")
        qt.clicked.connect(on_quit)
        b2.addStretch(1)
        b2.addWidget(st)
        b2.addWidget(qt)
        v.addLayout(b2)

        # 细边：整条自己画（见 Rail），不摆子控件 —— 摆字母会把字距拉散
        self.rail = Rail(self)
        self.rail.setFixedWidth(self.RAIL_W)
        self.rail.installEventFilter(self)

        # 记词板那一扇"活板门"（窗口最左边，收起时整扇滑到窗口外）
        self.wbpanel = QtWidgets.QFrame(self, objectName="wbpanel")
        self.wbpanel.setFixedWidth(self.WB_W)
        self.wbpanel.setFixedHeight(self.PANEL_H)
        wv = QtWidgets.QVBoxLayout(self.wbpanel)
        wv.setContentsMargins(14, 14, 14, 12)
        wv.setSpacing(8)
        self.wb_head = QtWidgets.QLabel("记词板", objectName="panelword")
        wv.addWidget(self.wb_head)
        streak2 = QtWidgets.QFrame(objectName="streak")
        streak2.setFixedHeight(1)
        wv.addWidget(streak2)
        self.wb_scroll = QtWidgets.QScrollArea()
        self.wb_scroll.setWidgetResizable(True)
        self.wb_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.wb_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.wb_scroll.setStyleSheet("QScrollArea{background:transparent}"
                                     "QScrollArea>QWidget>QWidget{background:transparent}")
        self.wb_list = QtWidgets.QWidget()
        self.wb_v = QtWidgets.QVBoxLayout(self.wb_list)
        self.wb_v.setContentsMargins(0, 0, 0, 0)
        self.wb_v.setSpacing(4)
        self.wb_v.addStretch(1)
        self.wb_scroll.setWidget(self.wb_list)
        self.wb_scroll.viewport().setStyleSheet("background:transparent")
        wv.addWidget(self.wb_scroll, 1)
        self.wb_empty = QtWidgets.QLabel("还没有存过词。\n框选或查一个词，点卡片上的「存词」。",
                                         objectName="note")
        self.wb_empty.setWordWrap(True)
        wv.addWidget(self.wb_empty)
        wbrow = QtWidgets.QHBoxLayout()
        wbrow.setSpacing(8)
        self.btn_wb_copy = SmoothButton("复制全部", "ghost")
        self.btn_wb_copy.setToolTip("把整板的词按行复制进剪贴板")
        self.btn_wb_copy.clicked.connect(self._wb_copy_all)
        self.btn_wb_export = SmoothButton("导出", "ghost")
        self.btn_wb_export.setToolTip("导出成 Markdown 或 CSV（按扩展名自动选）")
        self.btn_wb_export.clicked.connect(self._wb_export)
        self.btn_wb_clear = SmoothButton("清空", "ghost")
        self.btn_wb_clear.clicked.connect(self._wb_clear)
        wbrow.addWidget(self.btn_wb_copy, 1)
        wbrow.addWidget(self.btn_wb_export, 1)
        wbrow.addWidget(self.btn_wb_clear, 1)
        wv.addLayout(wbrow)
        # 抽屉永远停在窗口最左边 x=0；关着的时候靠 mask（_apply_mask）把它整条裁掉，
        # 所以关→开是"掀开"（可见宽度 0→WB_W），不是整块滑进来。
        self.wbpanel.move(0, 0)
        self._wb_fold = _WordbookFold(self)
        self._wb_fold.setGeometry(0, 0, self.WB_W, self.PANEL_H)
        self._wb_fold.hide()
        self.wbpanel.setVisible(self.wb_open)
        # 记词板是"从面板背后抽出来的抽屉"：关着的时候它就停在面板正后方（同一个 x），
        # 所以面板必须压在它上面 —— 这条 raise_ 是那扇门的"门框"。
        self.panel.raise_()
        self.rail.raise_()
        self.grip.raise_()          # 抓条要在面板内容之上才收得到鼠标
        self.refresh_wordbook()

    def set_hint(self, text):
        self.lb_hint.setText(text)

    # ---------- 记词板 ----------
    def toggle_wordbook(self):
        """「活板门」：记词板是窗口最左边那一扇，打开是"掀开"，不是整块平移。

        为什么不是平移：抽屉原来从 x=WB_W 滑到 x=0（整块滑出来），用户报"不像活板门"。
        抽屉始终在窗口内 x=0；动画时由铰链侧展开的预览层绘制折叠画面，
        真实抽屉暂时隐藏，结束后恢复交互。窗口 mask 只在两个稳态切换。
        曲线 OutCubic（开）/ InCubic（关）：门是"荡开、收住"，不用过冲回弹。
        """
        previous = getattr(self, "_wb_anim", None)
        if previous is not None:
            previous.stop()
            previous.deleteLater()
            self._wb_anim = None
        self.wb_open = not self.wb_open
        self.refresh_wordbook()
        self.wbpanel.move(0, 0)
        self.wbpanel.show()
        self.wbpanel.clearMask()
        self.panel.raise_()
        self.rail.raise_()
        self.grip.raise_()
        if self.wb_open:
            self._apply_mask()
        pix = self.wbpanel.grab()
        self.wbpanel.hide()
        self._wb_fold.start(pix, self._wb_reveal / float(self.WB_W))
        self._wb_fold.raise_()
        _stop_anims(self.wbpanel, b"pos")       # 旧版留下过的位移动画：别让两层动画打架
        a = QtCore.QVariantAnimation(self)
        a.setStartValue(float(self._wb_reveal))
        a.setEndValue(float(self.WB_W if self.wb_open else 0.0))
        a.setDuration(1 if REDUCE["on"] else int(DUR["base"]))
        a.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic if self.wb_open
                         else QtCore.QEasingCurve.Type.InCubic)
        a.valueChanged.connect(lambda v: self._set_wb_reveal(float(v)))
        a.finished.connect(self._settle)
        self._wb_anim = a                        # 留引用：被 GC 掉动画会停在半开
        a.start()
        self._save()

    def _set_wb_reveal(self, v):
        self._wb_reveal = v
        self._clip_wordbook()
        if hasattr(self, "_wb_fold"):
            self._wb_fold.set_progress(float(v) / float(self.WB_W))

    def _clip_wordbook(self):
        """把抽屉裁到"已掀开的那条"（`[WB_W-r, WB_W)`）。

        **必须裁在子控件上，不能逐帧改窗口 mask**：窗口 mask 走 `SetWindowRgn`，而这窗口是
        分层（`WA_TranslucentBackground`）窗口 —— 每帧把 region 放大时，Qt 并不一定跟着重画
        新露出来的那条，屏幕上就是"记词板直接消失、只偶尔闪一小块"（用户实测）。子控件自己的
        mask 由 Qt 光栅化器处理，改完顺手 `update()` 把露出来的那条重画，不牵扯窗口系统。
        """
        r = int(round(min(max(self._wb_reveal, 0.0), float(self.WB_W))))
        if r <= 0:
            # 空 QRegion = "没有 mask" = 整块可见，所以不能用 QRegion() 表示"全遮住"，
            # 得给一条落在控件外面的一像素：可见区是 mask ∩ 控件，交集空 = 什么都看不见。
            self.wbpanel.setMask(QtGui.QRegion(-1, -1, 1, 1))
        elif r >= self.WB_W:
            self.wbpanel.clearMask()
        else:
            self.wbpanel.setMask(QtGui.QRegion(self.WB_W - r, 0, r, self.PANEL_H))
        self.wbpanel.update()

    def _wb_counts(self):
        if self.wb is None:
            return 0, 0
        try:
            items = self.wb.items()
        except Exception:
            traceback.print_exc()
            return 0, 0
        return len(items), sum(1 for it in items if it.get("mastered"))

    def refresh_wordbook(self):
        """重建列表（增删改之后都走这里，别去改单行——列表是"真相的投影"）。"""
        n, mastered = self._wb_counts()
        tail = "（%d 已掌握）" % mastered if mastered else ""
        self.wb_head.setText("记词板 · %d 词%s" % (n, tail))
        self.btn_wb.setText("记词板 · %d" % n if n else "记词板")
        # 清空旧行（最后一项是 addStretch(1)）
        while self.wb_v.count() > 1:
            it = self.wb_v.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.wb_empty.setVisible(not n)
        if self.wb is None or not n:
            return
        items = self.wb.items()[:200]       # 列表只画前 200 条，再多也没人翻
        for it in items:
            self.wb_v.insertWidget(self.wb_v.count() - 1, self._wb_row(it))
        if self.wb_open:
            self.wb_scroll.verticalScrollBar().setValue(0)

    def _wb_row(self, it):
        row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        word = it["word"]
        b = SmoothButton(word, "ghost")
        b.setToolTip("再查一次：%s" % (it.get("cn") or word))
        b.clicked.connect(lambda _=False, w=word: self.on_word(w))
        b.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                        QtWidgets.QSizePolicy.Policy.Fixed)
        h.addWidget(b, 1)
        cp = SmoothButton("⧉", "ghost")
        cp.setFixedWidth(30)
        cp.setToolTip("复制这个单词")
        cp.clicked.connect(lambda _=False, w=word, btn=cp: self._wb_copy(w, btn))
        h.addWidget(cp)
        star = SmoothButton("★" if it.get("mastered") else "☆", "ghost")
        star.setFixedWidth(30)
        star.setToolTip("点一下取消「已掌握」" if it.get("mastered") else "标成「已掌握」（沉到列表底部）")
        star.clicked.connect(lambda _=False, w=word, on=not it.get("mastered"):
                             self._wb_master(w, on))
        h.addWidget(star)
        x = SmoothButton("✕", "ghost")
        x.setFixedWidth(30)
        x.setToolTip("从记词板删掉")
        x.clicked.connect(lambda _=False, w=word: self._wb_del(w))
        h.addWidget(x)
        return row

    def _wb_master(self, word, on):
        if self.wb is not None:
            self.wb.set_mastered(word, on)
        self.refresh_wordbook()

    def _wb_del(self, word):
        if self.wb is not None:
            self.wb.remove(word)
        self.refresh_wordbook()

    def _wb_copy(self, word, btn=None):
        """复制单个词：剪贴板 + 按钮绿闪 + 底部提示（三处反馈，用户才知道点到了）。"""
        QtWidgets.QApplication.clipboard().setText(word)
        if btn is not None:
            btn.flash_success()
        self.set_hint("已复制：%s" % word)

    def _wb_copy_all(self):
        """整板的词按行复制（一行一个，直接粘进生词本/Anki 那种地方）。"""
        if self.wb is None:
            return
        try:
            words = [it["word"] for it in self.wb.items()]
        except Exception:
            traceback.print_exc()
            return
        if not words:
            self.set_hint("记词板还是空的")
            return
        QtWidgets.QApplication.clipboard().setText("\n".join(words))
        self.btn_wb_copy.flash_success()
        self.set_hint("已复制 %d 个词到剪贴板" % len(words))

    def _wb_clear(self):
        if self.wb is None or not self._wb_counts()[0]:
            return
        r = QtWidgets.QMessageBox.question(
            self, "清空记词板", "把 %d 个词都删掉？这个动作不能撤销。" % self._wb_counts()[0],
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.wb.clear()
        self.refresh_wordbook()
        self.set_hint("记词板已清空")

    def _wb_export(self):
        if self.wb is None or not self._wb_counts()[0]:
            self.set_hint("记词板还是空的，没东西可导出")
            return
        from pathlib import Path
        home = Path.home() / "Desktop"
        if not home.is_dir():
            home = Path.home()
        default = str(home / "SnapWord-记词板.md")
        path, _sel = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出记词板", default, "Markdown (*.md);;CSV (*.csv)")
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                self.wb.export_csv(path)
            else:
                self.wb.export_md(path)
        except Exception as e:
            traceback.print_exc()
            self.set_hint("导出失败：%s" % e)
            return
        self.set_hint("已导出到 %s" % path)

    # ---------- 展开 / 收起 / 摆位置 ----------
    def toggle(self):
        self.expanded = not self.expanded
        if not self.expanded and self.wb_open:
            # 主面板收回去时记词板一起收：那扇门本来就没有"自己单独开着"的样子
            # （它的开关按钮在主面板里，展开时才有）。
            self.wb_open = False
            self._wb_reveal = 0.0
            self.wbpanel.setVisible(False)
        self._apply(animate=True)
        if self.expanded:
            self.input.setFocus()       # 展开就是为了打字（点细边本来就是用户主动操作）
        self._save()

    def set_screen(self, name):
        """换一块屏挂（设置 / 托盘里选的）。"""
        self._screen_name = name or ""
        self._apply(animate=False)
        self._save()

    def set_enabled(self, on):
        self.setVisible(bool(on))
        if on:
            self.raise_()
        self.cfg.setdefault("dock", {})["enabled"] = bool(on)
        self._save()

    def _screen(self):
        """盯死一块屏（按名字找；没指定或找不到就主屏）。"""
        if self._screen_name:
            for s in QtWidgets.QApplication.screens():
                if s.name() == self._screen_name:
                    return s
        return QtWidgets.QApplication.primaryScreen()

    def _apply(self, animate=False):
        self.rail.expanded = self.expanded      # 箭头方向由 Rail 画，翻向跟着状态走
        self.rail.setToolTip("收起面板" if self.expanded else "展开 SnapWord 面板（不用热键）")
        self._place(animate=animate)
        self.raise_()               # 别人也是 topmost 的话，每次变化都再顶一次

    def _place(self, animate=False):
        scr = self._screen()
        if scr is None:
            return
        g = scr.availableGeometry()      # 扣掉任务栏那一条（这台机器上任务栏自动隐藏，等于全屏）
        x0 = self.WB_W                   # 主面板展开时的窗口内 x：左边 300px 留给记词板
        W = x0 + self._pan_w + 6 + self.RAIL_W   # 记词板 + 主面板 + 6px 缝 + rail（rail 别被窗口裁掉）
        h = self.PANEL_H
        y = g.y() + (g.height() - h) // 2 if self._y is None else int(self._y)
        y = min(max(g.y(), y), max(g.y(), g.y() + g.height() - h))
        # 右边缘永远是屏幕右边缘（EDGE_GAP=0）。窗口宽度只随面板宽度变，展开/收起只滑
        # panel，不改窗口尺寸。**记词板是主面板背后那层抽屉**：窗口一开始就有它的位置，
        # 关着时它停在 x=WB_W（面板正后方，被面板盖住），所以开不开记词板主面板一个像素都不动。
        # 旧实现是窗口 26↔352 地 resize，它一口气带来两个用户能看到的毛病：
        # 1) Windows 对半透明窗口 resize，会先拿旧 buffer **拉伸**显示一帧 —— rail 是不透明
        #    的，那帧被拉长的 rail 就画出来了（新增的 326px 在窗口左侧），用户看到的就是
        #    "界面左侧有东西一闪而过"；
        # 2) 那次 resize 的重排还排在动画定时器前面，把头几帧挤掉（实测首帧 46ms 已推进
        #    31%），面板"愣住后突然出现在半路"。
        # 窗口尺寸不变，这两个毛病都没了。透明区不挡鼠标靠 mask（见 `_settle`）。
        right = g.x() + g.width() - self.EDGE_GAP
        geo = QtCore.QRect(int(right - W), int(y), W, h)
        if self.geometry() != geo:
            self.setGeometry(geo)                # 只有竖直拖动/换屏/拖宽度才会走到这

        x_hidden = x0 + self._pan_w + self.RAIL_W   # panel 藏起来时的窗口内 x
        self.rail.setGeometry(x0 + self._pan_w + 6, 0, self.RAIL_W, h)
        if not animate:
            self.panel.move(x0 if self.expanded else x_hidden, 0)
            self.panel.setVisible(self.expanded)
            self._settle()                       # 抽屉位置/mask 由 _settle 统一摆
            return
        # 起点用**当前**位置而不是写死的端点：连点细边时（上一个动画还没跑完就反向），
        # 从当前位置接着走才不会跳一下。
        if self.expanded:
            self.clearMask()                     # 动画期间整窗可画（mask 会把 panel 裁掉）
            self.panel.setVisible(True)
            # 曲线选 OutQuint：抽屉抽出要的是"冲出来、尾巴收住"。
            _anim(self.panel, b"pos", self.panel.pos(), QtCore.QPoint(x0, 0),
                  DUR["base"], curve=QtCore.QEasingCurve.Type.OutQuint, done=self._settle)
        else:
            _anim(self.panel, b"pos", self.panel.pos(), QtCore.QPoint(x_hidden, 0),
                  DUR["base"], curve=QtCore.QEasingCurve.Type.InCubic, done=self._settle)

    def _settle(self):
        """把窗口和子控件摆成当前状态的"静止样子"（动画结束时也走这里）。

        窗口是固定宽度的一块（记词板宽 + 主面板宽 + 缝 + 细边），收起时大部分是透明的：
        不设 mask 的话，鼠标点到那块透明区域点到的是这个窗口而不是背后的应用（透明窗口
        照样吃点击）。所以可点区域要**逐块**裁出来：细边永远算，主面板展开才算，记词板
        按 `_wb_reveal`（掀开到哪）算。
        """
        x0 = self.WB_W
        x_rail = x0 + self._pan_w + 6
        self.rail.setGeometry(x_rail, 0, self.RAIL_W, self.PANEL_H)
        self.panel.setVisible(self.expanded)
        self.panel.move(x0 if self.expanded else x0 + self._pan_w + self.RAIL_W, 0)
        self.wbpanel.move(0, 0)                 # 抽屉位置恒定，露多少由 mask 决定
        self.wbpanel.setVisible(self.wb_open)
        if hasattr(self, "_wb_fold"):
            self._wb_fold.hide()
            self._wb_fold.set_progress(1.0 if self.wb_open else 0.0)
        self._wb_reveal = float(self.WB_W if self.wb_open else 0)
        self._clip_wordbook()
        self._apply_mask()
        # 抽屉永远压在面板之下（setVisible 之后 Qt 会重新排 z 序，所以这里每次都要重申）
        self.panel.raise_()
        self.rail.raise_()
        self.grip.setGeometry(0, 0, 6, self.PANEL_H)
        self.grip.raise_()
        self.raise_()

    def _apply_mask(self):
        """可点/可画区域 = 细边 ∪ 主面板（展开时）∪ 记词板占的那块（开着时）。

        **只按稳态算**（开/关两个状态各一次），动画期间不再逐帧改窗口 mask：逐帧
        `SetWindowRgn` 在分层窗口上会露出没重画的旧像素（见 `_clip_wordbook` 的注释）。
        "露出多少"由 `_clip_wordbook()` 裁子控件负责。
        """
        x0 = self.WB_W
        reg = QtGui.QRegion(x0 + self._pan_w + 6, 0, self.RAIL_W, self.PANEL_H)
        if self.expanded:
            reg = reg.united(QtGui.QRegion(x0, 0, self._pan_w, self.PANEL_H))
        if self.wb_open:
            reg = reg.united(QtGui.QRegion(0, 0, x0, self.PANEL_H))
        self.setMask(reg)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.raise_()

    def anchor(self):
        """卡片该出现在哪儿：dock 左边、别被 dock 盖住。

        `Card.show_at` 是按 `at.x() - 卡片宽/3` 摆的，所以这里把那个偏移算进去，
        让卡片右边缘正好落在 dock 左边 12px 处。卡片宽度是可拖的，得按配置里的实际
        宽度算 —— 写死 430 的话，卡片被拖窄/拖宽之后会偏。
        """
        w = int(self.cfg.get("card_width") or 0) or 430
        left = self.geometry().left() + self.WB_W      # 主面板的左边缘（不是窗口的）
        if self.expanded and self.wb_open:
            left = self.geometry().left()              # 记词板也开着时避让它
        return QtCore.QPoint(left - 12 - w + w // 3,
                             self.geometry().top() + 46)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if obj is self.grip:
            if t == QtCore.QEvent.Type.MouseButtonPress and ev.button() == QtCore.Qt.MouseButton.LeftButton:
                self._grip_drag = True
                return True
            if t == QtCore.QEvent.Type.MouseMove and self._grip_drag:
                scr = self._screen()
                g = scr.availableGeometry() if scr else QtCore.QRect(0, 0, 0, 0)
                # 抓条贴着的就是面板左边缘：把"屏幕右边缘到鼠标"这段减掉细边和缝，就是面板宽度。
                w = int(g.x() + g.width() - self.EDGE_GAP - ev.globalPosition().toPoint().x()) \
                    - self.RAIL_W - 6
                w = min(max(self.PANEL_W_MIN, w), self.PANEL_W_MAX)
                if w != self._pan_w:
                    self._pan_w = w
                    self.panel.setFixedWidth(w)
                    self._place(animate=False)
                return True
            if t == QtCore.QEvent.Type.MouseButtonRelease and self._grip_drag:
                self._grip_drag = None
                self._save()
                return True
        if obj is self.rail:
            if t == QtCore.QEvent.Type.MouseButtonPress and ev.button() == QtCore.Qt.MouseButton.LeftButton:
                self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
                self._moved = False
                return True
            if t == QtCore.QEvent.Type.MouseMove and self._drag is not None:
                scr = self._screen()
                g = scr.availableGeometry() if scr else QtCore.QRect(0, 0, 0, 0)
                y = ev.globalPosition().toPoint().y() - self._drag.y()
                y = min(max(g.y() + self.EDGE_GAP, y),
                        max(g.y() + self.EDGE_GAP, g.y() + g.height() - self.height() - self.EDGE_GAP))
                self._moved = True
                self._y = y
                self.move(self.x(), int(y))
                return True
            if t == QtCore.QEvent.Type.MouseButtonRelease and self._drag is not None:
                moved, self._drag = self._moved, None
                if moved:
                    self._save()            # 拖过 = 挪位置
                else:
                    self.toggle()           # 没拖 = 点了一下
                return True
        return super().eventFilter(obj, ev)

    def _save(self):
        from . import config       # 延迟导入，免得 gui 和 config 互相缠
        d = self.cfg.setdefault("dock", {})
        d["expanded"] = self.expanded
        d["wb"] = bool(self.wb_open)
        d["panel_w"] = int(self._pan_w)
        d["screen"] = self._screen_name or None     # 空 = 主屏；只有显式挑过才记名字
        if self._y is not None:
            d["y"] = int(self._y)
        try:
            config.save(self.cfg)
        except Exception:
            traceback.print_exc()      # 存不上不影响使用（日志在 data\snapword.log）
            return False
        return True


class Settings(QtWidgets.QDialog):
    def __init__(self, cfg, parent=None, dock=None):
        super().__init__(parent)
        self.cfg = cfg
        # 注意：self.dock 这个名字在本类里已经被"挂一个常驻面板"那个勾选框占了（apply() 用），
        # 常驻面板本体放这儿，定位时要问它在哪块屏。
        self.dock_panel = dock
        self.setWindowTitle("SnapWord 设置 · v%s" % __version__)
        self.setMinimumWidth(460)
        self.setStyleSheet(QSS)
        # 表单加长之后（接 DeepSeek 的引导块进来后实测 870px）可能比屏幕还高，
        # 矮屏幕上会有一截点不到「保存」。装进滚动区，并把高度夹在可用屏高的 90%。
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._wrap = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(self._wrap)
        f.setSpacing(12)            # 同上：回到令牌刻度
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._wrap)
        self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self._scroll)
        scr_h = QtGui.QGuiApplication.primaryScreen().availableGeometry().height()
        self.setMaximumHeight(max(560, int(scr_h * 0.9)))
        # 能自己缩小：一打开就顶到屏高的 90% 的话，矮屏上永远"刚好装下"，
        # 滚动条永远不出现（用户看到的就是"设置不能滚"）。松开尺寸限制 + 右下角抓手，
        # 想缩就缩，一缩内容就真的能滚了。
        self.setSizeGripEnabled(True)
        self._wheel_kids = []       # 滚轮要转给滚动区的子控件（下拉框会吃掉滚轮）
        p = cfg["providers"]

        self.hotkey = SmoothLineEdit(cfg["hotkey"])
        self.engine = SmoothComboBox()
        self.engine.addItems(["auto", "system", "native"])
        self.engine.setCurrentText(cfg.get("ocr_engine", "auto"))
        self.url = SmoothLineEdit(p["deepseek"]["url"])
        self.model = SmoothLineEdit(p["deepseek"]["model"])
        self.key = SmoothLineEdit(p["deepseek"]["key"])
        self.key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.ollama = SmoothLineEdit(p["ollama"]["model"])
        bd = p.get("baidu") or {}
        self.bd_appid = SmoothLineEdit(bd.get("appid", ""))
        self.bd_key = SmoothLineEdit(bd.get("key", ""))
        self.bd_key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.auto_detail = QtWidgets.QCheckBox("出结果后自动生成详细解释（会多花 token）")
        self.auto_detail.setChecked(bool(cfg.get("auto_detail")))
        # 减少动效（Apple HIG / WCAG 2.3.3）：开了以后所有动画一帧落终点，只留颜色变化
        self.reduce_motion = QtWidgets.QCheckBox("减少动效（所有动画直接到位，不再滑动/淡入）")
        self.reduce_motion.setChecked(bool(cfg.get("reduce_motion")))
        self.dock = QtWidgets.QCheckBox("挂一个常驻面板在屏幕右边缘（收起时只有一条细边，不用热键）")
        self.dock.setChecked(bool((cfg.get("dock") or {}).get("enabled", True)))
        # 开机启动：状态以注册表为准（HKCU\...\Run 里那条命令在不在），不在 config 里存一份，
        # 免得两边不一致。winput.autostart_sync() 会在启动时把挪过目录的旧路径改写掉。
        self.autostart = QtWidgets.QCheckBox("开机自动启动（登录后就在屏幕右边常驻，不弹窗）")
        try:
            self.autostart.setChecked(winput.autostart_enabled())
        except OSError:
            self.autostart.setEnabled(False)
            self.autostart.setText(self.autostart.text() + "（这台机器上写不了注册表）")
        # 挂在哪块屏：默认主屏（"" = 主屏）。以前默认"鼠标所在那块屏"，鼠标在副屏时
        # 面板就跑到副屏去了 —— 用户报的"侧边栏跑我副屏上面去了"。
        primary = QtWidgets.QApplication.primaryScreen()
        cur = (cfg.get("dock") or {}).get("screen") or ""
        self.dock_screen = SmoothComboBox()
        for i, s in enumerate(QtWidgets.QApplication.screens()):
            name = "" if s is primary else s.name()
            g = s.geometry()
            self.dock_screen.addItem("%s（%d×%d）" % (s.name(), g.width(), g.height()), name)
            if name == cur:
                self.dock_screen.setCurrentIndex(i)
        if dock is not None:            # 换屏要立刻看见效果，不等"保存"
            self.dock_screen.currentIndexChanged.connect(
                lambda _i: dock.set_screen(self.dock_screen.currentData() or ""))
        # 两个下拉框有焦点时会吃掉滚轮（见 eventFilter）：盯住它们，滚轮一律转给滚动区
        for cb in (self.engine, self.dock_screen):
            cb.installEventFilter(self)
            self._wheel_kids.append(cb)

        # 分组：12 行平铺时用户只能一行行扫，不知道哪些是常用的、哪些是填一次就不用管的。
        # 按"什么时候会来改"分成四组，组内顺序不动。
        f.addRow(self._sec("基本"))
        f.addRow("取词热键", self.hotkey)
        f.addRow("", self.dock)
        f.addRow("面板挂在哪块屏", self.dock_screen)
        f.addRow("", self.autostart)

        f.addRow(self._sec("屏幕识别"))
        f.addRow("OCR 引擎", self.engine)
        f.addRow("百度 appid", self.bd_appid)
        f.addRow("百度密钥", self.bd_key)

        f.addRow(self._sec("问答模型"))
        f.addRow("本地模型", self.ollama)
        f.addRow("DeepSeek 地址", self.url)
        f.addRow("DeepSeek 模型", self.model)
        f.addRow("DeepSeek Key", self.key)
        f.addRow(self._ds_guide())      # 接 DS 的路径得写在手边，不能让用户去猜

        f.addRow(self._sec("进阶"))
        f.addRow("", self.auto_detail)
        f.addRow("", self.reduce_motion)
        hint = QtWidgets.QLabel(
            "释义以离线词典 ECDICT 和有道为准，查不到再用百度翻译（要在 fanyi-api.baidu.com "
            "免费领 appid 和密钥）。\n"
            "问答默认交给本地 9B（免费、不花 token）；本地没在跑时会自动改用 DeepSeek。"
            "按住 Shift 点「问 AI」就是强制走 DeepSeek。")
        hint.setObjectName("note")
        hint.setWordWrap(True)
        f.addRow("", hint)

        # 不用 StandardButton：那两个按钮是系统样式画法，状态切换仍然是一帧硬切。
        # 换成自绘的会过渡按钮，但保留 Accept/Reject 角色（键盘 Esc/Enter 行为不变）。
        bb = QtWidgets.QDialogButtonBox()      # 空盒子，只承载 Accept/Reject 角色（键盘 Esc/Enter 不变）
        ok = SmoothButton("保存", "primary")      # 保存是主操作，和卡片主按钮同一套视觉
        cancel = SmoothButton("取消", "normal")
        bb.addButton(ok, QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(cancel, QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        # 按钮盒挂在滚动区**外面**当固定页脚：装在里面的话，内容一长就得先滚到底才能
        # 看到「保存」（矮屏上更是永远在屏幕外）。现在滚动的是表单，按钮一直贴在底部。
        foot = QtWidgets.QWidget()
        fv = QtWidgets.QHBoxLayout(foot)
        fv.setContentsMargins(12, 8, 12, 12)
        fv.addWidget(bb)
        outer.addWidget(foot)

    # ---------- 设置窗自己的交互修补 ----------
    def eventFilter(self, obj, ev):
        """滚轮落在下拉框上时转给滚动区。

        QComboBox 一旦有焦点就自己吃掉滚轮去改选项 —— 用户在设置里滚页面，
        结果把「OCR 引擎」从 auto 滚成了 native。转给滚动区视口，滚动语义就正常了。
        """
        if ev.type() == QtCore.QEvent.Type.Wheel and obj in self._wheel_kids:
            QtWidgets.QApplication.sendEvent(self._scroll.viewport(), ev)
            return True
        return super().eventFilter(obj, ev)

    @staticmethod
    def _sec(title):
        """分组小标题。用"轻量标题 + 留白"分组，不再加一层 QGroupBox 边框 —— 那会在
        近黑底上切出一片片灰框，反而更重。"""
        lb = QtWidgets.QLabel(title)
        lb.setObjectName("sechead")
        return lb

    # ---------- DeepSeek 接入引导 ----------
    def _ds_guide(self):
        """「DeepSeek 怎么接」这件事得在设置窗里讲清楚，不能让用户自己猜。

        三件事按这个顺序说：①**不接也能用**（本地 9B 免费，这是产品的默认路径）；
        ②想接的话，Key 从哪里来、填哪儿；③填完到底成没成（给一个"测一下"，
        把"我填完了"变成一个确定答案）。
        """
        box = QtWidgets.QFrame(objectName="tip")
        v = QtWidgets.QVBoxLayout(box)
        # 内边距交给 #tip 的 QSS（8px 12px）：再设一遍布局 margins 就变成双重内边距，
        # 而且 10 也不在布局间距刻度上
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)
        title = QtWidgets.QLabel("接 DeepSeek（可选）", objectName="tiphead")
        self.lb_ds_state = QtWidgets.QLabel("", objectName="tiphead")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.lb_ds_state)
        v.addLayout(head)

        self.lb_ds_free = QtWidgets.QLabel(
            "不接也能用：问答默认走本地 9B（免费、不花 token），本地没在跑时才轮到 DeepSeek。",
            objectName="tipline")
        self.lb_ds_free.setWordWrap(True)
        v.addWidget(self.lb_ds_free)

        # 没 Key 时才展开这三步；填了 Key 就收起来，别一直占着地方
        self.ds_steps = QtWidgets.QWidget()
        sv = QtWidgets.QVBoxLayout(self.ds_steps)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(4)        # 三步之间挨紧一点，但 2 不在刻度上
        for i, line in enumerate([
            "① 打开 platform.deepseek.com，注册后在 API Keys 里创建一个 Key",
            "② 复制那串 sk- 开头的 Key",
            "③ 粘到上面的「DeepSeek Key」，点保存",
        ], 1):
            lb = QtWidgets.QLabel(line, objectName="tipline")
            lb.setWordWrap(True)
            sv.addWidget(lb)
        v.addWidget(self.ds_steps)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.btn_ds_open = SmoothButton("打开申请页", "ghost")
        self.btn_ds_open.setToolTip("用默认浏览器打开 platform.deepseek.com 的 API Keys 页")
        self.btn_ds_test = SmoothButton("测一下能不能通", "normal")
        self.btn_ds_test.setToolTip("发一个只花 1 个 token 的请求，确认地址和 Key 是不是真的能用")
        row.addWidget(self.btn_ds_open)
        row.addWidget(self.btn_ds_test)
        row.addStretch(1)
        v.addLayout(row)

        self.btn_ds_open.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl("https://platform.deepseek.com/api_keys")))
        self.btn_ds_test.clicked.connect(self._ping_ds)
        self.key.textChanged.connect(self._sync_ds)
        self._ping_job = None
        self._sync_ds()
        return box

    def _sync_ds(self):
        """Key 有没有填 → 决定显示"三步引导"还是"可以测了"。"""
        has = bool(self.key.text().strip())
        self.ds_steps.setVisible(not has)
        self.btn_ds_test.setEnabled(has)
        self.btn_ds_open.setVisible(not has)
        self._set_state("还没接：照下面三步走一遍" if not has else "已填 Key，点「测一下」确认", None)

    def _set_state(self, text, ok):
        """状态文字：None=中性、True=通、False=不通（绿/红是语义色，只在真有结论时才用）。

        有结论（通/不通）时淡入——等了几秒的请求终于回来，结果值得一个柔和的到场，
        而不是在满屏设置项里"啪"地变色。"""
        self.lb_ds_state.setText(text)
        self.lb_ds_state.setObjectName("tiphead" if ok is None else ("dsok" if ok else "dsbad"))
        self.lb_ds_state.style().unpolish(self.lb_ds_state)
        self.lb_ds_state.style().polish(self.lb_ds_state)
        if ok is not None:
            _fade_in(self.lb_ds_state, _motion("standard", "fast")[0])

    def _ping_ds(self):
        """真的发一个最小请求去验 Key —— 别让用户凭"保存成功"以为接好了。"""
        url, key = self.url.text().strip(), self.key.text().strip()
        if not key:
            return
        self.btn_ds_test.setEnabled(False)
        self._set_state("正在测…", None)
        from . import providers      # 延迟导入，免得 gui 和 providers 互相缠

        def done(res):
            ok, msg = res
            try:                     # 对话框可能已经关了，控件没了
                self.btn_ds_test.setEnabled(True)
                self._set_state(("连上了：%s" % msg) if ok else ("连不上：%s" % msg), ok)
            except RuntimeError:
                pass

        def fail(msg):
            try:
                self.btn_ds_test.setEnabled(True)
                self._set_state("测不了：%s" % msg, False)
            except RuntimeError:
                pass

        j = Job(providers.deepseek_up, url, key, self.model.text().strip())
        j.done.connect(done)
        j.fail.connect(fail)
        self._ping_job = j           # 留引用：Qt 里被 GC 掉就等于线程没了
        j.start()

    def showEvent(self, ev):
        super().showEvent(ev)
        if not getattr(self, "_fit", False):
            # 装了滚动区之后，对话框不会自己按内容撑开（QScrollArea 的 sizeHint 不等于内容高），
            # 首次显示时按内容量一次：够高就全显示，不够就交给滚动。
            # **但不许顶到 maximumHeight**：顶满的话矮屏上永远"刚好装下"，滚动条永远不出现
            # （用户看到的就是"设置不能滚"）。夹到可用屏高的 80%，想更大自己拖。
            self._fit = True
            # 挂到**常驻面板所在那块屏**上（面板挂哪块屏是设置里的选项），并且居中。
            # 原来什么定位都不做，交给 Windows 摆：实测它摆到 y=357，而窗子高 921，
            # 于是尾巴垂到屏幕外 126px —— 之后拖上/下边，系统一边重排一边缩，手感就是
            # 用户报的"极其灵敏、上下两边同时动"。居中夹进可用区就没了。
            panel = getattr(self, "dock_panel", None)
            screen = None
            if panel is not None and hasattr(panel, "_screen"):
                screen = panel._screen()
            screen = screen or self.screen() or QtWidgets.QApplication.primaryScreen()
            g = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1920, 1080)
            self.setMaximumHeight(max(560, int(g.height() * 0.9)))   # 上限按目标屏重算
            hint = self._wrap.sizeHint()
            w = max(self.minimumWidth(), hint.width() + 20)
            h = min(hint.height() + 20, int(g.height() * 0.8), self.maximumHeight())
            self.resize(w, h)
            # move() 摆的是**窗框**（原生标题栏在上面，实测 30px），而 geometry() 是客户区，
            # 所以居中/夹取都得用窗框高，不然窗子会往下多探出标题栏那条。
            fh = h + max(0, self.frameGeometry().height() - self.height())
            self.move(min(max(g.left(), g.center().x() - w // 2), g.right() - w + 1),
                      min(max(g.top(), g.center().y() - fh // 2), g.bottom() - fh + 1))
        self.setWindowOpacity(0.0)
        _anim(self, b"windowOpacity", 0.0, 1.0, DUR["fast"])

    def done(self, r):
        # 关闭也淡出（docs/UI-STYLE.md 动效表欠的"出场"）：保存/取消/Esc 全都汇经这里。
        # 闸门防重入 —— 动画跑完回调里会再进一次 done，那时直接走 super 收掉，不会二连动画。
        if getattr(self, "_closing", False) or not self.isVisible():
            super().done(r)
            return
        self._closing = True
        _anim(self, b"windowOpacity", self.windowOpacity(), 0.0, DUR["fast"],
              curve=QtCore.QEasingCurve.Type.InCubic, done=lambda: self.done(r))

    def apply(self):
        c = self.cfg
        c["hotkey"] = self.hotkey.text().strip() or c["hotkey"]
        c["ocr_engine"] = self.engine.currentText()
        p = c["providers"]
        p["ollama"]["model"] = self.ollama.text().strip()
        p["deepseek"]["url"] = self.url.text().strip()
        p["deepseek"]["model"] = self.model.text().strip()
        p["deepseek"]["key"] = self.key.text().strip()
        p["deepseek"]["enabled"] = bool(p["deepseek"]["key"])
        b = p.setdefault("baidu", {})
        b["appid"] = self.bd_appid.text().strip()
        b["key"] = self.bd_key.text().strip()
        c["auto_detail"] = self.auto_detail.isChecked()
        c["reduce_motion"] = self.reduce_motion.isChecked()
        REDUCE["on"] = self.reduce_motion.isChecked()      # 立即生效，不用重启
        d = c.setdefault("dock", {})
        d["enabled"] = self.dock.isChecked()
        d["screen"] = self.dock_screen.currentData() or None
        try:
            winput.autostart_set(self.autostart.isChecked())
        except OSError:
            traceback.print_exc()       # 写不了注册表也别把"保存"整个搞失败
        return c


class App(QtCore.QObject):
    """把热键、遮罩、卡片、后台任务串起来。"""

    # 热键是在 winput.HotkeyThread 那个线程里收到的，而那里面**绝对不能碰 Qt 控件**
    # （跨线程建窗口是未定义行为，实测会直接把进程搞崩）。信号跨线程是排队投递的，
    # 所以热键线程只负责 emit，真正的活排回主线程干。
    hotkey_fired = QtCore.Signal(object)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        REDUCE["on"] = bool(cfg.get("reduce_motion"))   # 启动时把"减少动效"接进来
        self.brief_cache = {}
        self.cards = []
        self.current = None
        self.selector = None
        self._jobs = []
        self._chat_hist = []        # 当前这个词的追问上下文（换词就清空）
        self._ask_started = False
        self._esc_thread = None
        self.lookup = None          # GUI 起来之后再建（要读词典文件）
        self.dock = None            # 屏幕右边缘那条常驻面板（run() 里建）
        self.wb = None              # 记词板（run() 里建；没建起来也不影响查词）
        self.hotkey_fired.connect(self.on_hotkey)

    # ---------- 取词入口 ----------
    def start_pick(self):
        if self.selector:
            return
        if self.dock:
            self.dock.hide()        # 遮罩是全屏的，别让它压着面板
        pos = QtGui.QCursor.pos()
        scr = QtWidgets.QApplication.screenAt(pos) or QtWidgets.QApplication.primaryScreen()
        pm = scr.grabWindow(0)
        self.selector = Selector(pm, scr.geometry(), pm.devicePixelRatio() or 1.0)
        self.selector.picked.connect(self._on_picked)
        self.selector.cancelled.connect(self._on_cancel)
        self.selector.show()
        # 遮罩淡入（iOS 的全屏覆盖都是淡出来的，不是闪出来的）——只动 windowOpacity，
        # 不碰几何，安全（QGraphicsOpacityEffect 那类残影坑不涉及窗口级透明度）
        self.selector.setWindowOpacity(0.0)
        _anim(self.selector, b"windowOpacity", 0.0, 1.0, _motion("standard", "fast")[0],
              curve=EASE["standard"])
        self.selector.raise_()
        self.selector.activateWindow()

    def _on_cancel(self):
        if self.selector:
            self.selector.close()
            self.selector = None
        if self.dock:
            self.dock.show()        # WA_ShowWithoutActivating，不会把焦点抢回来

    def _on_picked(self, rect, pixmap):
        self._on_cancel()
        at = QtGui.QCursor.pos()
        self._last_at = at
        self._ocr_info = None
        card = self._ensure_card(pos=at)
        card.show_brief(
            {"query": "识别中…", "cn": [], "kind": "word", "source": "…"}, at=at)
        png = pixmap_png(pixmap)
        self._run(self._ocr_then_lookup, png, card=card)

    def lookup_text(self, text, at=None):
        text = (text or "").strip()
        if not text:
            return
        self._ocr_info = None
        card = self._ensure_card(pos=at or QtGui.QCursor.pos())
        self._run(self._lookup_brief, text, at, card=card)

    def on_hotkey(self, name):
        if name is None:
            self._notify("热键都被别的软件占了，先用托盘右键菜单里的「查屏幕上的词」。")
            return
        if name == "esc":
            if self.current:
                self.current.hide_card()
            return
        if name == "pick":
            self.start_pick()
        elif name == "selection":
            self._from_selection()

    def _from_selection(self):
        """模拟 Ctrl+C 拿划词。控制台里 Ctrl+C 会打断进程，所以先看前台窗口类名。"""
        if not winput.copy_is_safe():
            self.start_pick()
            return
        cb = QtWidgets.QApplication.clipboard()
        before = winput.clipboard_seq()
        winput.wait_modifiers_released()
        winput.send_ctrl_c()
        text = ""
        for _ in range(15):
            QtCore.QThread.msleep(30)
            if winput.clipboard_seq() != before:
                text = cb.text()
                break
        if text.strip():
            self.lookup_text(text.strip(), at=QtGui.QCursor.pos())
        else:
            self.start_pick()

    # ---------- 后台任务 ----------
    def _run(self, fn, *args, card=None, **kw):
        """card=：这个任务的结果回哪张卡片；不传就等于"当前那张"（老行为）。

        为什么必须能传：结果一律投给 `self.current` 的话，钉住旧卡再点「详细解释」，
        转圈和答案会跑到最新那张卡上，旧卡毫无反应（hy4 评审第 3 条）。
        """
        j = Job(fn, *args, stream=bool(kw.pop("stream", False)), **kw)
        j.done.connect(lambda r, f=fn, c=card: self._on_job_done(f, r, c))
        j.fail.connect(lambda e, f=fn, c=card: self._on_job_fail(f, e, c))
        j.progress.connect(lambda p, f=fn, c=card: self._on_job_progress(f, p, c))
        j.finished.connect(lambda j=j: self._jobs.remove(j) if j in self._jobs else None)
        self._jobs.append(j)
        j.start()

    def _on_job_progress(self, fn, piece, card=None):
        """流式回答：先来 ("src", 谁在答)，之后是一段段 ("text", 正文)。"""
        card = card if card is not None else self.current
        if getattr(fn, "__name__", "") != "_ask_stream" or not card:
            return
        kind, val = piece
        if kind == "src":
            self._ask_started = True
            who = "AI（DeepSeek）" if val == "deepseek" else "AI（本地 9B）"
            card.chat_begin(who)
        elif kind == "text":
            card.chat_push(str(val))

    def _on_job_done(self, fn, result, card=None):
        name = getattr(fn, "__name__", "")
        card = card if card is not None else self.current
        if name == "_ocr_then_lookup":
            r = result
            if not r.get("ok"):
                self._card_msg("识别失败", r.get("error") or "OCR 没读出东西")
                return
            text = (r.get("text") or "").strip()
            if not text:
                self._card_msg("没看到字", "框里没识别出文字，框大一点或者挑清楚的地方。")
                return
            self._ocr_info = {"name": OCR_ENGINE_CN.get(r.get("engine"), r.get("engine")),
                              "ms": r.get("ms")}
            self.brief_cache["_last_text"] = text
            self._run(self._lookup_brief, text, getattr(self, "_last_at", None), card=card)
        elif name == "_lookup_brief":
            brief, text, at = result
            self._show_brief(brief, text, at, card=card)
        elif name == "_lookup_enrich":
            if card:
                card.set_enrich(result)
        elif name == "_lookup_detail":
            md, err = result
            if card:
                card.set_detail(md, err)
        elif name == "_ask_stream":
            # 收尾全在主线程做：流式回答的对话历史不能从 worker 线程改（hy4 评审第 10 条）
            if card:
                self._chat_hist.append({"role": "user", "content": getattr(self, "_ask_q", "")})
                self._chat_hist.append({"role": "assistant", "content": str(result)})
                del self._chat_hist[:-8]
                card.chat_in.setEnabled(True)    # 答完了，放开输入框
                card._resize_keep_place()        # 收尾时按最终高度定一次

    def _on_job_fail(self, fn, err, card=None):
        name = getattr(fn, "__name__", "")
        card = card if card is not None else self.current
        if name == "_ask_stream" and card:
            card.chat_in.setEnabled(True)
            if not getattr(self, "_ask_started", False):
                card.chat_begin("系统")          # 一个字都没来得及吐：先开气泡再说
            card.chat_push("出错了：" + err)
            card._resize_keep_place()
        elif card:
            card.set_detail(None, err)

    # ---------- 这些在后台线程里跑 ----------
    def _ocr_then_lookup(self, png):
        from . import ocr
        # judge 用离线词典给两个引擎的结果打分（查得到才是硬信号），
        # 免得 native 吐半截也当答案 —— 用户报的"只能识别出一个字母"就是这个。
        return ocr.recognize_png(self.cfg["ocr_helper"], png, self.cfg["ocr_engine"],
                                 ocr_dir=self.cfg.get("ocr_dir"),
                                 ocr_dir_strong=self.cfg.get("ocr_dir_strong"),
                                 judge=self.lookup.score_ocr)

    def _lookup_brief(self, text, at):
        b = self.lookup.brief(text)
        info = getattr(self, "_ocr_info", None)
        if info:                    # 从 OCR 来的就标出来，用户好判断认没认对
            b.setdefault("notes", []).append(
                "识别自 %s（%s 毫秒）" % (info.get("name"), info.get("ms")))
        return b, text, at

    def _lookup_enrich(self, brief):
        return self.lookup.enrich(brief)

    def _lookup_detail(self, brief):
        try:
            return self.lookup.detail(brief), None
        except Exception as ex:
            return None, str(ex)

    def _ask_stream(self, brief, q, escalate):
        """后台线程里跑的流式答疑：一个字一个字地 yield 给 Job 转发到界面。

        这里**只读**对话历史（`list(...)` 快照），不写 —— 写回主线程的收尾里做
        （见 `_on_job_done` 的 `_ask_stream` 分支），否则换词的瞬间会和主线程抢同一个列表。
        """
        for kind, val in self.lookup.ask_stream(brief, q, list(self._chat_hist),
                                                escalate=escalate):
            yield (kind, val)

    # ---------- 卡片管理 ----------
    def _ensure_card(self, pos=None):
        if self.current and not self.current.pinned:
            return self.current
        c = Card(width=int(self.cfg.get("card_width") or 0))
        c.closed.connect(self._on_card_closed)
        # 绑定 card：结果回发起那一次交互的卡片，而不是"最新那张"（hy4 评审第 3 条）
        c.detail_requested.connect(lambda b, c=c: self._on_detail(c, b))
        c.ask_requested.connect(lambda b, q, e, c=c: self._on_ask(c, b, q, e))
        c.word_clicked.connect(lambda w: self.lookup_text(w))
        c.save_requested.connect(lambda b, c=c: self._on_save(c, b))
        c.width_changed.connect(self._on_card_width)
        self.cards.append(c)
        self.current = c
        return c

    def _on_save(self, card, brief):
        """卡片上的「存词」：一个按钮两个方向 —— 没存过就存，存过再点就取消。"""
        if self.wb is None or not brief or card is None:
            return
        from . import wordbook
        word = wordbook.key_of(brief)
        if not word:
            return
        if self.wb.has(word):
            self.wb.remove(word)
            card.set_saved(False)
            self._notify("已从记词板删掉：%s" % word, 4000)
        else:
            self.wb.add(brief)
            card.set_saved(True)
            self._notify("已存到记词板：%s" % word, 4000)
        if self.dock:
            self.dock.refresh_wordbook()

    def _on_card_width(self, w):
        """卡片宽度拖完了：记住它（下次开卡片照这个宽）。"""
        try:
            w = int(w)
        except (TypeError, ValueError):
            return
        if w <= 0 or w == self.cfg.get("card_width"):
            return
        self.cfg["card_width"] = w
        try:
            from . import config
            config.save(self.cfg)
        except Exception:
            traceback.print_exc()      # 存不上不影响使用

    def _on_card_closed(self, card):
        if card in self.cards:
            self.cards.remove(card)
        if self.current is card:
            self.current = self.cards[-1] if self.cards else None
        self._sync_esc()

    def _on_detail(self, card, brief):
        if not brief or card is None:
            return
        card.btn_detail.setEnabled(False)
        card.start_detail_wait()            # 按钮上数秒：冷启动十几秒也看得见它在动
        card.detail.show()
        card.detail.setPlainText("正在整理…（本地模型冷启动要十几秒，DeepSeek 快一些）")
        card._resize_keep_place()
        self._run(self._lookup_detail, brief, card=card)

    def _on_ask(self, card, brief, q, escalate):
        # 谁来答由 lookup.ask_plan 决定：本地 9B 在跑就用它，没在跑就自动上网（填了 key 的话）。
        if card is None:
            return
        self._ask_q = q                     # 收尾时补进对话历史（_on_job_done）
        self._ask_started = False
        card.chat_in.setEnabled(False)      # 防重入：回答没回来前别再发第二条（评审第 7 条）
        self._run(self._ask_stream, brief, q, escalate, stream=True, card=card)

    def _show_brief(self, brief, text, at, card=None):
        card = card if card is not None else self.current
        if card is None:
            # 卡片已经被关了，结果没地方投 —— 以前这里会 AttributeError，
            # 结果被静默丢掉（hy4 评审第 2 条）。
            return
        self._chat_hist = []        # 换词了，之前的追问上下文作废
        card.show_brief(brief, text=text, at=at)
        if self.wb is not None:
            from . import wordbook
            card.set_saved(self.wb.has(wordbook.key_of(brief)))     # 存过的词，按钮直接亮着
        card.btn_detail.setEnabled(True)
        if self.cfg.get("auto_detail") and (brief.get("cn") or brief.get("en")):
            self._on_detail(card, brief)
        else:
            self._run(self._lookup_enrich, brief, card=card)
        self._sync_esc()

    def _card_msg(self, title, msg):
        self._ensure_card().show_brief(
            {"query": title, "cn": [msg], "kind": "word", "source": "提示"}, at=QtGui.QCursor.pos())

    def _notify(self, msg, ms=8000):
        """用托盘气泡，不要弹模态框（模态框会抢焦点，违反第一条硬要求）。"""
        tray = getattr(self, "tray", None)
        if tray:
            tray.showMessage("SnapWord", msg, QtWidgets.QSystemTrayIcon.MessageIcon.Information, ms)
        else:
            print("SnapWord:", msg)

    # ---------- Esc：只在卡片开着的时候才抢全局 Esc ----------
    def _sync_esc(self):
        want = bool(self.cards)
        if want and not self._esc_thread:
            t = winput.HotkeyThread({"esc": "esc"}, self.hotkey_fired.emit)
            t.start()
            self._esc_thread = t
        elif not want and self._esc_thread:
            self._esc_thread.stop()
            self._esc_thread = None


def run(cfg):
    from .cache import Cache
    from .ecdict import Ecdict
    from .lookup import Lookup

    log = install_crash_log(os.path.dirname(cfg["cache"]))
    log.write("\n=== SnapWord 启动 %s (pid %d) ===\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
    try:    # 开着开机启动但路径变了（挪过目录、换过 venv）就改写，不然它会静默失效
        if winput.autostart_sync():
            log.write("开机启动的路径变了，已改写为 %s\n" % winput.autostart_command())
    except OSError:
        traceback.print_exc()

    QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # 托盘常驻程序：**任何窗口关掉都不许连坐退出整个进程**。默认是"最后一个窗口关了就退出"，
    # 那样关掉卡片、或者收起面板，整个 SnapWord 就跟着没了（连托盘图标一起）。
    app.setQuitOnLastWindowClosed(False)

    lookup = Lookup(cfg, dic=Ecdict(cfg["ecdict"]), cache=Cache(cfg["cache"]))
    ctrl = App(cfg)
    ctrl.lookup = lookup
    # 记词板（存下来的词）。坏掉也不该拖垮整个程序 —— 查词是主功能，本子是附加的。
    try:
        from .wordbook import Wordbook
        ctrl.wb = Wordbook(cfg.get("wordbook") or "")
    except Exception:
        traceback.print_exc()
        ctrl.wb = None

    def on_settings():
        old_screen = (cfg.get("dock") or {}).get("screen") or ""
        d = Settings(cfg, dock=dock)
        if d.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            new = d.apply()
            from . import config
            config.save(new)
            if dock:
                dock.set_enabled(bool((new.get("dock") or {}).get("enabled", True)))
            QtWidgets.QMessageBox.information(
                None, "SnapWord", "已保存。热键的改动要重启才生效。")
        elif dock:
            # 换屏是"立刻预览"的（见 Settings 里的注释），点取消就把它收回去，
            # 否则取消之后面板留在新屏上、config 也被预览值改掉了（hy4 评审第 9 条）。
            dock.set_screen(old_screen)

    def on_quit():
        try:
            ctrl._esc_thread and ctrl._esc_thread.stop()
        finally:
            # 先把在后台跑的线程收干净再拆进程：QThread 还在跑就把进程结束掉会直接崩
            # （复现过：exit code 0xC0000409，并打印 "QThread: Destroyed while thread is
            # still running"。流式回答/慢 OCR 期间点托盘退出就会踩到）
            deadline = time.monotonic() + 1.5
            for j in list(ctrl._jobs):
                left = int((deadline - time.monotonic()) * 1000)
                if left > 0:
                    j.wait(left)
            QtWidgets.QApplication.quit()

    tray = Tray(ctrl, cfg["hotkey"], on_settings, on_quit)
    tray.show()
    ctrl.tray = tray

    # 屏幕右边缘的常驻面板 —— 这是"不依赖热键"的那条路。
    # 面板**无条件建**：enabled=False 只是不显示它，托盘里的「显示常驻面板」必须一直在，
    # 否则关掉它以后本次会话就再也打不开了（要重启；hy4 评审第 6 条）。
    def on_clip():
        ctrl.lookup_text(QtWidgets.QApplication.clipboard().text(),
                         at=dock.anchor() if dock else None)

    dock = Dock(cfg, ctrl.start_pick,
                lambda t: ctrl.lookup_text(t, at=dock.anchor()),
                on_clip, on_settings, on_quit,
                wb=ctrl.wb,
                on_word=lambda w: ctrl.lookup_text(w, at=dock.anchor()))
    dock.set_hint("热键：%s 框选屏幕。不想记热键就用上面的按钮。" % cfg["hotkey"])
    ctrl.dock = dock
    tray.attach_dock(dock)      # 托盘里能开关面板、换屏、展开收起
    if (cfg.get("dock") or {}).get("enabled", True):
        dock.show()

    def chain(preferred, alts):
        return [preferred] + [a for a in alts if a != preferred]

    hk = winput.HotkeyThread({
        "pick": chain(cfg["hotkey"], ["ctrl+alt+Q", "ctrl+shift+D", "alt+shift+D", "ctrl+alt+F9"]),
        "selection": chain(cfg.get("hotkey_selection", "ctrl+alt+S"), ["ctrl+shift+S", "alt+shift+S"]),
    }, ctrl.hotkey_fired.emit)
    hk.start()
    ctrl.hotkeys = hk

    def after_hotkeys():
        got = hk.registered
        pick = got.get("pick")
        if not got:
            tips = "；".join([x for x in hk.errors][:3]) or "原因不明"
            ctrl._notify("热键一个都没注册上（%s）。屏幕右边那条 SnapWord 细边里，功能一样能点出来。" % tips, 12000)
            if dock:
                dock.set_hint("热键一个都没注册上（%s）。无所谓 —— 上面的按钮和输入框都能干同样的事。"
                              % tips)
            return
        if pick != cfg["hotkey"]:
            ctrl._notify("%s 被别的软件占了，框选热键改成 %s。" % (cfg["hotkey"], pick))
            tray.pick_action.setText("查屏幕上的词（%s）" % pick)
        if "selection" not in got:
            ctrl._notify("%s 没注册上，取划词不可用，用框选就行。"
                         % cfg.get("hotkey_selection", "ctrl+alt+S"))
        tray.setToolTip("SnapWord v%s —— %s 框选查词" % (__version__, pick))
        if dock:
            dock.set_hint("热键：%s 框选屏幕，%s 取划词。不想记热键就用上面的按钮。"
                          % (pick, cfg.get("hotkey_selection", "ctrl+alt+S")))

    QtCore.QTimer.singleShot(600, after_hotkeys)

    # 自检钩子：SNAPWORD_DEMO=serendipity 直接弹一张卡片；
    # SNAPWORD_DEMO=selector 直接进框选遮罩；再给 SNAPWORD_SHOT=路径 就把卡片自身
    # 渲染成 PNG（用 widget.grab()，不用抓屏，所以不会拍到桌面上别的东西）然后退出。
    demo = os.environ.get("SNAPWORD_DEMO")
    if demo:

        def _demo_start():
            if demo == "selector":
                ctrl.start_pick()
            elif demo in ("dock", "dock-expanded"):
                # SNAPWORD_DEMO=dock 看收起的那条细边；dock-expanded 看展开后的面板。
                # 要**强制**成那个状态（config 里存着上次的 expanded，不然两个钩子会反着来）。
                # 展开/收起是 180ms 动画，几何要等它走完才准 —— 打印放在 _shot 里。
                if ctrl.dock and ctrl.dock.expanded != (demo == "dock-expanded"):
                    ctrl.dock.toggle()
            elif demo.startswith("chat"):
                # SNAPWORD_DEMO=chat:serendipity 连对话一起验：真发一问、真流式回答
                # （SNAPWORD_ASK 可以换问题）。截图要等它答完，所以 _shot 会晚一点。
                word = demo.split(":", 1)[1] if ":" in demo else "serendipity"
                ctrl.lookup_text(word)

                def _ask_demo():
                    c = ctrl.current
                    if not c:
                        return
                    c._toggle_chat()
                    c.chat_in.setText(os.environ.get("SNAPWORD_ASK", "它和 luck 有什么区别？"))
                    c._send(False)

                QtCore.QTimer.singleShot(1200, _ask_demo)
            elif demo == "settings":
                # 设置对话框也要能截图：on_settings 用的是 exec()，会停在嵌套事件循环里，
                # 定时器抓不到它；这里非模态 show() 出来，_shot 就能 grab。
                ctrl._dlg_shown = Settings(cfg, dock=dock)
                ctrl._dlg_shown.show()
            elif demo == "wordbook":
                # 记词板的样子：**不碰用户的真记词板**，临时塞一个 scratch 库进去。
                import tempfile
                from .wordbook import Wordbook
                scratch = Wordbook(os.path.join(tempfile.mkdtemp(prefix="snapword-demo-"), "wb.db"))
                for b in ({"query": "serendipity", "phonetic": "/ˌserənˈdɪpəti/",
                           "cn": ["n. 意外发现珍奇事物的本领"], "en": ["n. good luck in making discoveries"]},
                          {"query": "photosynthesis", "phonetic": "/ˌfəʊtəʊˈsɪnθəsɪs/",
                           "cn": ["n. 光合作用"], "en": ["n. synthesis of compounds with radiant energy"]},
                          {"query": "ephemeral", "phonetic": "/ɪˈfem(ə)rəl/",
                           "cn": ["a. 短暂的，转瞬即逝的"], "en": ["a. lasting for a very short time"]}):
                    scratch.add(b)
                scratch.set_mastered("photosynthesis", True)
                if ctrl.dock:
                    ctrl.dock.wb = scratch
                    ctrl.dock.refresh_wordbook()
                    if not ctrl.dock.expanded:
                        ctrl.dock.toggle()
                    if not ctrl.dock.wb_open:
                        ctrl.dock.toggle_wordbook()
            elif demo == "detail":
                # 点过「详细解释」后的卡：验证详解淡入收尾的视觉（不突兀）
                ctrl.lookup_text("serendipity")
                QtCore.QTimer.singleShot(400, lambda: ctrl.current and ctrl.current.set_detail(
                    "**serendipity**  /ˌserənˈdɪpəti/\n\n名词：意外发现珍奇事物的本领；机缘凑巧。\n\n"
                    "例：*A fortunate stroke of serendipity led to the discovery.*\n\n"
                    "词源：Horace Walpole 1774 年杜撰，出自童话《锡兰三王子》(The Three Princes of Serendip)。"))
            elif demo == "pinned":
                ctrl.lookup_text("serendipity")
                QtCore.QTimer.singleShot(400, lambda: ctrl.current and ctrl.current._toggle_pin())
            elif demo == "pills":
                # 多义词卡：底部一整排词胶囊（点击切查那个词），验证胶囊行不突兀
                ctrl.lookup_text("run")
            elif demo.startswith("ocr"):
                # 造一张写着某个词的图，直接喂给"框选完"那一步：
                # 走的是真路径（画图 -> pixmap_png -> ocr-helper.exe -> 查词 -> 卡片）
                # SNAPWORD_DEMO=ocr:quixotic 可以换要识别成什么词
                word = demo.split(":", 1)[1] if ":" in demo else "serendipity"
                pm = QtGui.QPixmap(600, 120)
                pm.fill(QtGui.QColor("#ffffff"))
                p = QtGui.QPainter(pm)
                p.setPen(QtGui.QColor("#000000"))
                p.setFont(QtGui.QFont("Consolas", 40))
                p.drawText(24, 80, word)
                p.end()
                ctrl._on_picked(QtCore.QRect(0, 0, 600, 120), pm)
            else:
                ctrl.lookup_text(demo)

        QtCore.QTimer.singleShot(300, _demo_start)
        shot = os.environ.get("SNAPWORD_SHOT")
        if shot:
            def _shot():
                w = ctrl.dock if (demo.startswith("dock") or demo == "wordbook") else (
                    ctrl.selector or getattr(ctrl, "_dlg_shown", None) or ctrl.current)
                if w and ctrl.dock:
                    # 几何只看这里：展开/收起是 180ms 动画，_demo_start 里那会儿还没走完。
                    sc = ctrl.dock._screen()
                    g = sc.geometry()
                    dg = ctrl.dock.geometry()
                    print("dock: geo=%s 右边缘=%d 屏右边缘=%d 贴边=%s 屏=%s 展开=%s"
                          % (dg, dg.x() + dg.width(), g.x() + g.width(),
                             dg.x() + dg.width() == g.x() + g.width(), sc.name(), ctrl.dock.expanded))
                if w:
                    w.grab().save(shot)
                    print("shot -> %s %dx%d px" % (shot, w.width(), w.height()))
                app.quit()

            QtCore.QTimer.singleShot(14000 if demo.startswith("chat") else 5000, _shot)

    if not lookup.dic.available:
        tray.showMessage("SnapWord", "还没建离线词典：跑一下 tools\\fetch_dict.py（走 npm 镜像下 182 MB 词库）。",
                         QtWidgets.QSystemTrayIcon.MessageIcon.Warning, 8000)
    rc = app.exec()
    # 兜底：退出时还有线程没停下来（流式回答卡在网络上、OCR 卡在 helper 上）。on_quit 已经
    # 等过 1.5 秒，这里对剩下的**强杀**——让 Qt 去析构一个还在跑的 QThread 会直接 abort
    # （复现过 exit 0xC0000409）。进程都要没了，强杀比崩溃体面。
    for j in list(ctrl._jobs):
        if j.isRunning():
            j.terminate()
            j.wait(500)
    return rc


if __name__ == "__main__":
    from . import config

    sys.exit(run(config.load()))
