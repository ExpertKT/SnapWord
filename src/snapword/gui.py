"""悬浮查词卡片（PySide6）。

三条硬要求怎么落实的：
  · 不抢焦点 —— WA_ShowWithoutActivating + Qt.Tool，show() 不调用 activateWindow()；
    只有用户自己点进输入框，键盘才归卡片。
  · 快 —— 热键按下先把整屏抓下来（快路径先出词典结果，联网/LLM 都在后台线程）。
  · 省 token —— 结果按词进 SQLite 缓存，DeepSeek 只在你要"详细解释"或"转 DeepSeek"时才发。
"""
import html
import io
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


def _anim(obj, prop, start, end, ms=180, curve=None, done=None):
    """给 obj 的 prop 做一次属性动画。

    **必须留引用**：PySide6 里动画对象一旦被 GC，属性就停在原地不动（看起来像"没动画"）。
    """
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


def _fade_in(w, ms=150):
    """控件淡入（透明度 0→1）。给"异步冒出来的小块"用：有道对照行、多词 chips、设置窗。

    用完必须把 effect 摘掉（`setGraphicsEffect(None)`）—— 挂着 `QGraphicsOpacityEffect`
    的控件以后走软件渲染，白拖性能。用完就摘也就不会和 `_shadow()` 抢同一个 effect。
    """
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

# ---- 视觉规范见 docs/UI-STYLE.md：令牌只在这里定义一次，QSS 里别随手写颜色 ----
T = {
    "canvas": "#07080a", "surface": "#0d0d0d", "elevated": "#101111", "card": "#121212",
    "hairline": "#242728", "hairline_strong": "#33373d",
    "ink": "#f4f4f6", "body": "#cdcdcd", "mute": "#9c9c9d", "ash": "#6a6b6c",
    "blue": "#57c1ff", "blue_soft": "rgba(87,193,255,0.16)",
    "green": "#59d499", "yellow": "#ffc533", "red": "#ff6161",
}
DUR = {"fast": 120, "base": 180, "slow": 240}

# 卡片备注里给用户看的引擎名 —— 让人一眼知道"这次是谁认的"，出问题也好反馈
OCR_ENGINE_CN = {"native": "本地 PP-OCR", "system": "系统 OCR", "auto": "自动"}


QSS = Template("""
#card { background: $surface; border: 1px solid $hairline; border-radius: 10px; }
/* 常驻面板：和右边那条细边拼成**一整块**，所以右侧两个角不要圆 */
#dockpanel { background: $surface; border: 1px solid $hairline; border-right: none;
             border-top-left-radius: 12px; border-bottom-left-radius: 12px;
             border-top-right-radius: 0; border-bottom-right-radius: 0; }
QLabel { color: $body; }
#word { color: $ink; font-size: 24px; font-weight: 600; }
#panelword { color: $ink; font-size: 17px; font-weight: 600; }
#phon { color: $mute; font-size: 13px; }
#src  { color: $blue; font-size: 11px; background: $blue_soft;
        border: 1px solid $hairline; border-radius: 4px; padding: 1px 6px; }
#note { color: $mute; font-size: 11px; }
#cn   { color: $ink; font-size: 15px; }
#alt  { color: $mute; font-size: 12px; }
#en   { color: $mute; font-size: 12px; }
#sd   { color: $ash; font-size: 11px; }
#hr   { background: $hairline; border: none; max-height: 1px; }
QPushButton {
    background: $elevated; color: $body; border: 1px solid $hairline;
    border-radius: 8px; padding: 6px 12px; font-size: 12px; min-height: 18px;
}
QPushButton:hover { background: $card; color: $ink; border-color: $hairline_strong; }
QPushButton:pressed { background: $canvas; }
QPushButton:disabled { color: $ash; background: $surface; border-color: $hairline; }
QPushButton#go { background: $blue; border-color: $blue; color: $canvas; font-weight: 600; }
QPushButton#go:hover { background: #7bd0ff; border-color: #7bd0ff; }
QPushButton#go:pressed { background: #3aa9ea; border-color: #3aa9ea; }
QPushButton#ghost { background: transparent; border-color: transparent; color: $mute; }
QPushButton#ghost:hover { background: $card; border-color: transparent; color: $ink; }
QTextBrowser { background: $elevated; border: 1px solid $hairline; border-radius: 8px;
               color: $body; font-size: 13px; padding: 8px 10px; }
QLineEdit { background: $elevated; border: 1px solid $hairline; border-radius: 8px;
            color: $ink; padding: 7px 10px; font-size: 13px;
            selection-background-color: $blue; selection-color: $canvas; }
QLineEdit:focus { border-color: $hairline_strong; }
QComboBox { background: $elevated; border: 1px solid $hairline; border-radius: 8px;
            padding: 6px 10px; color: $ink; font-size: 12px; }
QComboBox:hover { border-color: $hairline_strong; }
QComboBox::drop-down { border: none; width: 16px; }
QComboBox QAbstractItemView { background: $elevated; color: $ink; border: 1px solid $hairline;
                              selection-background-color: $card; outline: none; }
QCheckBox { color: $body; font-size: 12px; spacing: 8px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid $hairline_strong;
                       border-radius: 4px; background: $elevated; }
QCheckBox::indicator:checked { background: $blue; border-color: $blue; }
QDialog { background: $canvas; }
QMenu { background: $elevated; color: $body; border: 1px solid $hairline; padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: 6px; }
QMenu::item:selected { background: $card; color: $ink; }
QMenu::separator { height: 1px; background: $hairline; margin: 4px 6px; }
QToolTip { background: $elevated; color: $ink; border: 1px solid $hairline; padding: 4px 6px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: $hairline_strong; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #4a5058; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
#dockrail { background: $surface; border: 1px solid $hairline; border-right: none;
            border-top-right-radius: 0; border-bottom-right-radius: 0;
            border-top-left-radius: 12px; border-bottom-left-radius: 12px; }
#dockrail:hover { background: $card; border-color: $hairline_strong; }
#raillogo { color: $blue; font-size: 16px; font-weight: 800; }
#railtext { color: $ash; font-size: 10px; letter-spacing: 1px; }
QFrame#dockrail:hover QLabel#railtext { color: $mute; }
#railarrow { color: $mute; font-size: 13px; }
QFrame#dockrail:hover QLabel#railarrow { color: $blue; }
#chip { background: $elevated; border: 1px solid $hairline; border-radius: 9px;
        color: $mute; font-size: 10px; padding: 1px 7px; }
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
                pen = QtGui.QPen(QtGui.QColor(138, 180, 248), 2)
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


class Card(QtWidgets.QWidget):
    """一张卡片。pinned 之后就固定住，再查会新开一张。"""

    closed = QtCore.Signal(object)
    ask_requested = QtCore.Signal(object, str, bool)   # brief, 问题, 是否用 DeepSeek
    detail_requested = QtCore.Signal(object)
    word_clicked = QtCore.Signal(str)

    def __init__(self, font_pt=10):
        super().__init__(None)
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(QSS)
        self.setFixedWidth(430)
        self.brief = None
        self.pinned = False
        self._drag = None
        self._jobs = []
        self._build()
        self.setFont(QtGui.QFont("Microsoft YaHei UI", font_pt))

    # ---------- 界面 ----------
    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 12)      # 给 frame 外围留出投影的地方
        self.frame = QtWidgets.QFrame(objectName="card")
        outer.addWidget(self.frame)
        _shadow(self.frame)                        # 卡片浮在别人家网页上，一层淡投影分开
        v = QtWidgets.QVBoxLayout(self.frame)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        self.lb_word = QtWidgets.QLabel("…", objectName="word")
        self.lb_phon = QtWidgets.QLabel("", objectName="phon")
        self.lb_src = QtWidgets.QLabel("", objectName="src")
        head.addWidget(self.lb_word)
        head.addWidget(self.lb_phon)
        head.addStretch(1)
        head.addWidget(self.lb_src)
        v.addLayout(head)

        self.lb_text = QtWidgets.QLabel("", objectName="sd")
        self.lb_text.setWordWrap(True)
        self.lb_text.hide()
        v.addWidget(self.lb_text)

        self.chips = QtWidgets.QHBoxLayout()
        self.chips.setSpacing(4)
        v.addLayout(self.chips)

        self.lb_cn = QtWidgets.QLabel("", objectName="cn")
        self.lb_cn.setWordWrap(True)
        self.lb_cn.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.lb_cn)

        # 有道/百度的对照行单独一个 label：混在 lb_cn 里会跟词典释义一样白一样大，看不出主次
        self.lb_alt = QtWidgets.QLabel("", objectName="alt")
        self.lb_alt.setWordWrap(True)
        self.lb_alt.hide()
        v.addWidget(self.lb_alt)

        self.lb_en = QtWidgets.QLabel("", objectName="en")
        self.lb_en.setWordWrap(True)
        v.addWidget(self.lb_en)

        self.lb_note = QtWidgets.QLabel("", objectName="note")
        self.lb_note.setWordWrap(True)
        v.addWidget(self.lb_note)

        self.detail = QtWidgets.QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setMinimumHeight(260)
        self.detail.hide()
        v.addWidget(self.detail)

        self.chat_log = QtWidgets.QTextBrowser()
        self.chat_log.setMinimumHeight(110)
        self.chat_log.hide()
        v.addWidget(self.chat_log)

        self.chat_row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(self.chat_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.chat_in = QtWidgets.QLineEdit()
        self.chat_in.setPlaceholderText("就这个词问点什么…（回车发送）")
        self.btn_esc = QtWidgets.QPushButton("发送")
        self.btn_esc.setObjectName("go")
        self.btn_esc.setToolTip("回车也是发送。默认本地 9B 答（免费）；答不满意就按住 Shift 点这里，"
                                "强制转 DeepSeek。")
        h.addWidget(self.chat_in, 1)
        h.addWidget(self.btn_esc)
        self.chat_row.hide()
        v.addWidget(self.chat_row)

        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(4)
        hr = QtWidgets.QFrame(objectName="hr")
        hr.setFixedHeight(1)
        v.addWidget(hr)
        self.btn_detail = QtWidgets.QPushButton("详细解释")
        self.btn_detail.setObjectName("go")
        self.btn_chat = QtWidgets.QPushButton("问 AI")
        self.btn_copy = QtWidgets.QPushButton("复制")
        self.btn_pin = QtWidgets.QPushButton("钉住")
        self.btn_close = QtWidgets.QPushButton("✕")
        btns.addWidget(self.btn_detail)
        btns.addWidget(self.btn_chat)
        btns.addStretch(1)
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

    # ---------- 数据 ----------
    def show_brief(self, brief, text=None, at=None):
        self.brief = brief
        self._clear_chips()
        self.detail.hide()
        self.chat_log.hide()
        self.chat_row.hide()
        self.chat_log.clear()

        asked = brief.get("query") or ""
        lex = brief.get("lexeme") or asked
        self.lb_word.setText(lex if brief.get("kind") == "word" else "整句/词组")
        self.lb_phon.setText(("/" + brief["phonetic"] + "/") if brief.get("phonetic") else "")
        tags = " ".join(brief.get("tags") or [])
        self.lb_src.setText(" · ".join(x for x in [brief.get("source"), tags] if x))

        raw = text or asked
        multi = brief.get("kind") == "phrase" or (raw.strip() and raw.strip().lower() != asked.strip().lower())
        if multi:
            self.lb_text.setText(raw.strip()[:300])
            self.lb_text.show()
            self._fill_chips(raw)
        else:
            self.lb_text.hide()

        self.lb_cn.setText("\n".join(brief.get("cn") or []) or "（没有释义）")
        self.lb_alt.setText("")
        self.lb_alt.hide()
        self.lb_en.setText(brief.get("en") or "")
        self.lb_en.setVisible(bool(brief.get("en")))
        notes = list(brief.get("notes") or [])
        if brief.get("cached"):
            notes.append("来自本地缓存（没再联网、没再花 token）")
        if brief.get("exchange"):
            notes.append("变形：" + brief["exchange"])
        self.lb_note.setText(" · ".join(notes))
        self.lb_note.setVisible(bool(notes))

        self.frame.adjustSize()
        self.adjustSize()
        self._place(at)
        self._closing = False
        # 弹出来别"啪"一下：淡入 + 从下方 10px 滑上来（docs/UI-STYLE.md 的动效表）
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        _anim(self, b"windowOpacity", 0.0, 1.0, DUR["base"])
        p = self.pos()
        _anim(self, b"pos", QtCore.QPoint(p.x(), p.y() + 10), p, DUR["base"])

    def set_enrich(self, y):
        """有道回来了就补一行对照（不覆盖词典释义）。"""
        if not y:
            return
        extra = y.get("explains") or []
        if y.get("translation") and not extra:
            extra = [y["translation"]]
        if extra:
            self.lb_alt.setText("有道：" + "；".join(extra[:4]))
            self.lb_alt.show()
            _fade_in(self.lb_alt, DUR["base"])     # 异步补上的对照行，淡入比"啪一下冒出来"好
        if not self.lb_phon.text() and y.get("phonetic"):
            self.lb_phon.setText("/" + y["phonetic"] + "/")
        self._resize_keep_place()

    def set_detail(self, md, err=None):
        self.detail.show()
        if md:
            self.detail.setMarkdown(md)
        else:
            self.detail.setPlainText("没能生成详解：" + (err or "模型没返回内容"))
        self._resize_keep_place()

    def append_chat(self, who, text):
        self.chat_log.show()
        self.chat_row.show()
        color = {"你": "#8ab4f8", "AI": "#e8e8ea", "系统": "#9aa0a6"}.get(who, "#e8e8ea")
        self.chat_log.append('<b style="color:%s">%s</b>：%s' % (color, who, _esc(text)))
        self._resize_keep_place()

    def chat_begin(self, who):
        """开一个空气泡，之后用 chat_push 一段段往里加（流式回答用）。"""
        self.chat_log.show()
        self.chat_row.show()
        color = {"你": "#8ab4f8", "AI": "#e8e8ea", "系统": "#9aa0a6"}.get(who, "#e8e8ea")
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
        if len(toks) < 2:
            return
        for t in toks:
            b = QtWidgets.QPushButton(t)
            b.setToolTip("查这个单词")
            b.clicked.connect(lambda _=False, w=t: self.word_clicked.emit(w))
            self.chips.addWidget(b)
        _fade_in(self.chips, DUR["base"])

    def _clear_chips(self):
        while self.chips.count():
            it = self.chips.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    # ---------- 交互 ----------
    def _toggle_chat(self):
        on = not self.chat_row.isVisible()
        self.chat_row.setVisible(on)
        if on:
            self.chat_log.show()
            self._resize_keep_place()
            # 用户主动要问，才把键盘交过去（这就是"不抢焦点"的让步）
            self.activateWindow()
            self.chat_in.setFocus()

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
        QtCore.QTimer.singleShot(1200, lambda: self.btn_copy.setText("复制"))

    def _toggle_pin(self):
        self.pinned = not self.pinned
        self.btn_pin.setText("已钉住" if self.pinned else "钉住")

    def hide_card(self):
        if getattr(self, "_closing", False):
            return                      # Esc 和 ✕ 一起按、或连按两次 Escape
        self._closing = True
        if not self.isVisible():
            self._really_hide()
            return
        _anim(self, b"windowOpacity", self.windowOpacity(), 0.0, DUR["fast"],
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
        self.move(int(x), int(y0))

    def _resize_keep_place(self):
        """内容变高变矮时跟着改窗口大小；位置不动，高度动起来别跳。

        追问时文字是一段段往外吐的，每段都起 animation 会互相打架，所以用 `_resizing` 当闸门。
        **但闸门期间的请求不能丢** —— 丢了就会出现"点了详细解释、窗口还是聊天时那个高度，
        详解挤成一团／叠在按钮上"，也就是用户报的那个重叠 bug。所以记一个 `_resize_again`，
        等这轮动画跑完再量一次。
        """
        pos = self.pos()
        old = self.height()
        self.frame.adjustSize()
        self.adjustSize()
        new = self.height()
        if getattr(self, "_resizing", False):
            self._resize_again = True       # 别丢：动画结束会再量一次
            return
        if abs(new - old) < 20:
            self.move(pos)                  # adjustSize 已经调过了，小变化不值得动画
            return
        self._resizing = True
        self.resize(self.width(), old)
        _anim(self, b"geometry",
              QtCore.QRect(pos.x(), pos.y(), self.width(), old),
              QtCore.QRect(pos.x(), pos.y(), self.width(), new),
              DUR["base"], done=self._resized)

    def _resized(self):
        self._resizing = False
        if getattr(self, "_resize_again", False):
            self._resize_again = False
            self._resize_keep_place()

    # 拖标题栏移动
    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _):
        self._drag = None


class Tray(QtWidgets.QSystemTrayIcon):
    def __init__(self, app, hotkey, on_settings, on_quit):
        super().__init__()
        pm = QtGui.QPixmap(64, 64)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setBrush(QtGui.QColor("#2d5c8f"))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 60, 60)
        p.setPen(QtGui.QColor("#ffffff"))
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

    def __init__(self, cfg, on_pick, on_lookup, on_clip, on_settings, on_quit):
        super().__init__(None)
        self.cfg = cfg
        d = cfg.get("dock") or {}
        self.expanded = bool(d.get("expanded"))
        self._y = d.get("y")
        self._drag = None
        self._moved = False
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
        self.panel.setFixedWidth(self.PANEL_W)
        self.panel.setFixedHeight(self.PANEL_H)

        v = QtWidgets.QVBoxLayout(self.panel)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(QtWidgets.QLabel("SnapWord", objectName="panelword"))
        head.addWidget(QtWidgets.QLabel("v" + __version__, objectName="chip"))
        head.addStretch(1)
        v.addLayout(head)

        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText("输入或粘贴一个词，回车查")
        self.input.setMinimumHeight(32)
        self.input.returnPressed.connect(lambda: on_lookup(self.input.text()))
        v.addWidget(self.input)

        b1 = QtWidgets.QHBoxLayout()
        b1.setSpacing(8)
        go = QtWidgets.QPushButton("框选屏幕", objectName="go")
        go.setMinimumHeight(30)
        go.clicked.connect(lambda: on_pick())
        clip = QtWidgets.QPushButton("剪贴板")
        clip.setMinimumHeight(30)
        clip.clicked.connect(on_clip)
        b1.addWidget(go, 3)
        b1.addWidget(clip, 2)
        v.addLayout(b1)

        self.lb_hint = QtWidgets.QLabel("", objectName="note")
        self.lb_hint.setWordWrap(True)

        v.addStretch(1)
        v.addWidget(self.lb_hint)       # 提示紧贴底部按钮，中间留白才不像没画完
        v.addSpacing(2)

        b2 = QtWidgets.QHBoxLayout()
        b2.setSpacing(4)
        st = QtWidgets.QPushButton("设置", objectName="ghost")
        st.clicked.connect(on_settings)
        qt = QtWidgets.QPushButton("退出", objectName="ghost")
        qt.clicked.connect(on_quit)
        b2.addStretch(1)
        b2.addWidget(st)
        b2.addWidget(qt)
        v.addLayout(b2)

        # 细边：竖排的 "SnapWord"（一个字母一行，省得写旋转绘制）
        self.rail = QtWidgets.QFrame(self, objectName="dockrail")
        self.rail.setFixedWidth(self.RAIL_W)
        self.rail.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.rail.installEventFilter(self)

        rv = QtWidgets.QVBoxLayout(self.rail)
        rv.setContentsMargins(0, 10, 0, 10)
        rv.setSpacing(6)
        lg = QtWidgets.QLabel("S", objectName="raillogo")
        lg.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
        rv.addWidget(lg)
        rv.addStretch(1)
        self.lb_rail = QtWidgets.QLabel("S\nn\na\np\nW\no\nr\nd", objectName="railtext")
        self.lb_rail.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
        rv.addWidget(self.lb_rail)
        rv.addStretch(1)
        self.lb_arrow = QtWidgets.QLabel("‹", objectName="railarrow")
        self.lb_arrow.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
        rv.addWidget(self.lb_arrow)
        # 点到 label 上必须也算点在细边上：label 会把鼠标事件吃掉，事件过滤器就配不上
        # 「按下 → 抬起」这一对，细边表现为"点不开"。让它们对鼠标完全透明。
        for w in (lg, self.lb_rail, self.lb_arrow):
            w.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_hint(self, text):
        self.lb_hint.setText(text)

    # ---------- 展开 / 收起 / 摆位置 ----------
    def toggle(self):
        self.expanded = not self.expanded
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
        self.lb_arrow.setText("›" if self.expanded else "‹")
        self.rail.setToolTip("收起面板" if self.expanded else "展开 SnapWord 面板（不用热键）")
        self._place(animate=animate)
        self.raise_()               # 别人也是 topmost 的话，每次变化都再顶一次

    def _place(self, animate=False):
        scr = self._screen()
        if scr is None:
            return
        g = scr.availableGeometry()      # 扣掉任务栏那一条（这台机器上任务栏自动隐藏，等于全屏）
        W = self.PANEL_W + self.RAIL_W
        h = self.PANEL_H
        y = g.y() + (g.height() - h) // 2 if self._y is None else int(self._y)
        y = min(max(g.y(), y), max(g.y(), g.y() + g.height() - h))
        # 右边缘永远是屏幕右边缘（EDGE_GAP=0）：展开时窗口 352 宽、x = 右-352；收起时
        # 窗口只有 26 宽、x = 右-26。两种情况 rail 的**屏幕坐标**都是最后那 26px，动的是
        # 窗口左边缘，所以用 layout 会错位（见 _build 的注释），手动摆才对得上。
        right = g.x() + g.width() - self.EDGE_GAP
        self._geo_open = QtCore.QRect(int(right - W), int(y), W, h)
        self._geo_shut = QtCore.QRect(int(right - self.RAIL_W), int(y), self.RAIL_W, h)

        if not animate:
            self.panel.move(0 if self.expanded else W, 0)
            self._settle()
            return

        if self.expanded:
            self.panel.setVisible(True)
            self.setGeometry(self._geo_open)        # 窗口先长出来，rail 立刻到位
            self.rail.setGeometry(self.PANEL_W + 6, 0, self.RAIL_W, h)
            self.panel.move(W, 0)                   # 先藏在窗口右边缘外面
            _anim(self.panel, b"pos", QtCore.QPoint(W, 0), QtCore.QPoint(0, 0), DUR["base"])
        else:
            _anim(self.panel, b"pos", QtCore.QPoint(0, 0), QtCore.QPoint(W, 0), DUR["base"],
                  curve=QtCore.QEasingCurve.Type.InCubic, done=self._settle)

    def _settle(self):
        """把窗口和两个子控件摆成当前状态的"静止样子"（动画结束时也走这里）。"""
        if self.expanded:
            self.setGeometry(self._geo_open)
            self.rail.setGeometry(self.PANEL_W + 6, 0, self.RAIL_W, self.PANEL_H)
            self.panel.move(0, 0)
            self.panel.setVisible(True)
        else:
            self.setGeometry(self._geo_shut)
            self.rail.setGeometry(0, 0, self.RAIL_W, self.PANEL_H)
            self.panel.move(self.PANEL_W + self.RAIL_W, 0)
            self.panel.setVisible(False)
        self.raise_()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.raise_()

    def anchor(self):
        """卡片该出现在哪儿：dock 左边、别被 dock 盖住。

        `Card.show_at` 是按 `at.x() - 卡片宽/3` 摆的，所以这里把那个偏移算进去，
        让卡片右边缘正好落在 dock 左边 12px 处。
        """
        w = 430
        return QtCore.QPoint(self.geometry().left() - 12 - w + w // 3,
                             self.geometry().top() + 46)

    def eventFilter(self, obj, ev):
        t = ev.type()
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
        self.setWindowTitle("SnapWord 设置 · v%s" % __version__)
        self.setMinimumWidth(460)
        self.setStyleSheet(QSS)
        f = QtWidgets.QFormLayout(self)
        f.setSpacing(10)
        p = cfg["providers"]

        self.hotkey = QtWidgets.QLineEdit(cfg["hotkey"])
        self.engine = QtWidgets.QComboBox()
        self.engine.addItems(["auto", "system", "native"])
        self.engine.setCurrentText(cfg.get("ocr_engine", "auto"))
        self.url = QtWidgets.QLineEdit(p["deepseek"]["url"])
        self.model = QtWidgets.QLineEdit(p["deepseek"]["model"])
        self.key = QtWidgets.QLineEdit(p["deepseek"]["key"])
        self.key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.ollama = QtWidgets.QLineEdit(p["ollama"]["model"])
        bd = p.get("baidu") or {}
        self.bd_appid = QtWidgets.QLineEdit(bd.get("appid", ""))
        self.bd_key = QtWidgets.QLineEdit(bd.get("key", ""))
        self.bd_key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.auto_detail = QtWidgets.QCheckBox("出结果后自动生成详细解释（会多花 token）")
        self.auto_detail.setChecked(bool(cfg.get("auto_detail")))
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
        self.dock_screen = QtWidgets.QComboBox()
        for i, s in enumerate(QtWidgets.QApplication.screens()):
            name = "" if s is primary else s.name()
            g = s.geometry()
            self.dock_screen.addItem("%s（%d×%d）" % (s.name(), g.width(), g.height()), name)
            if name == cur:
                self.dock_screen.setCurrentIndex(i)
        if dock is not None:            # 换屏要立刻看见效果，不等"保存"
            self.dock_screen.currentIndexChanged.connect(
                lambda _i: dock.set_screen(self.dock_screen.currentData() or ""))

        f.addRow("取词热键", self.hotkey)
        f.addRow("OCR 引擎", self.engine)
        f.addRow("本地模型", self.ollama)
        f.addRow("DeepSeek 地址", self.url)
        f.addRow("DeepSeek 模型", self.model)
        f.addRow("DeepSeek Key", self.key)
        f.addRow("百度 appid", self.bd_appid)
        f.addRow("百度密钥", self.bd_key)
        f.addRow("", self.auto_detail)
        f.addRow("", self.dock)
        f.addRow("面板挂在哪块屏", self.dock_screen)
        f.addRow("", self.autostart)
        hint = QtWidgets.QLabel(
            "释义以离线词典 ECDICT 和有道为准，查不到再用百度翻译（要在 fanyi-api.baidu.com "
            "免费领 appid 和密钥）。\n"
            "问答默认交给本地 9B（免费、不花 token）；本地没在跑时会自动改用 DeepSeek。"
            "按住 Shift 点「问 AI」就是强制走 DeepSeek。")
        hint.setObjectName("note")
        hint.setWordWrap(True)
        f.addRow("", hint)

        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText("保存")
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.setWindowOpacity(0.0)
        _anim(self, b"windowOpacity", 0.0, 1.0, DUR["fast"])

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
        self._ensure_card(pos=at).show_brief(
            {"query": "识别中…", "cn": [], "kind": "word", "source": "…"}, at=at)
        png = pixmap_png(pixmap)
        self._run(self._ocr_then_lookup, png)

    def lookup_text(self, text, at=None):
        text = (text or "").strip()
        if not text:
            return
        self._ocr_info = None
        self._ensure_card(pos=at or QtGui.QCursor.pos())
        self._run(self._lookup_brief, text, at)

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
    def _run(self, fn, *args, **kw):
        j = Job(fn, *args, stream=bool(kw.pop("stream", False)), **kw)
        j.done.connect(lambda r, f=fn: self._on_job_done(f, r))
        j.fail.connect(lambda e, f=fn: self._on_job_fail(f, e))
        j.progress.connect(lambda p, f=fn: self._on_job_progress(f, p))
        j.finished.connect(lambda j=j: self._jobs.remove(j) if j in self._jobs else None)
        self._jobs.append(j)
        j.start()

    def _on_job_progress(self, fn, piece):
        """流式回答：先来 ("src", 谁在答)，之后是一段段 ("text", 正文)。"""
        if getattr(fn, "__name__", "") != "_ask_stream" or not self.current:
            return
        kind, val = piece
        if kind == "src":
            self._ask_started = True
            who = "AI（DeepSeek）" if val == "deepseek" else "AI（本地 9B）"
            self.current.chat_begin(who)
        elif kind == "text":
            self.current.chat_push(str(val))

    def _on_job_done(self, fn, result):
        name = getattr(fn, "__name__", "")
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
            self._run(self._lookup_brief, text, getattr(self, "_last_at", None))
        elif name == "_lookup_brief":
            brief, text, at = result
            self._show_brief(brief, text, at)
        elif name == "_lookup_enrich":
            if self.current:
                self.current.set_enrich(result)
        elif name == "_lookup_detail":
            md, err = result
            if self.current:
                self.current.set_detail(md, err)
        elif name == "_ask_stream":
            if self.current:
                self.current._resize_keep_place()   # 收尾时按最终高度定一次

    def _on_job_fail(self, fn, err):
        name = getattr(fn, "__name__", "")
        if name == "_ask_stream" and self.current:
            if not getattr(self, "_ask_started", False):
                self.current.chat_begin("系统")      # 一个字都没来得及吐：先开气泡再说
            self.current.chat_push("出错了：" + err)
            self.current._resize_keep_place()
        elif self.current:
            self.current.set_detail(None, err)

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

        顺手把这轮问答记进 self._chat_hist（只留最近 8 条），这样追问「再举个例子」
        模型知道上文。换词查时 _show_brief 会把历史清掉。
        """
        buf = []
        for kind, val in self.lookup.ask_stream(brief, q, list(self._chat_hist),
                                                escalate=escalate):
            if kind == "text":
                buf.append(val)
            yield (kind, val)
        self._chat_hist.append({"role": "user", "content": q})
        self._chat_hist.append({"role": "assistant", "content": "".join(buf)})
        del self._chat_hist[:-8]

    # ---------- 卡片管理 ----------
    def _ensure_card(self, pos=None):
        if self.current and not self.current.pinned:
            return self.current
        c = Card()
        c.closed.connect(self._on_card_closed)
        c.detail_requested.connect(self._on_detail)
        c.ask_requested.connect(self._on_ask)
        c.word_clicked.connect(lambda w: self.lookup_text(w))
        self.cards.append(c)
        self.current = c
        return c

    def _on_card_closed(self, card):
        if card in self.cards:
            self.cards.remove(card)
        if self.current is card:
            self.current = self.cards[-1] if self.cards else None
        self._sync_esc()

    def _on_detail(self, brief):
        if not brief:
            return
        self.current.btn_detail.setEnabled(False)
        self.current.btn_detail.setText("生成中…")
        self.current.detail.show()
        self.current.detail.setPlainText("本地模型/DeepSeek 正在整理…")
        self.current._resize_keep_place()
        self._run(self._lookup_detail, brief)

    def _on_ask(self, brief, q, escalate):
        # 谁来答由 lookup.ask_plan 决定：本地 9B 在跑就用它，没在跑就自动上网（填了 key 的话）。
        self._ask_started = False
        self._run(self._ask_stream, brief, q, escalate, stream=True)

    def _show_brief(self, brief, text, at):
        self._chat_hist = []        # 换词了，之前的追问上下文作废
        self.current.show_brief(brief, text=text, at=at)
        self.current.btn_detail.setEnabled(True)
        if self.cfg.get("auto_detail") and (brief.get("cn") or brief.get("en")):
            self._on_detail(brief)
        else:
            self._run(self._lookup_enrich, brief)
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

    def on_settings():
        d = Settings(cfg, dock=dock)
        if d.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            new = d.apply()
            from . import config
            config.save(new)
            if dock:
                dock.set_enabled(bool((new.get("dock") or {}).get("enabled", True)))
            QtWidgets.QMessageBox.information(
                None, "SnapWord", "已保存。热键的改动要重启才生效。")

    def on_quit():
        try:
            ctrl._esc_thread and ctrl._esc_thread.stop()
        finally:
            QtWidgets.QApplication.quit()

    tray = Tray(ctrl, cfg["hotkey"], on_settings, on_quit)
    tray.show()
    ctrl.tray = tray

    # 屏幕右边缘的常驻面板 —— 这是"不依赖热键"的那条路
    dock = None
    if (cfg.get("dock") or {}).get("enabled", True):
        def on_clip():
            ctrl.lookup_text(QtWidgets.QApplication.clipboard().text(),
                             at=dock.anchor() if dock else None)

        dock = Dock(cfg, ctrl.start_pick,
                    lambda t: ctrl.lookup_text(t, at=dock.anchor()),
                    on_clip, on_settings, on_quit)
        dock.set_hint("热键：%s 框选屏幕。不想记热键就用上面的按钮。" % cfg["hotkey"])
        dock.show()
        ctrl.dock = dock
        tray.attach_dock(dock)      # 托盘里能开关面板、换屏、展开收起

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
                w = ctrl.dock if demo.startswith("dock") else ctrl.current
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
    return app.exec()


if __name__ == "__main__":
    from . import config

    sys.exit(run(config.load()))
