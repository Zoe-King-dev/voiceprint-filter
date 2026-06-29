"""PyQt application bootstrap: tray icon + main window + Qt event loop."""
from __future__ import annotations

import logging
import math
import sys
import warnings
from logging.handlers import RotatingFileHandler

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from .app import FilterService
from .config import AppConfig
from .main_window import MainWindow
from .paths import PathResolver
from .theme import Colors

log = logging.getLogger(__name__)


# ----- Tray icon ----------------------------------------------------------

# Six states the tray icon can communicate. Centralized so the icon painter
# and the state→color mapping can't drift apart.
_TRAY_STATES = {
    "ok":        (Colors.STATE_OK,       "V"),  # actively filtering, voice is mine
    "watch":     (Colors.STATE_WARN,     "V"),  # paused by user
    "degraded":  (Colors.STATE_DEGRADED, "!"),  # device missing
    "off":       (Colors.STATE_NEUTRAL,  "V"),  # stopped / not started
    "no_enroll": (Colors.STATE_NEUTRAL,  "?"),  # never enrolled
}


def _make_tray_icon(state: str, pulse: float = 1.0) -> QIcon:
    """Generate a polished tray icon programmatically (no asset files).

    Design: a filled core circle + a glowing outer ring whose alpha is driven
    by ``pulse`` (0..1). When ``pulse`` is animated down, the ring fades —
    a gentle "breathing" effect that signals "I'm working" without being noisy.
    """
    color_hex, letter = _TRAY_STATES.get(state, _TRAY_STATES["off"])

    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Outer ring — alpha animated. base 0.25 + 0.55*pulse keeps it always
    # visible (so the icon never "disappears") but obviously breathing.
    ring = QColor(color_hex)
    ring.setAlphaF(max(0.0, min(1.0, 0.25 + 0.55 * pulse)))
    pen = QPen(ring)
    pen.setWidth(3)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(4, 4, size - 8, size - 8)

    # Core circle
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color_hex))
    p.drawEllipse(12, 12, size - 24, size - 24)

    # Letter
    p.setPen(QColor("white"))
    font = p.font()
    font.setBold(True)
    font.setPointSize(22 if letter == "V" else 26)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    p.end()
    return QIcon(pm)


class _TrayPulse:
    """Drives the running state's breathing-ring alpha.

    A 1200 ms period was picked because faster reads as "alarm", slower as
    "stalled". Phase advances every 60 ms (~17 fps) — plenty for an alpha
    change on a 64×64 pixmap; Qt's QPixmap cache keeps it cheap.
    """

    PERIOD_MS = 1200
    TICK_MS = 60

    def __init__(self, tray: QSystemTrayIcon):
        self._tray = tray
        self._phase = 0.0
        self._timer = QTimer()
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._active = False

    def start(self) -> None:
        self._active = True
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._active = False
        # Final frame: full opacity so the resting state looks intentional.
        self._tray.setIcon(_make_tray_icon("ok", pulse=1.0))

    def _tick(self) -> None:
        self._phase = (self._phase + self.TICK_MS / self.PERIOD_MS * 2 * math.pi) % (
            2 * math.pi
        )
        # Ease-in-out cosine — softer than raw sine.
        s = 0.5 - 0.5 * math.cos(self._phase)
        self._tray.setIcon(_make_tray_icon("ok", pulse=s))


class VoiceprintTrayApp(QObject):
    def __init__(self, cfg: AppConfig, resolver: PathResolver):
        super().__init__()
        self.cfg = cfg
        self.resolver = resolver
        self.service = FilterService(cfg, resolver.project_root)
        self.window = MainWindow(self.service)
        self._build_tray()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(
            _make_tray_icon("no_enroll" if not self.service.has_enrollment() else "off")
        )
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

        self.pulse = _TrayPulse(self.tray)
        self.service.state_changed.connect(self._sync_tray_state)
        self.service.error.connect(self._on_error)
        self.service.enrollment_completed.connect(self._on_enrollment_completed)

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
        # Map service-state text → tray icon state. Same precedence rules as
        # the in-window badge so the two visual cues can never disagree.
        if "丢失" in msg or ("等待" in msg and "恢复" in msg):
            state = "degraded"
            self.pulse.stop()
        elif "运行" in msg or "已恢复" in msg:
            state = "ok"
            self.pulse.start()
        elif "已注册" in msg:
            # Enrolled but not filtering yet — show a calm OK-color icon
            # without the breathing pulse (pulse means "audio is flowing",
            # which it isn't here).
            state = "off"
            self.pulse.stop()
        elif "暂停" in msg:
            state = "watch"
            self.pulse.stop()
        else:  # "已停止", "未启动", …
            state = "off"
            self.pulse.stop()

        self.tray.setIcon(_make_tray_icon(state))
        self.tray.setToolTip(f"Voiceprint Filter — {msg}")

    def _on_enrollment_completed(self) -> None:
        """Fresh enrollment saved. Toast the user via the tray so they see
        an unambiguous success even if the enrollment dialog / main window
        is minimized. This is the fix for the "注册完看不到任何进展" problem
        -- previously the only feedback was a green "运行中" badge with no
        streams running, which read as "stuck" for an hour.
        """
        if self.tray.supportsMessages():
            self.tray.showMessage(
                "Voiceprint Filter",
                "✅ 声纹注册成功。点击「启动过滤」开始使用。",
                QSystemTrayIcon.MessageIcon.Information,
            )
        # Make sure the tray icon is not still stuck on the "?" no_enroll
        # state from first launch -- _sync_tray_state will have run via the
        # state_changed signal mark_enrollment_loaded emits, but be explicit.
        if not self.service.is_running():
            self.tray.setIcon(_make_tray_icon("off"))
            self.tray.setToolTip("Voiceprint Filter — 已注册 — 等待启动过滤")

    def _on_error(self, msg: str) -> None:
        if self.tray.supportsMessages():
            self.tray.showMessage(
                "Voiceprint Filter 错误",
                msg,
                QSystemTrayIcon.MessageIcon.Critical,
            )

    def _quit(self) -> None:
        self.pulse.stop()
        self.service.stop()
        QApplication.instance().quit()

    def run(self, show_window: bool = False) -> int:
        # Show the main window on first launch (no enrollment yet), or when
        # the caller explicitly asks (e.g. `python main.py --show` during
        # dev/test). Otherwise the app starts minimized to the tray -- which
        # is correct for a frozen exe in normal use but confusing when you're
        # iterating on the UI and expect to see something on launch.
        if show_window or not self.service.has_enrollment():
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
            if not self.service.has_enrollment():
                self.window.status_badge.set_state(
                    "●  首次启动 — 请先注册声纹。",
                    Colors.STATE_WARN,
                )
        return QApplication.instance().exec()


def _setup_logging(resolver: PathResolver, level: str) -> None:
    """Console handler + rotating file handler under the user-data log dir.

    The file handler is the durable record a non-technical user ships to a
    bug report via the tray's "view logs" / "report problem" actions. In
    dev mode it lands under ``<repo>/logs/``; frozen, under
    ``%APPDATA%/voiceprint-filter/logs/``.
    """
    root = logging.getLogger()
    root.setLevel(level)
    # Wipe any prior basicConfig handlers so re-entry (e.g. tests) stays clean.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        log_path = resolver.user_data("logs", "voiceprint-filter.log")
        file_handler = RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        log.info("Log file: %s", log_path)
    except Exception:  # pragma: no cover — never let logging setup kill startup
        log.exception("Could not attach file log handler; continuing with console only.")


def run() -> int:
    """Module-level entry point used by main.py."""
    # Silence pydantic's 'Field "model_path" has conflict with protected namespace
    # "model_"' warning. We've already opted every config class out of the namespace
    # (ConfigDict(protected_namespaces=())), but PyInstaller's frozen bundle appears
    # to instantiate the models via a path that re-triggers the warning -- the
    # warning is a false positive for our field naming. Without this filter, every
    # launch prints a red stderr line that non-technical users see as a crash.
    warnings.filterwarnings(
        "ignore",
        message=r".*protected namespace.*",
        category=UserWarning,
    )

    from .config import AppConfig as _Cfg
    from .theme import apply_theme

    resolver = PathResolver()
    cfg = _Cfg.load(resolver)
    _setup_logging(resolver, cfg.log_level)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # tray keeps the app alive
    apply_theme(app)

    tray_app = VoiceprintTrayApp(cfg, resolver)
    return tray_app.run()
