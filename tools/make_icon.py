"""生成 `assets/snapword.ico` —— 桌面快捷方式用那个图标。

画法和 `src/snapword/gui.py` 里 Tray 的托盘图标一致（蓝底白 S），只是画大一点、带透明边。
ICO 容器就是 22 字节头 + 一张 PNG（Vista 以后都认），手写比找库省事。

用法：`.venv\\Scripts\\python.exe tools\\make_icon.py [输出路径]`
"""
import os
import struct
import sys
import tempfile

# 注意：**不能**用 offscreen 平台 —— 它没有字体库，`drawText` 只会画出个"缺字形"方框。
from PySide6 import QtCore, QtGui, QtWidgets      # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "snapword.ico")

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

SIZE = 256
pm = QtGui.QPixmap(SIZE, SIZE)
pm.fill(QtCore.Qt.GlobalColor.transparent)
p = QtGui.QPainter(pm)
p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
m = SIZE // 32                                   # 圆外边留一点空隙，缩到 16px 也不糊边
p.setBrush(QtGui.QColor("#2d5c8f"))
p.setPen(QtCore.Qt.PenStyle.NoPen)
p.drawEllipse(m, m, SIZE - 2 * m, SIZE - 2 * m)
p.setPen(QtGui.QColor("#ffffff"))
f = QtGui.QFont("Segoe UI")
f.setPointSize(int(SIZE * 0.52))
f.setBold(True)
p.setFont(f)
p.drawText(pm.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "S")
p.end()

fd, png_path = tempfile.mkstemp(suffix=".png")
os.close(fd)
try:
    if not pm.save(png_path, "PNG"):
        raise SystemExit("PNG 都没存下来：%s" % png_path)
    png = open(png_path, "rb").read()
finally:
    os.remove(png_path)                          # 中间产物不留，只留 .ico

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "wb") as fh:
    fh.write(struct.pack("<HHH", 0, 1, 1))                      # ICONDIR：保留位/类型=图标/张数
    fh.write(struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32,        # 0 = 256px，1 平面，32 位
                         len(png), 6 + 16))                     # 头 6 字节 + 一条目录 16 字节
    fh.write(png)
print("ico -> %s (%d bytes)" % (OUT, os.path.getsize(OUT)))
