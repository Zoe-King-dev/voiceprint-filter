"""Main control window — device pickers, threshold slider, live score bar."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QPalette
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .app import FilterService
from .enrollment import EnrollmentError, EnrollmentWizard, PROMPT_TEXT

log = logging.getLogger(__name__)


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

        intro = QLabel(
            "首次使用，请用日常说话音量朗读下方文本 20 秒。\n"
            "建议在安静环境、与会议时同一只麦克风下录制。\n"
            "⚠ 请选择你的真实麦克风（如「麦克风」「Headset Mic」）——\n"
            "VB-CABLE 虚拟端点不支持反向录音。"
        )
        intro.setWordWrap(True)
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
        layout.addWidget(self.status)

        btn_row = QHBoxLayout()
        self.btn_mic = QComboBox()
        for d in service.list_input_devices():
            self.btn_mic.addItem(f"[{d.idx}] {d.name}", userData=d)
        btn_row.addWidget(QLabel("麦克风:"))
        btn_row.addWidget(self.btn_mic, 1)

        self.btn_start = QPushButton("开始录制")
        self.btn_start.clicked.connect(self._start)
        btn_row.addWidget(self.btn_start)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        btn_row.addWidget(self.btn_cancel)

        layout.addLayout(btn_row)

    def _start(self) -> None:
        dev = self.btn_mic.currentData()
        if dev is None:
            QMessageBox.warning(self, "未选麦克风", "请先选择一个麦克风。")
            return
        self.btn_start.setEnabled(False)
        self.btn_mic.setEnabled(False)
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
                self.btn_cancel.setEnabled(False)
                return
            except Exception as e:  # pragma: no cover
                log.exception("Enrollment worker crashed")
                self.status.setText(f"内部错误: {e}")
                self.btn_start.setEnabled(True)
                self.btn_mic.setEnabled(True)
                self.btn_cancel.setEnabled(False)
                return
            # Re-load into service's engine
            self.service.engine.load_enrollment(self.service.embedding_path())
            self.status.setText("✅ 注册完成！可以关闭此窗口。")
            self.btn_start.setText("重新录制")
            self.btn_start.setEnabled(True)
            self.btn_mic.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.service.resume()

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _cancel(self) -> None:
        if self._wizard is not None:
            self._wizard.cancel()

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
        self.resize(720, 520)
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

        # Device pickers
        dev_row = QHBoxLayout()
        self.in_combo = QComboBox()
        self.out_combo = QComboBox()
        self._populate_devices()
        dev_row.addWidget(QLabel("真实麦克风:"))
        dev_row.addWidget(self.in_combo, 1)
        dev_row.addWidget(QLabel("虚拟麦克风:"))
        dev_row.addWidget(self.out_combo, 1)
        root.addLayout(dev_row)

        if not self.service.has_cable():
            warn = QLabel(
                "⚠ 未检测到 VB-CABLE 设备。请从 vb-audio.com/Cable 下载安装，"
                "然后重启电脑，再回到本程序。"
            )
            warn.setStyleSheet("color: #b00; padding: 6px;")
            warn.setWordWrap(True)
            root.addWidget(warn)

        # Start/stop
        ctrl_row = QHBoxLayout()
        self.btn_start = QPushButton("启动过滤")
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
        root.addLayout(ctrl_row)

        # Score bar
        score_label = QLabel("实时声纹相似度（绿色=我 / 红色=他人 / 灰色=静音）")
        root.addWidget(score_label)
        self.score_bar = QProgressBar()
        self.score_bar.setRange(-100, 100)
        self.score_bar.setValue(0)
        self.score_bar.setFormat("%v / 100")
        root.addWidget(self.score_bar)
        self.score_value = QLabel("—")
        self.score_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.score_value)

        # Threshold & gain controls
        thr_row = QHBoxLayout()
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
        root.addLayout(thr_row)

        # Log
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        root.addWidget(self.log_view, 1)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_label = QLabel("未启动")
        sb.addWidget(self.status_label)

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
        if not stats.last_is_speech:
            color = "#bbb"
        elif stats.last_is_me:
            color = "#3a3"
        else:
            color = "#c33"
        self.score_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {color}; }}"
        )
        if stats.last_is_speech:
            self.score_value.setText(
                f"score={stats.last_score:+.3f}  thr={self.service.cfg.speaker.threshold:.2f}  "
                f"{'ME' if stats.last_is_me else 'OTHER'}  infer={stats.last_infer_ms:.0f}ms"
            )
        else:
            self.score_value.setText("静音")
        self.status_label.setText(
            f"frames={stats.frames_processed}  me={stats.accepted_frames}  "
            f"rej={stats.rejected_frames}  gain={stats.last_gain_db:.1f}dB"
        )

    def _on_state(self, msg: str) -> None:
        self.status_label.setText(msg)

    def _on_error(self, msg: str) -> None:
        self._append_log(f"ERROR: {msg}")
        QMessageBox.critical(self, "错误", msg)

    def _append_log(self, msg: str) -> None:
        self.log_view.append(msg)

    def _refresh_status(self) -> None:
        if not self.service.has_enrollment():
            self.btn_start.setEnabled(True)  # can start, will run paused
            self.score_value.setText("未注册声纹 — 录制后才能识别。")
            self.score_value.setStyleSheet("color: #888;")

    def _refresh_stats(self) -> None:
        # Placeholder for periodic refresh; main updates come via signal.
        pass

    def closeEvent(self, event):  # noqa: N802
        # Hide to tray instead of quitting (if a tray is registered later)
        event.ignore()
        self.hide()