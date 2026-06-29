"""Main control window — device pickers, threshold slider, live score bar."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QTimer,
    Qt,
)
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .app import FilterService
from .enrollment import EnrollmentError, EnrollmentWizard, PROMPT_TEXT
from .theme import Colors, ScoreBar, SectionCard, Spacing, StatusBadge

log = logging.getLogger(__name__)


# Animation durations are picked to feel "responsive but not jumpy":
#   - dialog entrance: 240ms — enough to register motion, short enough that
#     the user isn't kept waiting to interact.
#   - score bar color: 180ms — must finish before the next hop (500ms) so a
#     fast speaker doesn't get mid-transition snaps.
_ANIM_DIALOG_MS = 240
_ANIM_SCORE_MS = 180


def _fade_in(widget: QWidget, duration_ms: int = _ANIM_DIALOG_MS,
             rise_px: int = 12) -> None:
    """Play a 'soft entrance' — fade from 0→1 and rise by ``rise_px``.

    This is the closest thing to a Framer-Motion ``initial={{opacity:0, y:12}}
    animate={{opacity:1, y:0}}`` in PyQt6. We use QPropertyAnimation for both
    the opacity (via QGraphicsOpacityEffect) and the window geometry.
    """
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)

    fade = QPropertyAnimation(eff, b"opacity", widget)
    fade.setDuration(duration_ms)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    end_pos = widget.pos()
    start_pos = QPoint(end_pos.x(), end_pos.y() + rise_px)
    widget.move(start_pos)
    slide = QPropertyAnimation(widget, b"pos", widget)
    slide.setDuration(duration_ms)
    slide.setStartValue(start_pos)
    slide.setEndValue(end_pos)
    slide.setEasingCurve(QEasingCurve.Type.OutCubic)

    fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    slide.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    # Keep references so GC doesn't kill the animations mid-flight.
    widget._fade_anim = fade  # type: ignore[attr-defined]
    widget._slide_anim = slide  # type: ignore[attr-defined]


class EnrollmentDialog(QMainWindow):
    """Simple modal-ish dialog for guided enrollment."""

    def __init__(self, service: FilterService, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("声纹注册 — Voiceprint Enrollment")
        self.resize(640, 480)

        self._wizard: Optional[EnrollmentWizard] = None
        self._thread = None

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        layout.setSpacing(Spacing.MD)

        intro = QLabel(
            "首次使用,请用日常说话音量朗读下方文本。\n"
            "录满 20 秒会自动结束;也可以读完后点「完成录制」提前结束(至少 5 秒)。\n"
            "建议在安静环境、与会议时同一只麦克风下录制。\n"
            "⚠ 请选择你的真实麦克风(如「麦克风」「Headset Mic」)——\n"
            "VB-CABLE 虚拟端点不支持反向录音。"
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "secondary")
        layout.addWidget(intro)

        prompt = QTextEdit()
        prompt.setPlainText(PROMPT_TEXT)
        prompt.setReadOnly(True)
        prompt.setStyleSheet("font-size: 14px; padding: 8px;")
        layout.addWidget(prompt, 1)

        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setFormat("麦克风音量 %v")
        layout.addWidget(self.level)

        self.progress = QProgressBar()
        self.progress.setRange(0, 20)
        self.progress.setFormat("录制进度 %v 秒")
        layout.addWidget(self.progress)

        self.status = QLabel("准备就绪。")
        self.status.setProperty("role", "secondary")
        layout.addWidget(self.status)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(Spacing.SM)
        self.btn_mic = QComboBox()
        for d in service.list_input_devices():
            self.btn_mic.addItem(f"[{d.idx}] {d.name}", userData=d)
        btn_row.addWidget(QLabel("麦克风:"))
        btn_row.addWidget(self.btn_mic, 1)

        self.btn_start = QPushButton("开始录制")
        self.btn_start.setProperty("role", "primary")
        self.btn_start.clicked.connect(self._start)
        btn_row.addWidget(self.btn_start)

        self.btn_finish = QPushButton("完成录制")
        self.btn_finish.setProperty("role", "primary")
        self.btn_finish.setEnabled(False)
        self.btn_finish.setToolTip("录够5秒以上即可提前结束。")
        self.btn_finish.clicked.connect(self._finish)
        btn_row.addWidget(self.btn_finish)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        btn_row.addWidget(self.btn_cancel)

        layout.addLayout(btn_row)

        # Defer the entrance animation until after the dialog is on-screen so
        # its initial geometry is set; otherwise the slide starts from a stale
        # position computed before show().
        QTimer.singleShot(0, lambda: _fade_in(self))

    def _start(self) -> None:
        dev = self.btn_mic.currentData()
        if dev is None:
            QMessageBox.warning(self, "未选麦克风", "请先选择一个麦克风。")
            return
        self.btn_start.setEnabled(False)
        self.btn_mic.setEnabled(False)
        self.btn_finish.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        self.progress.setValue(0)
        self.level.setValue(0)
        self.status.setText("正在录制...请开始朗读。")

        from .enrollment import EnrollmentRecorder
        from .speaker_engine import SpeakerEngine

        engine = SpeakerEngine(
            self.service.cfg.speaker.model_path,
            threshold=self.service.cfg.speaker.threshold,
        )
        rec = EnrollmentRecorder(duration_sec=20, sample_rate=self.service.cfg.audio.sample_rate)
        self._wizard = EnrollmentWizard(
            engine=engine,
            recorder=rec,
            on_progress=self._on_progress,
        )

        import threading

        def worker():
            try:
                self._wizard.run(dev.idx, self.service.embedding_path())
            except EnrollmentError as e:
                self.status.setText(f"失败: {e}")
                self.btn_start.setEnabled(True)
                self.btn_mic.setEnabled(True)
                self.btn_finish.setEnabled(False)
                self.btn_cancel.setEnabled(False)
                return
            except Exception as e:  # pragma: no cover
                log.exception("Enrollment worker crashed")
                self.status.setText(f"内部错误: {e}")
                self.btn_start.setEnabled(True)
                self.btn_mic.setEnabled(True)
                self.btn_finish.setEnabled(False)
                self.btn_cancel.setEnabled(False)
                return
            # Re-load into service's engine and fire the enrollment-completed
            # signal so the main window surfaces a clear success state. Do NOT
            # call service.resume() here -- if no pipeline is running yet that
            # would emit a misleading "运行中" with no streams attached.
            self.service.mark_enrollment_loaded()
            self.status.setText("✅ 注册完成！可以关闭此窗口。")
            self.btn_start.setText("重新录制")
            self.btn_start.setEnabled(True)
            self.btn_mic.setEnabled(True)
            self.btn_finish.setEnabled(False)
            self.btn_cancel.setEnabled(False)

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _cancel(self) -> None:
        if self._wizard is not None:
            self._wizard.cancel()

    def _finish(self) -> None:
        # Tell the worker to stop capturing at the next chunk boundary and
        # proceed to embedding extraction. Disabled immediately so the user
        # can't double-tap it; the worker re-enables buttons on completion.
        if self._wizard is not None:
            self.btn_finish.setEnabled(False)
            self.status.setText("正在结束录制,提取声纹...")
            self._wizard.finish()

    def _on_progress(self, elapsed: float, chunk) -> None:
        # update from worker thread — use Qt signals in production, but
        # Qt widgets are tolerant of cross-thread setValue() for progress bars.
        from .utils.power import rms_db

        rms = rms_db(chunk)
        # Map -60..0 dB → 0..100
        lvl = max(0, min(100, int((rms + 60) * 100 / 60)))
        self.level.setValue(lvl)
        self.progress.setValue(int(elapsed))


class MainWindow(QMainWindow):
    def __init__(self, service: FilterService):
        super().__init__()
        self.service = service
        self.setWindowTitle("声纹过滤 — Voiceprint Filter")
        self.resize(760, 600)
        self._build_ui()
        self._wire_signals()
        self._refresh_status()
        self._poll_stats = QTimer(self)
        self._poll_stats.timeout.connect(self._refresh_stats)
        self._poll_stats.start(200)

    # --- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        root.setSpacing(Spacing.MD)

        # ----- Devices section -----
        dev_card = SectionCard("设备", self)
        dev_row = QHBoxLayout()
        dev_row.setSpacing(Spacing.SM)
        self.in_combo = QComboBox()
        self.out_combo = QComboBox()
        self._populate_devices()
        dev_row.addWidget(QLabel("真实麦克风:"))
        dev_row.addWidget(self.in_combo, 1)
        dev_row.addSpacing(Spacing.MD)
        dev_row.addWidget(QLabel("虚拟麦克风:"))
        dev_row.addWidget(self.out_combo, 1)
        dev_card.body_layout.addLayout(dev_row)

        if not self.service.has_cable():
            self.warn_label = QLabel(
                "⚠ 未检测到 VB-CABLE 设备。请从 vb-audio.com/Cable 下载安装,"
                "然后重启电脑,再回到本程序。"
            )
            self.warn_label.setWordWrap(True)
            self.warn_label.setStyleSheet(
                f"color: {Colors.STATE_WARN}; padding: 6px 8px;"
                f"background: rgba(243,182,100,0.08);"
                f"border-radius: 4px;"
            )
            dev_card.body_layout.addWidget(self.warn_label)

        root.addWidget(dev_card)

        # ----- Controls section -----
        ctrl_card = SectionCard("控制", self)
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(Spacing.SM)
        self.btn_start = QPushButton("启动过滤")
        self.btn_start.setProperty("role", "primary")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(lambda: self._on_stop())
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._on_pause_toggled)
        self.btn_enroll = QPushButton("注册声纹…")
        self.btn_enroll.clicked.connect(self._on_enroll)
        ctrl_row.addWidget(self.btn_start)
        ctrl_row.addWidget(self.btn_stop)
        ctrl_row.addWidget(self.btn_pause)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self.btn_enroll)
        ctrl_card.body_layout.addLayout(ctrl_row)
        root.addWidget(ctrl_card)

        # ----- Score / decision section -----
        score_card = SectionCard("声纹判定", self)
        score_caption = QLabel("实时声纹相似度")
        score_caption.setProperty("role", "muted")
        score_card.body_layout.addWidget(score_caption)

        self.score_bar = ScoreBar()
        self.score_bar.setRange(-100, 100)
        self.score_bar.setValue(0)
        self.score_bar.setFormat("%v / 100")
        score_card.body_layout.addWidget(self.score_bar)

        self.score_value = QLabel("—")
        self.score_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.score_value.setProperty("role", "secondary")
        score_card.body_layout.addWidget(self.score_value)

        thr_row = QHBoxLayout()
        thr_row.setSpacing(Spacing.SM)
        thr_row.addWidget(QLabel("判定阈值:"))
        self.thr_spin = QDoubleSpinBox()
        self.thr_spin.setRange(0.30, 0.95)
        self.thr_spin.setSingleStep(0.01)
        self.thr_spin.setValue(self.service.cfg.speaker.threshold)
        self.thr_spin.valueChanged.connect(self._on_threshold_changed)
        thr_row.addWidget(self.thr_spin)
        thr_row.addStretch(1)
        thr_row.addWidget(QLabel("他人衰减 (dB):"))
        self.other_gain = QDoubleSpinBox()
        self.other_gain.setRange(-60.0, 0.0)
        self.other_gain.setSingleStep(1.0)
        self.other_gain.setValue(self.service.cfg.speaker.other_gain_db)
        self.other_gain.valueChanged.connect(self._on_other_gain_changed)
        thr_row.addWidget(self.other_gain)
        score_card.body_layout.addLayout(thr_row)

        root.addWidget(score_card)

        # ----- Log section -----
        log_card = SectionCard("日志", self)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        log_card.body_layout.addWidget(self.log_view)
        root.addWidget(log_card, 1)

        # ----- Status bar with badge -----
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_badge = StatusBadge("●  未启动")
        sb.addWidget(self.status_badge)
        self.status_metrics = QLabel("")
        self.status_metrics.setProperty("role", "secondary")
        self.status_metrics.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; padding-right: 8px;")
        sb.addPermanentWidget(self.status_metrics)

    def _populate_devices(self) -> None:
        for d in self.service.list_input_devices():
            self.in_combo.addItem(f"[{d.idx}] {d.name}", userData=d.name)
        for d in self.service.list_output_devices():
            self.out_combo.addItem(f"[{d.idx}] {d.name}", userData=d.name)
        # Best-effort defaults
        self._set_default(self.in_combo, self.service.cfg.audio.input_device_substring)
        self._set_default(self.out_combo, self.service.cfg.audio.output_device_substring)

    @staticmethod
    def _set_default(combo: QComboBox, needle: str) -> None:
        for i in range(combo.count()):
            if needle.lower() in combo.itemText(i).lower():
                combo.setCurrentIndex(i)
                return

    def _wire_signals(self) -> None:
        self.service.stats_changed.connect(self._on_stats)
        self.service.state_changed.connect(self._on_state)
        self.service.error.connect(self._on_error)
        self.service.log.connect(self._append_log)
        self.service.enrollment_completed.connect(self._on_enrollment_completed)

    # --- slots ------------------------------------------------------------

    def _on_start(self) -> None:
        in_sub = self.in_combo.currentData() or self.service.cfg.audio.input_device_substring
        out_sub = self.out_combo.currentData() or self.service.cfg.audio.output_device_substring
        self.service.start(in_sub, out_sub)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.in_combo.setEnabled(False)
        self.out_combo.setEnabled(False)

    def _on_stop(self) -> None:
        self.service.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setChecked(False)
        self.in_combo.setEnabled(True)
        self.out_combo.setEnabled(True)

    def _on_pause_toggled(self, checked: bool) -> None:
        if checked:
            self.service.pause()
        else:
            self.service.resume()

    def _on_enroll(self) -> None:
        dlg = EnrollmentDialog(self.service, parent=self)
        dlg.show()

    def _on_threshold_changed(self, v: float) -> None:
        self.service.set_threshold(v)

    def _on_other_gain_changed(self, v: float) -> None:
        self.service.set_other_gain_db(v)

    def _on_stats(self, stats) -> None:
        score_pct = int(max(-1.0, min(1.0, stats.last_score)) * 100)
        self.score_bar.setValue(score_pct)
        thr_pct = int(self.service.cfg.speaker.threshold * 100)
        # Pick a semantic state color rather than embedding raw hex in slots.
        if not stats.last_is_speech:
            color = Colors.STATE_NEUTRAL
            label = "静音"
        elif stats.last_is_me:
            color = Colors.STATE_OK
            label = "ME"
        else:
            color = Colors.STATE_BAD
            label = "OTHER"
        # Smoothly crossfade the score-bar chunk color instead of snapping.
        self.score_bar.set_state_color(color)

        if stats.last_is_speech:
            self.score_value.setText(
                f"score={stats.last_score:+.3f}  thr={self.service.cfg.speaker.threshold:.2f}  "
                f"{label}  infer={stats.last_infer_ms:.0f}ms"
            )
        else:
            self.score_value.setText("静音")
        self.status_metrics.setText(
            f"frames={stats.frames_processed}  me={stats.accepted_frames}  "
            f"rej={stats.rejected_frames}  gain={stats.last_gain_db:.1f}dB"
        )

    def _on_state(self, msg: str) -> None:
        # Map service state text → (badge text, color). Centralizing this here
        # means new states only need one entry instead of scattered updates.
        text = f"●  {msg}"
        if ("丢失" in msg) or ("等待" in msg and "恢复" in msg):
            color = Colors.STATE_DEGRADED
        elif "运行" in msg or "已恢复" in msg:
            color = Colors.STATE_OK
        elif "已注册" in msg:
            # Just finished enrollment but not filtering yet. OK color says
            # "you succeeded" without the "运行中" green that implies audio
            # is flowing -- which it isn't until the user hits 启动过滤.
            color = Colors.STATE_OK
        elif "暂停" in msg:
            color = Colors.STATE_WARN
        else:  # "已停止", "未启动", "未注册", …
            color = Colors.STATE_NEUTRAL
        self.status_badge.set_state(text, color)

    def _on_enrollment_completed(self) -> None:
        """Fresh enrollment saved + loaded. Surface an unambiguous success.

        Before this signal existed, the enrollment dialog called
        service.resume() which (with no pipeline running) emitted "运行中"
        and painted the status badge green -- while no audio was actually
        flowing. Users saw a green "运行中" that never advanced for an hour
        and assumed the app had silently failed. Now we instead show a
        clear "注册成功,下一步启动过滤" prompt and nudge the start button.
        """
        # Clear the pre-enrollment warning styling on the score line.
        self.score_value.setStyleSheet("")
        self.score_value.setText(
            "✅ 声纹注册成功 — 点击「启动过滤」开始使用。"
            "（会议里记得把麦克风设为 CABLE Output）"
        )
        # Make sure the start button is enabled and visually draw the eye
        # to it as the next action. setFocus also lets the user press Enter.
        self.btn_start.setEnabled(True)
        self.btn_start.setFocus()
        self._append_log("✅ 声纹注册成功。下一步：点击「启动过滤」。")

    def _on_error(self, msg: str) -> None:
        self._append_log(f"ERROR: {msg}")
        QMessageBox.critical(self, "错误", msg)

    def _append_log(self, msg: str) -> None:
        self.log_view.append(msg)

    def _refresh_status(self) -> None:
        if not self.service.has_enrollment():
            self.btn_start.setEnabled(True)  # can start, will run paused
            self.score_value.setText("未注册声纹 — 录制后才能识别。")
            self.score_value.setStyleSheet(f"color: {Colors.STATE_WARN};")

    def _refresh_stats(self) -> None:
        # Placeholder for periodic refresh; main updates come via signal.
        pass

    def closeEvent(self, event):  # noqa: N802
        # Hide to tray instead of quitting (if a tray is registered later)
        event.ignore()
        self.hide()
