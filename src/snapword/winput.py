"""Windows 侧的小工具：全局热键、模拟 Ctrl+C 取划词、前台窗口类名。

只用 ctypes（标准库），所以不引 pywin32/keyboard 之类。
"""
import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_MENU = 0x12   # Alt
VK_SHIFT = 0x10
VK_C = 0x43

# Ctrl+C 在控制台里是"打断"信号，不能乱发
CONSOLE_CLASSES = {
    "ConsoleWindowClass",              # conhost
    "CASCADIA_HOSTING_WINDOW_CLASS",   # Windows Terminal
    "mintty", "Vim", "PuTTY", "tty",
}

ULONG_PTR = ctypes.c_size_t

# INPUT 的 union 里最大的成员是 MOUSEINPUT：x64 = 32 字节，x86 = 24 字节。
_MOUSEINPUT_SIZE = 32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTunion(ctypes.Union):
    # union 里最大的成员是 MOUSEINPUT（x64 上 32 字节、x86 上 24），cbSize 得按它算。
    # 我们只发键盘事件，但 SendInput 拿 cbSize 跟真正的 sizeof(INPUT) 对，对不上就
    # **一个事件都不发**（返回 0 + ERROR_INVALID_PARAMETER），不报错也不崩 —— 划词会静默失效。
    # 注意这里必须放一个跟 MOUSEINPUT **一样大**的成员：union 取最大的那个，
    # 放"差值"是没用的（ki 本身 24 字节，比差值大，union 就还是 24）。
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("_pad_to_mouseinput", ctypes.c_byte * _MOUSEINPUT_SIZE),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


def _key(vk, up=False):
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return inp


def send_ctrl_c():
    seq = (_INPUT * 4)(
        _key(VK_CONTROL), _key(VK_C), _key(VK_C, True), _key(VK_CONTROL, True))
    user32.SendInput(4, ctypes.byref(seq), ctypes.sizeof(_INPUT))


def _down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def wait_modifiers_released(timeout=0.8):
    """热键还按着的时候发的 Ctrl+C 会变成 Ctrl+Alt+C，等用户松手。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not (_down(VK_CONTROL) or _down(VK_MENU) or _down(VK_SHIFT)):
            return True
        time.sleep(0.02)
    return False


def foreground_class():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def clipboard_seq():
    return kernel32.GetClipboardSequenceNumber()


def copy_is_safe():
    return foreground_class() not in CONSOLE_CLASSES


# ---------------- 全局热键 ----------------
_KEYS = {"esc": 0x1B, "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08,
         "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23}


def _vk(name):
    n = name.lower()
    if n in _KEYS:
        return _KEYS[n]
    if len(n) == 2 and n[0] == "f" and n[1].isdigit():
        return 0x70 + int(n[1]) - 1
    if len(n) == 3 and n[0] == "f" and n[1:].isdigit():
        i = int(n[1:])
        if 1 <= i <= 24:
            return 0x70 + i - 1
    if len(n) == 1:
        return ord(n.upper())
    raise ValueError("认不出的按键：" + name)


def parse_hotkey(text):
    mods, vk = 0, None
    for part in text.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part in ("alt", "menu"):
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "super"):
            mods |= MOD_WIN
        elif part:
            vk = _vk(part)
    if vk is None:
        raise ValueError("热键没有主键：" + text)
    return mods | MOD_NOREPEAT, vk


class HotkeyThread(threading.Thread):
    """在独立线程里 RegisterHotKey(hwnd=NULL)，用 PeekMessage 收 WM_HOTKEY。

    Qt 的事件循环收不到别的线程的 WM_HOTKEY，所以必须自己开一个消息泵。

    **callback 是在这个线程里被调用的**：里面绝对不要碰 Qt 控件 —— 跨线程创建/显示窗口
    是未定义行为，实测直接把进程搞崩。要更新界面就把 callback 传成某个 QObject 信号的
    `.emit`（信号跨线程自动排队回主线程，见 gui.App.hotkey_fired）。

    mapping 的值可以是字符串（首选热键）或字符串列表（首选 + 备选）。
    **全局热键是稀缺资源**：这台机器上 ctrl+alt+D、ctrl+alt+W 已经被别的软件占了
    （RegisterHotKey 返回 0，错误码 1409 ERROR_HOTKEY_ALREADY_REGISTERED），
    所以每个动作都给一条备选链，注册上的那个记在 `registered` 里。
    """

    daemon = True

    def __init__(self, mapping, callback):
        super().__init__(name="snapword-hotkeys")
        self.mapping = mapping          # {动作名: 热键 或 [热键, ...]}
        self.callback = callback        # callback(动作名)
        self._stop = threading.Event()
        self._ids = {}                  # id -> 动作名
        self.registered = {}            # 动作名 -> 真正注册上的热键
        self.errors = []

    def run(self):
        i = 0
        for name, cands in self.mapping.items():
            if isinstance(cands, str):
                cands = [cands]
            for hk in cands:
                i += 1
                try:
                    mods, vk = parse_hotkey(hk)
                except ValueError as ex:
                    self.errors.append(str(ex))
                    continue
                if user32.RegisterHotKey(None, i, mods, vk):
                    self._ids[i] = name
                    self.registered[name] = hk
                    break
                self.errors.append("热键 %s 被占用（RegisterHotKey 错误码 %d）"
                                   % (hk, ctypes.get_last_error()))
        if not self.registered:
            self.callback(None)   # 一个都没注册上，让主线程发现
            return

        msg = wintypes.MSG()
        while not self._stop.is_set():
            got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)  # PM_REMOVE
            if got:
                if msg.message == WM_HOTKEY:
                    name = self._ids.get(int(msg.wParam) & 0xFFFF)  # wParam 低位是热键 id
                    if name:
                        self.callback(name)
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.02)
        for i in self._ids:
            user32.UnregisterHotKey(None, i)
        self._ids.clear()

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------
# 开机启动：写 HKCU 的 Run 键。不用计划任务、不用 StartUp 文件夹快捷方式 ——
# 注册表最省事（不需要管理员，开关就是写/删一个值），卸载也干净。
# --------------------------------------------------------------------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "SnapWord"


def autostart_command():
    """开机要执行的命令行。用 pythonw.exe，不然登录时会先闪一个黑框。"""
    import sys
    from pathlib import Path
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            exe = pyw
    return '"%s" -m snapword.gui' % exe


def autostart_get():
    """注册表里那条命令；没开就是 None。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, _RUN_NAME)
            return val or None
    except OSError:
        return None


def autostart_enabled():
    return autostart_get() is not None


def autostart_sync():
    """开着但路径变了（挪了目录 / 换了 venv）就改写成现在的命令。

    不做的话用户挪一次文件夹，开机启动就静默失效了。
    """
    cur = autostart_get()
    if cur is None:
        return False
    want = autostart_command()
    if cur != want:
        autostart_set(True)
        return True
    return False


def autostart_set(on):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, _RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, _RUN_NAME)
            except FileNotFoundError:
                pass
    return autostart_enabled() == bool(on)
