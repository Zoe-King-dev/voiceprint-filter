"""Centralized UI design tokens, global QSS, and reusable themed widgets.

Why this module exists:
    PyQt6 ships no design system. Without central tokens, every widget picks its
    own colors and spacing, the result looks like 1990s Win32. This file defines
    the project's color/spacing/typography scale once and exports:

    * :func:`apply_theme` — call once at startup with the ``QApplication``.
    * :class:`StatusBadge` — colored-dot + text label used in the status bar.
    * :class:`ScoreBar` — animated progress bar whose chunk color crossfades
      instead of snapping.
    * :class:`SectionCard` — ``QFrame`` wrapper that gives the visual grouping
      the rest of the UI is built out of.

Design choices:
    * Dark surfaces, light text — matches "monitoring" / DAW aesthetic and
      makes the colored states (green me / red other / grey silence) pop.
    * Single accent (``#4f9dff``) so eye is drawn to interactive controls.
    * Motion budget: 180–240ms ease-out for transitions, 1200ms for tray
      pulse. Anything longer feels sluggish for a tool the user opens often.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    pyqtProperty,
)
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

class Colors:
    """Semantic color palette. Single source of truth — never hardcode hex."""

    # Surfaces (backgrounds)
    BG_BASE = "#1e2230"        # window background
    BG_PANEL = "#262b3b"       # section card
    BG_PANEL_ALT = "#2e3447"   # hovered / active section
    BG_INPUT = "#181c28"       # text fields, log view

    # Text
    TEXT_PRIMARY = "#e6e9f2"
    TEXT_SECONDARY = "#9aa3b8"
    TEXT_MUTED = "#6b7388"

    # Borders
    BORDER = "#353c52"
    BORDER_FOCUS = "#4f9dff"

    # Accent (interactive)
    ACCENT = "#4f9dff"
    ACCENT_HOVER = "#6cb0ff"
    ACCENT_PRESSED = "#3a8bef"

    # State semantics — used by ScoreBar + StatusBadge
    STATE_OK = "#5dd39e"       # me / running
    STATE_WARN = "#f3b664"     # threshold zone / degraded
    STATE_BAD = "#ec6a6a"      # other / error
    STATE_NEUTRAL = "#5a6178"  # silent / paused / disabled
    STATE_DEGRADED = "#c98aff" # device missing


class Spacing:
    """4-pt spacing scale. Stick to these so things line up."""

    XS = 4
    SM = 8
    MD = 12
    LG = 16
    XL = 24


class Radius:
    CARD = 8
    BUTTON = 6
    INPUT = 4


# ---------------------------------------------------------------------------
# Global QSS
# ---------------------------------------------------------------------------

GLOBAL_QSS = f"""
/* ----- Root ----- */
QMainWindow, QDialog {{
    background-color: {Colors.BG_BASE};
}}

/* ----- Labels ----- */
QLabel {{
    color: {Colors.TEXT_PRIMARY};
    background: transparent;
}}
QLabel[role="muted"] {{
    color: {Colors.TEXT_MUTED};
}}
QLabel[role="secondary"] {{
    color: {Colors.TEXT_SECONDARY};
}}

/* ----- Inputs ----- */
QComboBox, QDoubleSpinBox, QSpinBox {{
    background-color: {Colors.BG_INPUT};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER};
    border-radius: {Radius.INPUT}px;
    padding: 5px 8px;
    selection-background-color: {Colors.ACCENT};
}}
QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover {{
    border-color: {Colors.ACCENT};
}}
QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {Colors.ACCENT};
}}
QComboBox QAbstractItemView {{
    background-color: {Colors.BG_PANEL};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER};
    selection-background-color: {Colors.ACCENT};
    outline: 0;
}}

/* ----- Buttons ----- */
QPushButton {{
    background-color: {Colors.BG_PANEL};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER};
    border-radius: {Radius.BUTTON}px;
    padding: 6px 14px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {Colors.BG_PANEL_ALT};
    border-color: {Colors.ACCENT};
}}
QPushButton:pressed {{
    background-color: {Colors.ACCENT_PRESSED};
    border-color: {Colors.ACCENT_PRESSED};
}}
QPushButton:disabled {{
    color: {Colors.TEXT_MUTED};
    background-color: {Colors.BG_BASE};
    border-color: {Colors.BORDER};
}}
QPushButton[role="primary"] {{
    background-color: {Colors.ACCENT};
    border-color: {Colors.ACCENT};
    color: white;
}}
QPushButton[role="primary"]:hover {{
    background-color: {Colors.ACCENT_HOVER};
    border-color: {Colors.ACCENT_HOVER};
}}
QPushButton[role="primary"]:pressed {{
    background-color: {Colors.ACCENT_PRESSED};
}}
QPushButton[role="primary"]:disabled {{
    background-color: {Colors.STATE_NEUTRAL};
    border-color: {Colors.STATE_NEUTRAL};
}}

/* ----- Progress bars ----- */
QProgressBar {{
    background-color: {Colors.BG_INPUT};
    border: 1px solid {Colors.BORDER};
    border-radius: {Radius.INPUT}px;
    text-align: center;
    color: {Colors.TEXT_PRIMARY};
    min-height: 14px;
}}
QProgressBar::chunk {{
    background-color: {Colors.STATE_NEUTRAL};
    border-radius: 3px;
    margin: 1px;
}}

/* ----- Text edit / log ----- */
QTextEdit {{
    background-color: {Colors.BG_INPUT};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER};
    border-radius: {Radius.INPUT}px;
    padding: 6px;
    selection-background-color: {Colors.ACCENT};
}}

/* ----- Status bar ----- */
QStatusBar {{
    background-color: {Colors.BG_PANEL};
    color: {Colors.TEXT_SECONDARY};
    border-top: 1px solid {Colors.BORDER};
}}
QStatusBar::item {{
    border: none;
}}

/* ----- Section card frame ----- */
QFrame[role="card"] {{
    background-color: {Colors.BG_PANEL};
    border: 1px solid {Colors.BORDER};
    border-radius: {Radius.CARD}px;
}}

/* ----- Section title ----- */
QLabel[role="section-title"] {{
    color: {Colors.TEXT_SECONDARY};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
    padding-top: 2px;
}}

/* ----- Menus (tray) ----- */
QMenu {{
    background-color: {Colors.BG_PANEL};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER};
    border-radius: {Radius.INPUT}px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 18px;
    border-radius: {Radius.INPUT}px;
}}
QMenu::item:selected {{
    background-color: {Colors.ACCENT};
}}
QMenu::separator {{
    height: 1px;
    background: {Colors.BORDER};
    margin: 4px 8px;
}}

/* ----- Tooltip ----- */
QToolTip {{
    background-color: {Colors.BG_PANEL_ALT};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER};
    border-radius: 4px;
    padding: 4px 6px;
}}
"""


def apply_theme(app: QApplication) -> None:
    """Apply the global QSS + a slightly nicer default font."""
    app.setStyleSheet(GLOBAL_QSS)
    # Force the Fusion style on Windows — gives consistent look regardless of
    # Windows theme and matches the QSS-defined colors exactly.
    app.setStyle("Fusion")
    font = QFont("Segoe UI", 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)


# ---------------------------------------------------------------------------
# Status badge — colored dot + text, used in status bar
# ---------------------------------------------------------------------------

class StatusBadge(QLabel):
    """A small colored dot followed by a status string.

    The dot is painted manually with :class:`QPainter` rather than via QSS so
    that we can animate its color smoothly with :class:`QPropertyAnimation`.
    """

    def __init__(self, text: str = "—", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setMinimumWidth(220)
        self._dot_color = QColor(Colors.STATE_NEUTRAL)
        self._anim = QPropertyAnimation(self, b"dotColor", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # Expose dotColor as a Qt property so QPropertyAnimation can drive it.
    def get_dot_color(self) -> QColor:
        return self._dot_color

    def set_dot_color(self, c: QColor) -> None:
        self._dot_color = QColor(c)
        self.update()

    dotColor = pyqtProperty(QColor, fget=get_dot_color, fset=set_dot_color)

    def set_state(self, text: str, color_hex: str) -> None:
        """Smoothly transition the dot color and update the text label."""
        self._anim.stop()
        self._anim.setStartValue(self._dot_color)
        self._anim.setEndValue(QColor(color_hex))
        self._anim.start()
        self.setText(text)

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt naming
        # Let QLabel draw the text first, then paint the dot on the left.
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(self._dot_color)
        p.setPen(Qt.PenStyle.NoPen)
        # Place the dot at the left margin, vertically centered with the text.
        d = 10
        p.drawEllipse(2, (self.height() - d) // 2, d, d)
        p.end()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        s = super().minimumSizeHint()
        return QSize(s.width() + 16, s.height())


# ---------------------------------------------------------------------------
# ScoreBar — animated color crossfade on the chunk
# ---------------------------------------------------------------------------

class ScoreBar(QProgressBar):
    """A progress bar whose ``::chunk`` color animates between states.

    Without this, the score bar snaps from green→red→grey on every state
    change, which feels mechanical. We animate the chunk's background color
    with QPropertyAnimation so the transition reads as a smooth hue shift.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._chunk_color = QColor(Colors.STATE_NEUTRAL)
        self._anim = QPropertyAnimation(self, b"chunkColor", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_chunk_color(self) -> QColor:
        return self._chunk_color

    def set_chunk_color(self, c: QColor) -> None:
        self._chunk_color = QColor(c)
        # Build a QSS fragment just for this widget; cheaper than rebuilding
        # the whole global QSS and scopes the change correctly.
        self.setStyleSheet(
            f"QProgressBar{{background-color:{Colors.BG_INPUT};"
            f"border:1px solid {Colors.BORDER};border-radius:4px;"
            f"text-align:center;color:{Colors.TEXT_PRIMARY};}}"
            f"QProgressBar::chunk{{background-color:{self._chunk_color.name()};"
            f"border-radius:3px;margin:1px;}}"
        )

    chunkColor = pyqtProperty(QColor, fget=get_chunk_color, fset=set_chunk_color)

    def set_state_color(self, hex_color: str) -> None:
        """Animate the chunk to ``hex_color`` instead of snapping."""
        self._anim.stop()
        self._anim.setStartValue(self._chunk_color)
        self._anim.setEndValue(QColor(hex_color))
        self._anim.start()


# ---------------------------------------------------------------------------
# Section card — visual grouping container
# ---------------------------------------------------------------------------

class SectionCard(QFrame):
    """A rounded, faintly tinted frame that groups related controls.

    Visual grouping reduces cognitive load: instead of 8 loose widgets on one
    canvas, the user sees three labeled clusters (设备 / 控制 / 判定).
    """

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setProperty("role", "card")
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.MD)
        layout.setSpacing(Spacing.SM)

        title_label = QLabel(title.upper())
        title_label.setProperty("role", "section-title")
        layout.addWidget(title_label)

        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(Spacing.SM)
        layout.addWidget(self.body)

    def add_row(self, widgets: list[QWidget]) -> QHBoxLayout:
        """Convenience: add a horizontal row of widgets to the body."""
        row = QHBoxLayout()
        row.setSpacing(Spacing.SM)
        for w in widgets:
            row.addWidget(w)
        self.body_layout.addLayout(row)
        return row

    def add_widget(self, w: QWidget) -> None:
        self.body_layout.addWidget(w)
