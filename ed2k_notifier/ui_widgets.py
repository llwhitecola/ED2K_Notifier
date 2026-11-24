"""Modern UI components, HiDPI vector icons, themes, and Windows DWM helpers."""

from __future__ import annotations

import ctypes
from pathlib import Path
import sys
from typing import Sequence

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def get_resource_path(relative_path: str) -> Path:
    """Resolve bundled asset paths in both source mode and PyInstaller frozen mode."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative_path


def get_app_icon() -> QIcon:
    """Load the application icon (multi-size ICO preferred, falling back to PNG)."""
    for rel in ("assets/logo.ico", "assets/logo.png"):
        p = get_resource_path(rel)
        if p.is_file():
            icon = QIcon(str(p))
            if not icon.isNull():
                return icon
    return make_icon("film", "#38bdf8", 32)


def set_windows_app_id(app_id: str = "llwhitecola.ED2KNotifier.1") -> None:
    """Set explicit Windows AppUserModelID so taskbar displays the custom app icon."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def apply_windows_titlebar_theme(widget: QWidget, dark: bool = True) -> None:
    """Enable/disable native Windows 11/10 immersive dark title bar via DWM API."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return
        value = ctypes.c_int(1 if dark else 0)
        dwmapi = ctypes.windll.dwmapi
        # Windows 11 & Windows 10 20H1+ (attribute 20); older Win10 builds (attribute 19)
        res = dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        if res != 0:
            dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def make_icon(name: str, color: str = "#cbd5e1", size: int = 16) -> QIcon:
    """Render a crisp 2x HiDPI Lucide-style monochrome vector icon using QPainter."""
    scale = 2
    px_size = size * scale
    pm = QPixmap(px_size, px_size)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(scale)

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 1.65, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    # Coordinate system normalized to 16x16 logical space
    s = size / 16.0
    p.scale(s, s)

    if name == "file_plus":
        path = QPainterPath()
        path.moveTo(4, 2)
        path.lineTo(9.5, 2)
        path.lineTo(13, 5.5)
        path.lineTo(13, 14)
        path.lineTo(4, 14)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(9.5, 2), QPointF(9.5, 5.5))
        p.drawLine(QPointF(9.5, 5.5), QPointF(13, 5.5))
        p.drawLine(QPointF(8.5, 8.2), QPointF(8.5, 11.8))
        p.drawLine(QPointF(6.7, 10.0), QPointF(10.3, 10.0))

    elif name == "folder_plus":
        path = QPainterPath()
        path.moveTo(2, 4.2)
        path.lineTo(6.2, 4.2)
        path.lineTo(7.6, 5.8)
        path.lineTo(14, 5.8)
        path.lineTo(14, 13.2)
        path.lineTo(2, 13.2)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(8, 7.8), QPointF(8, 11.2))
        p.drawLine(QPointF(6.3, 9.5), QPointF(9.7, 9.5))

    elif name == "play":
        p.setBrush(QBrush(QColor(color)))
        poly = QPolygonF([QPointF(4.5, 3.0), QPointF(13.0, 8.0), QPointF(4.5, 13.0)])
        p.drawPolygon(poly)

    elif name == "stop":
        p.setBrush(QBrush(QColor(color)))
        p.drawRoundedRect(QRectF(4.0, 4.0, 8.0, 8.0), 1.5, 1.5)

    elif name == "clean":
        p.drawLine(QPointF(3, 5), QPointF(13, 5))
        p.drawLine(QPointF(5, 9), QPointF(11, 9))
        p.drawLine(QPointF(7, 13), QPointF(9, 13))

    elif name == "history":
        p.drawArc(QRectF(2.5, 2.5, 11, 11), 30 * 16, 300 * 16)
        p.drawLine(QPointF(8, 5), QPointF(8, 8.2))
        p.drawLine(QPointF(8, 8.2), QPointF(10.5, 9.5))
        p.drawLine(QPointF(2.4, 5.2), QPointF(4.8, 5.2))
        p.drawLine(QPointF(2.4, 5.2), QPointF(2.4, 2.8))

    elif name == "settings":
        p.drawEllipse(QPointF(8, 8), 2.5, 2.5)
        p.drawEllipse(QPointF(8, 8), 5.3, 5.3)
        for angle_pt in [(8, 1.4), (8, 14.6), (1.4, 8), (14.6, 8)]:
            p.drawPoint(QPointF(*angle_pt))

    elif name == "copy":
        p.drawRoundedRect(QRectF(5.5, 5.5, 8.0, 8.5), 1.4, 1.4)
        p.drawLine(QPointF(3.5, 10.5), QPointF(2.8, 10.5))
        p.drawLine(QPointF(2.8, 10.5), QPointF(2.8, 2.8))
        p.drawLine(QPointF(2.8, 2.8), QPointF(10.2, 2.8))
        p.drawLine(QPointF(10.2, 2.8), QPointF(10.2, 3.6))

    elif name == "file_text":
        path = QPainterPath()
        path.moveTo(3.5, 2)
        path.lineTo(9.5, 2)
        path.lineTo(12.5, 5)
        path.lineTo(12.5, 14)
        path.lineTo(3.5, 14)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(5.5, 8.0), QPointF(10.5, 8.0))
        p.drawLine(QPointF(5.5, 10.8), QPointF(9.5, 10.8))

    elif name == "send":
        poly = QPolygonF([QPointF(14.0, 2.2), QPointF(2.0, 7.2), QPointF(6.8, 9.2), QPointF(14.0, 2.2)])
        p.drawPolygon(poly)
        p.drawLine(QPointF(14.0, 2.2), QPointF(10.2, 13.8))
        p.drawLine(QPointF(10.2, 13.8), QPointF(6.8, 9.2))

    elif name == "trash":
        p.drawLine(QPointF(2.8, 4.4), QPointF(13.2, 4.4))
        p.drawLine(QPointF(6.0, 4.4), QPointF(6.0, 2.6))
        p.drawLine(QPointF(6.0, 2.6), QPointF(10.0, 2.6))
        p.drawLine(QPointF(10.0, 2.6), QPointF(10.0, 4.4))
        p.drawRoundedRect(QRectF(4.2, 4.4, 7.6, 9.4), 1.2, 1.2)
        p.drawLine(QPointF(6.8, 7.0), QPointF(6.8, 11.4))
        p.drawLine(QPointF(9.2, 7.0), QPointF(9.2, 11.4))

    elif name == "search":
        p.drawEllipse(QPointF(7.0, 7.0), 4.2, 4.2)
        p.drawLine(QPointF(10.2, 10.2), QPointF(13.8, 13.8))

    elif name == "film":
        p.drawRoundedRect(QRectF(2.0, 3.0, 12.0, 10.0), 1.8, 1.8)
        p.drawLine(QPointF(5.0, 3.0), QPointF(5.0, 13.0))
        p.drawLine(QPointF(11.0, 3.0), QPointF(11.0, 13.0))
        p.drawLine(QPointF(2.0, 6.4), QPointF(5.0, 6.4))
        p.drawLine(QPointF(2.0, 9.6), QPointF(5.0, 9.6))
        p.drawLine(QPointF(11.0, 6.4), QPointF(14.0, 6.4))
        p.drawLine(QPointF(11.0, 9.6), QPointF(14.0, 9.6))

    elif name == "external":
        p.drawLine(QPointF(9.0, 2.5), QPointF(13.5, 2.5))
        p.drawLine(QPointF(13.5, 2.5), QPointF(13.5, 7.0))
        p.drawLine(QPointF(13.5, 2.5), QPointF(7.2, 8.8))
        path = QPainterPath()
        path.moveTo(11.5, 9.0)
        path.lineTo(11.5, 13.2)
        path.lineTo(2.8, 13.2)
        path.lineTo(2.8, 4.5)
        path.lineTo(7.0, 4.5)
        p.drawPath(path)

    elif name == "sun":
        p.drawEllipse(QPointF(8, 8), 3.0, 3.0)
        for dx, dy in [(0, -5.5), (0, 5.5), (-5.5, 0), (5.5, 0), (-4, -4), (4, 4), (-4, 4), (4, -4)]:
            p.drawLine(QPointF(8 + dx * 0.72, 8 + dy * 0.72), QPointF(8 + dx, 8 + dy))

    elif name == "moon":
        path = QPainterPath()
        path.addEllipse(QRectF(2.8, 2.6, 10.6, 10.6))
        cut = QPainterPath()
        cut.addEllipse(QRectF(6.2, 1.2, 9.6, 9.6))
        crescent = path.subtracted(cut)
        p.setBrush(QBrush(QColor(color)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(crescent)

    elif name == "check":
        p.drawLine(QPointF(3.2, 8.3), QPointF(6.6, 11.6))
        p.drawLine(QPointF(6.6, 11.6), QPointF(13.0, 4.6))

    p.end()
    return QIcon(pm)


def rounded_pixmap(
    source: QPixmap,
    target_size: QSize,
    radius: int = 10,
    border_color: str = "#334155",
) -> QPixmap:
    """Scale and clip a poster QPixmap to crisp antialiased rounded corners."""
    scale = 2
    w, h = target_size.width(), target_size.height()
    scaled = source.scaled(
        w * scale,
        h * scale,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    # Center-crop to exact target aspect ratio
    x = max(0, (scaled.width() - w * scale) // 2)
    y = max(0, (scaled.height() - h * scale) // 2)
    cropped = scaled.copy(x, y, w * scale, h * scale)
    cropped.setDevicePixelRatio(scale)

    out = QPixmap(w * scale, h * scale)
    out.fill(Qt.GlobalColor.transparent)
    out.setDevicePixelRatio(scale)

    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), radius, radius)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, cropped)

    # Subtle crisp rim border
    p.setClipping(False)
    p.setPen(QPen(QColor(border_color), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), radius, radius)
    p.end()
    return out


class ToggleSwitch(QCheckBox):
    """Sleek iOS/Fluent-style pill toggle switch replacing default Qt checkboxes."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._track_w = 34
        self._track_h = 18
        self._dark = True

    def set_dark_mode(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self.text()) + (8 if self.text() else 0)
        return QSize(self._track_w + text_w + 4, max(self._track_h + 6, fm.height() + 4))

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        y = (self.height() - self._track_h) / 2.0
        track_rect = QRectF(1.0, y, self._track_w, self._track_h)
        radius = self._track_h / 2.0

        if self.isChecked():
            track_color = QColor("#3b82f6")
        else:
            track_color = QColor("#334155") if self._dark else QColor("#cbd5e1")

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_color))
        p.drawRoundedRect(track_rect, radius, radius)

        thumb_d = self._track_h - 4
        thumb_x = (self._track_w - thumb_d - 1.0) if self.isChecked() else 3.0
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QRectF(thumb_x, y + 2.0, thumb_d, thumb_d))

        if self.text():
            text_color = QColor("#cbd5e1") if self._dark else QColor("#334155")
            p.setPen(text_color)
            text_rect = QRectF(self._track_w + 8, 0, self.width() - self._track_w - 8, self.height())
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        p.end()


class SlimProgressWidget(QWidget):
    """Minimal 6px rounded progress track with subtle completion state (replaces bulky blue bricks)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = 0
        self._dark = True
        self.setMinimumHeight(24)

    def setValue(self, value: int) -> None:
        self._value = max(0, min(100, int(value)))
        self.update()

    def set_dark_mode(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()

        if self._value <= 0:
            p.setPen(QColor("#475569") if self._dark else QColor("#94a3b8"))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "—")
            p.end()
            return

        if self._value >= 100:
            # Clean subtle emerald check + 100%
            accent = QColor("#10b981") if self._dark else QColor("#059669")
            p.setPen(accent)
            font = QFont(self.font())
            font.setPointSize(9)
            font.setBold(True)
            p.setFont(font)
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "100%")
            p.end()
            return

        # Active computing: slim 6px rounded gradient track + compact % label
        text_w = 34
        bar_w = max(24, w - text_w - 14)
        bar_h = 6.0
        bar_x = 6.0
        bar_y = (h - bar_h) / 2.0

        track_bg = QColor("#1e293b") if self._dark else QColor("#e2e8f0")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_bg))
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 3.0, 3.0)

        fill_w = max(6.0, bar_w * (self._value / 100.0))
        grad = QLinearGradient(bar_x, 0, bar_x + fill_w, 0)
        grad.setColorAt(0.0, QColor("#38bdf8"))
        grad.setColorAt(1.0, QColor("#3b82f6"))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 3.0, 3.0)

        p.setPen(QColor("#93c5fd") if self._dark else QColor("#2563eb"))
        font = QFont(self.font())
        font.setPointSize(8)
        p.setFont(font)
        p.drawText(
            QRectF(bar_x + bar_w + 4, 0, text_w, h),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            f"{self._value}%",
        )
        p.end()


def classify_status_tone(status_text: str) -> str:
    """Map Chinese status text to a semantic color tone."""
    if "失败" in status_text:
        return "error"
    if "已推送" in status_text or "已打包推送" in status_text:
        return "success"
    if "缓存" in status_text or "秒算" in status_text:
        return "cache"
    if "计算中" in status_text or "查询" in status_text or "下载" in status_text or "推送中" in status_text or "推送 Telegram" in status_text:
        return "working"
    if "完成" in status_text:
        return "done"
    return "idle"


_PILL_STYLES_DARK = {
    "success": ("rgba(16, 185, 129, 0.16)", "#34d399", "rgba(16, 185, 129, 0.30)"),
    "done": ("rgba(56, 189, 248, 0.15)", "#38bdf8", "rgba(56, 189, 248, 0.28)"),
    "working": ("rgba(59, 130, 246, 0.18)", "#60a5fa", "rgba(59, 130, 246, 0.35)"),
    "cache": ("rgba(139, 92, 246, 0.18)", "#c4b5fd", "rgba(139, 92, 246, 0.34)"),
    "error": ("rgba(244, 63, 94, 0.16)", "#fb7185", "rgba(244, 63, 94, 0.32)"),
    "idle": ("rgba(148, 163, 184, 0.12)", "#94a3b8", "rgba(148, 163, 184, 0.20)"),
}

_PILL_STYLES_LIGHT = {
    "success": ("rgba(16, 185, 129, 0.14)", "#047857", "rgba(16, 185, 129, 0.30)"),
    "done": ("rgba(2, 132, 199, 0.12)", "#0369a1", "rgba(2, 132, 199, 0.26)"),
    "working": ("rgba(37, 99, 235, 0.12)", "#1d4ed8", "rgba(37, 99, 235, 0.28)"),
    "cache": ("rgba(124, 58, 237, 0.12)", "#6d28d9", "rgba(124, 58, 237, 0.26)"),
    "error": ("rgba(225, 29, 72, 0.12)", "#be123c", "rgba(225, 29, 72, 0.28)"),
    "idle": ("rgba(100, 116, 139, 0.10)", "#475569", "rgba(100, 116, 139, 0.20)"),
}


class PillBadgeCell(QWidget):
    """Centered compact rounded pill badge used inside QTableWidget cells."""

    def __init__(self, text: str, tone: str = "idle", dark: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dark = dark
        self._tone = tone
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)
        self.set_badge(text, tone, dark)

    def set_badge(self, text: str, tone: str | None = None, dark: bool | None = None) -> None:
        if tone is not None:
            self._tone = tone
        if dark is not None:
            self._dark = dark
        # Strip noisy raw emoji from cell pill text so badges stay clean
        clean_text = text.replace("✅", "").replace("⚠", "").replace("⚡", "").strip()
        self.label.setText(clean_text)
        palette = _PILL_STYLES_DARK if self._dark else _PILL_STYLES_LIGHT
        bg, fg, border = palette.get(self._tone, palette["idle"])
        self.label.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border: 1px solid {border};"
            "border-radius: 9px; padding: 2px 8px; font-size: 11px; font-weight: 600;"
        )


class FlowLayout(QLayout):
    """Standard Qt wrapping flow layout for Media Spec Chips."""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, h_spacing: int = 5, v_spacing: int = 5) -> None:
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._h_space = h_spacing
        self._v_space = v_spacing
        self._items: list = []

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = eff.x()
        y = eff.y()
        line_h = 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_space
            if next_x - self._h_space > eff.right() and line_h > 0:
                x = eff.x()
                y = y + line_h + self._v_space
                next_x = x + hint.width() + self._h_space
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class SpecChipsWidget(QWidget):
    """Renders extracted media specs (2160p, WEB-DL, Netflix, H.265, etc.) as sleek glass chips."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._flow = FlowLayout(self, margin=0, h_spacing=5, v_spacing=5)
        self._dark = True
        self._chips: list[str] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def set_chips(self, chips: Sequence[str], dark: bool | None = None) -> None:
        if dark is not None:
            self._dark = dark
        self._chips = [c for c in chips if c]
        while self._flow.count():
            item = self._flow.takeAt(0)
            if item and item.widget():
                w = item.widget()
                w.hide()
                w.setParent(None)
                w.deleteLater()

        bg = "rgba(56, 189, 248, 0.12)" if self._dark else "rgba(37, 99, 235, 0.09)"
        fg = "#7dd3fc" if self._dark else "#1d4ed8"
        border = "rgba(56, 189, 248, 0.25)" if self._dark else "rgba(37, 99, 235, 0.22)"
        self.setStyleSheet("background: transparent;")

        for text in self._chips:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"background-color: {bg}; color: {fg}; border: 1px solid {border};"
                "border-radius: 5px; padding: 2px 7px; font-size: 11px; font-weight: 600;"
            )
            self._flow.addWidget(lbl)
        self.setVisible(bool(self._chips))
        self.updateGeometry()


class EmptyDropZone(QFrame):
    """Elegant centered drag-and-drop welcome area shown when the queue is empty."""

    files_dropped = Signal(list)
    add_files_clicked = Signal()
    add_folder_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self._dark = True

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)

        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setPixmap(make_icon("film", "#60a5fa", 44).pixmap(44, 44))
        layout.addWidget(self.icon_label)

        title = QLabel("将电影 / 电视剧文件或整个文件夹拖拽到此处")
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        sub = QLabel("支持 MKV · MP4 · AVI · TS · ISO 等格式 · 自动识别整季并匹配 TMDB 海报与规格")
        sub.setObjectName("dropSub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        layout.addWidget(sub)

        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_row.setSpacing(10)

        self.btn_files = QPushButton("添加影视文件")
        self.btn_files.setObjectName("primaryBtn")
        self.btn_files.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_files.clicked.connect(self.add_files_clicked.emit)

        self.btn_folder = QPushButton("添加文件夹")
        self.btn_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_folder.clicked.connect(self.add_folder_clicked.emit)

        btn_row.addWidget(self.btn_files)
        btn_row.addWidget(self.btn_folder)
        layout.addSpacing(4)
        layout.addLayout(btn_row)
        self.set_dark_mode(True)

    def set_dark_mode(self, dark: bool) -> None:
        self._dark = dark
        self.icon_label.setPixmap(make_icon("film", "#60a5fa" if dark else "#2563eb", 44).pixmap(44, 44))
        self.btn_files.setIcon(make_icon("file_plus", "#ffffff", 15))
        self.btn_folder.setIcon(make_icon("folder_plus", "#cbd5e1" if dark else "#334155", 15))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            self.setProperty("dragHover", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.setProperty("dragHover", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("dragHover", False)
        self.style().unpolish(self)
        self.style().polish(self)
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


def get_stylesheet(theme: str = "dark") -> str:
    """Return complete polished QSS stylesheet for dark or light mode."""
    if theme == "light":
        return """
QMainWindow, QDialog {
    background-color: #f1f5f9;
    color: #0f172a;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}
QWidget {
    color: #0f172a;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
}
QFrame#topBarFrame {
    background-color: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
}
QFrame#cardFrame, QFrame#totalBarFrame {
    background-color: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
}
QFrame#telegraphCard {
    background-color: #eff6ff;
    border: 1px solid #bfdbfe;
    border-radius: 8px;
}
QFrame#dropZone {
    background-color: #ffffff;
    border: 2px dashed #cbd5e1;
    border-radius: 12px;
}
QFrame#dropZone[dragHover="true"] {
    background-color: #eff6ff;
    border-color: #2563eb;
}
QLabel#dropTitle {
    font-size: 15px;
    font-weight: 600;
    color: #0f172a;
}
QLabel#dropSub {
    font-size: 12px;
    color: #64748b;
}
QLabel#posterLabel {
    background-color: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    color: #94a3b8;
}
QPushButton {
    background-color: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 7px;
    padding: 6px 12px;
    color: #1e293b;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #f8fafc;
    border-color: #94a3b8;
}
QPushButton:pressed {
    background-color: #e2e8f0;
}
QPushButton:disabled {
    background-color: #f8fafc;
    color: #94a3b8;
    border-color: #e2e8f0;
}
QPushButton#ghostBtn {
    background-color: transparent;
    border: 1px solid transparent;
    padding: 6px 10px;
    color: #334155;
}
QPushButton#ghostBtn:hover {
    background-color: #f1f5f9;
    border-color: #e2e8f0;
}
QPushButton#primaryBtn {
    background-color: #2563eb;
    border: 1px solid #2563eb;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primaryBtn:hover {
    background-color: #1d4ed8;
}
QPushButton#dangerBtn {
    background-color: #e11d48;
    border: 1px solid #e11d48;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#dangerBtn:hover {
    background-color: #be123c;
}
QPushButton#successBtn {
    background-color: #059669;
    border: 1px solid #059669;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#successBtn:hover {
    background-color: #047857;
}
QPushButton#miniBtn {
    background-color: #f1f5f9;
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    padding: 3px 9px;
    font-size: 12px;
    color: #334155;
}
QPushButton#miniBtn:hover {
    background-color: #e2e8f0;
}
QLineEdit, QTextEdit, QComboBox, QListWidget {
    background-color: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 7px;
    padding: 6px 8px;
    color: #0f172a;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QTextEdit#ed2kBox {
    background-color: #f8fafc;
    border: 1px solid #e2e8f0;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
    color: #1e293b;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
    border-color: #2563eb;
}
QTableWidget {
    background-color: #ffffff;
    alternate-background-color: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    outline: none;
    selection-background-color: rgba(37, 99, 235, 0.12);
    selection-color: #0f172a;
}
QTableWidget::item {
    border-bottom: 1px solid #f1f5f9;
    padding: 4px 8px;
}
QTableWidget::item:hover {
    background-color: rgba(37, 99, 235, 0.06);
}
QTableWidget::item:selected {
    background-color: rgba(37, 99, 235, 0.14);
    color: #0f172a;
}
QHeaderView::section {
    background-color: #f8fafc;
    color: #475569;
    padding: 8px 8px;
    border: none;
    border-bottom: 1px solid #e2e8f0;
    font-weight: 600;
    font-size: 12px;
}
QMenu {
    background-color: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item {
    padding: 7px 16px;
    border-radius: 5px;
    color: #0f172a;
}
QMenu::item:selected {
    background-color: #eff6ff;
    color: #1d4ed8;
}
QMenu::separator {
    height: 1px;
    background: #e2e8f0;
    margin: 4px 8px;
}
QProgressBar#totalBar {
    background-color: #e2e8f0;
    border: none;
    border-radius: 4px;
    text-align: center;
    color: transparent;
    max-height: 8px;
    min-height: 8px;
}
QProgressBar#totalBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #38bdf8, stop:1 #2563eb);
    border-radius: 4px;
}
QTabWidget::pane {
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    background-color: #ffffff;
}
QTabBar::tab {
    background-color: #e2e8f0;
    color: #475569;
    padding: 8px 16px;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    margin-right: 3px;
}
QTabBar::tab:selected {
    background-color: #2563eb;
    color: #ffffff;
    font-weight: 600;
}
QStatusBar {
    background-color: #e2e8f0;
    color: #475569;
    border-top: 1px solid #cbd5e1;
}
QScrollBar:vertical {
    background: transparent;
    width: 7px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #cbd5e1;
    border-radius: 3px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: #94a3b8;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
    height: 0px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 7px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #cbd5e1;
    border-radius: 3px;
    min-width: 28px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
    width: 0px;
}
"""

    # Default: Polished Obsidian / Slate Dark Theme
    return """
QMainWindow, QDialog {
    background-color: #0b0f19;
    color: #e2e8f0;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}
QWidget {
    color: #e2e8f0;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
}
QFrame#topBarFrame {
    background-color: #111827;
    border: 1px solid #1e293b;
    border-radius: 10px;
}
QFrame#cardFrame, QFrame#totalBarFrame {
    background-color: #111827;
    border: 1px solid #1e293b;
    border-radius: 10px;
}
QFrame#telegraphCard {
    background-color: rgba(59, 130, 246, 0.10);
    border: 1px solid rgba(59, 130, 246, 0.30);
    border-radius: 8px;
}
QFrame#dropZone {
    background-color: #111827;
    border: 2px dashed #293548;
    border-radius: 12px;
}
QFrame#dropZone[dragHover="true"] {
    background-color: rgba(59, 130, 246, 0.10);
    border-color: #3b82f6;
}
QLabel#dropTitle {
    font-size: 15px;
    font-weight: 600;
    color: #f1f5f9;
}
QLabel#dropSub {
    font-size: 12px;
    color: #64748b;
}
QLabel#posterLabel {
    background-color: #0b0f19;
    border: 1px solid #1e293b;
    border-radius: 10px;
    color: #475569;
}
QPushButton {
    background-color: #1a2234;
    border: 1px solid #293548;
    border-radius: 7px;
    padding: 6px 12px;
    color: #e2e8f0;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #243047;
    border-color: #3b4b68;
}
QPushButton:pressed {
    background-color: #111827;
}
QPushButton:disabled {
    background-color: #131a29;
    color: #475569;
    border-color: #1e293b;
}
QPushButton#ghostBtn {
    background-color: transparent;
    border: 1px solid transparent;
    padding: 6px 10px;
    color: #cbd5e1;
}
QPushButton#ghostBtn:hover {
    background-color: #1e293b;
    border-color: #293548;
    color: #ffffff;
}
QPushButton#primaryBtn {
    background-color: #2563eb;
    border: 1px solid #3b82f6;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primaryBtn:hover {
    background-color: #1d4ed8;
}
QPushButton#dangerBtn {
    background-color: #e11d48;
    border: 1px solid #f43f5e;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#dangerBtn:hover {
    background-color: #be123c;
}
QPushButton#successBtn {
    background-color: #059669;
    border: 1px solid #10b981;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#successBtn:hover {
    background-color: #047857;
}
QPushButton#miniBtn {
    background-color: #1a2234;
    border: 1px solid #293548;
    border-radius: 5px;
    padding: 3px 9px;
    font-size: 12px;
    color: #cbd5e1;
}
QPushButton#miniBtn:hover {
    background-color: #243047;
    color: #ffffff;
}
QLineEdit, QTextEdit, QComboBox, QListWidget {
    background-color: #161f30;
    border: 1px solid #293548;
    border-radius: 7px;
    padding: 6px 8px;
    color: #f8fafc;
    selection-background-color: #2563eb;
}
QTextEdit#ed2kBox {
    background-color: #0b0f19;
    border: 1px solid #1e293b;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
    color: #cbd5e1;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
    border-color: #3b82f6;
}
QTableWidget {
    background-color: #111827;
    alternate-background-color: #141c2c;
    border: 1px solid #1e293b;
    border-radius: 10px;
    outline: none;
    selection-background-color: rgba(59, 130, 246, 0.22);
    selection-color: #ffffff;
}
QTableWidget::item {
    border-bottom: 1px solid #1a2335;
    padding: 4px 8px;
}
QTableWidget::item:hover {
    background-color: rgba(148, 163, 184, 0.08);
}
QTableWidget::item:selected {
    background-color: rgba(59, 130, 246, 0.22);
    color: #ffffff;
}
QHeaderView::section {
    background-color: #151d2e;
    color: #94a3b8;
    padding: 8px 8px;
    border: none;
    border-bottom: 1px solid #1e293b;
    font-weight: 600;
    font-size: 12px;
}
QMenu {
    background-color: #1a2234;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item {
    padding: 7px 16px;
    border-radius: 5px;
    color: #e2e8f0;
}
QMenu::item:selected {
    background-color: #2563eb;
    color: #ffffff;
}
QMenu::separator {
    height: 1px;
    background: #293548;
    margin: 4px 8px;
}
QProgressBar#totalBar {
    background-color: #1e293b;
    border: none;
    border-radius: 4px;
    text-align: center;
    color: transparent;
    max-height: 8px;
    min-height: 8px;
}
QProgressBar#totalBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #38bdf8, stop:1 #3b82f6);
    border-radius: 4px;
}
QTabWidget::pane {
    border: 1px solid #1e293b;
    border-radius: 8px;
    background-color: #111827;
}
QTabBar::tab {
    background-color: #1a2234;
    color: #94a3b8;
    padding: 8px 16px;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    margin-right: 3px;
}
QTabBar::tab:selected {
    background-color: #2563eb;
    color: #ffffff;
    font-weight: 600;
}
QStatusBar {
    background-color: #090d16;
    color: #64748b;
    border-top: 1px solid #1a2234;
}
QScrollBar:vertical {
    background: transparent;
    width: 7px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #293548;
    border-radius: 3px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: #475569;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
    height: 0px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 7px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #293548;
    border-radius: 3px;
    min-width: 28px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
    width: 0px;
}
"""
