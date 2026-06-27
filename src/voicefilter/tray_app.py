"""PyQt application bootstrap: tray icon + main window + Qt event loop."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from .app import FilterService
from .config import AppConfig
from .main_window import MainWindow

log = logging.getLogger(__name__)


def _make_tray_icon(active: bool) -> QIcon:
    """Generate a simple colored-circle icon (no asset files needed)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#3a3" if active else "#888"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(8, 8, 48, 48)
    p.setPen(QColor("white"))
    font = p.font()
    font.setBold(True)
    font.setPointSize(20)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "V")
    p.end()
    return QIcon(pm)


class VoiceprintTrayApp(QObject):
    def __init__(self, cfg: AppConfig, project_root: Path):
        super().__init__()
        self.cfg = cfg
        self.service = FilterService(cfg, project_root)
        self.window = MainWindow(self.service)
        self._build_tray()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(_make_tray_icon(self.service.has_enrollment()))
        self.tray.setToolTip("Voiceprint Filter")

        menu = QMenu()
        self.act_show = QAction("打开主面板")
        self.act_show.triggered.connect(self._show_window)
        self.act_pause = QAction("暂停过滤", checkable=True)
        self.act_pause.toggled.connect(self._on_pause_toggled)
        self.act_enroll = QAction("注册 / 重新注册声纹…")
        self.act_enroll.triggered.connect(self.window._on_enroll)
        self.act_quit = QAction("退出")
        self.act_quit.triggered.connect(self._quit)
        menu.addAction(self.act_show)
        menu.addAction(self.act_pause)
        menu.addSeparator()
        menu.addAction(self.act_enroll)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.service.state_changed.connect(self._sync_tray_state)
        self.service.error.connect(self._on_error)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_window()

    def _show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _on_pause_toggled(self, checked: bool) -> None:
        if checked:
            self.service.pause()
        else:
            self.service.resume()

    def _sync_tray_state(self, msg: str) -> None:
        active = "运行" in msg or "已恢复" in msg
        self.tray.setIcon(_make_tray_icon(active))
        self.tray.setToolTip(f"Voiceprint Filter — {msg}")

    def _on_error(self, msg: str) -> None:
        if self.tray.supportsMessages():
            self.tray.showMessage("Voiceprint Filter 错误", msg, QSystemTrayIcon.MessageIcon.Critical)

    def _quit(self) -> None:
        self.service.stop()
        QApplication.instance().quit()

    def run(self) -> int:
        # Show window on first launch if no enrollment yet
        if not self.service.has_enrollment():
            self.window.show()
            self.window.status_label.setText("首次启动 — 请先注册声纹。")
        return QApplication.instance().exec()


def run() -> int:
    """Module-level entry point used by main.py."""
    from .config import AppConfig as _Cfg

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    project_root = Path(__file__).resolve().parents[2]
    cfg = _Cfg.load(project_root)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # tray keeps the app alive

    tray_app = VoiceprintTrayApp(cfg, project_root)
    return tray_app.run()