"""Central QObject that owns engine, pipeline, and audio streams.

GUI components (main window, tray, enrollment dialog) all talk to this
object. It runs the sounddevice input/output streams in the background
and emits Qt signals when state changes.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .audio_router import AudioRouter, DeviceNotFoundError
from .config import AppConfig
from .filter_pipeline import FilterPipeline, FilterStats
from .speaker_engine import SpeakerEngine
from .utils.ringbuffer import RingBuffer

log = logging.getLogger(__name__)


class FilterService(QObject):
    """Owns runtime state. All mutations are serialized via ``_lock``."""

    stats_changed = pyqtSignal(object)  # FilterStats
    state_changed = pyqtSignal(str)     # human-readable status
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, cfg: AppConfig, project_root: Path):
        super().__init__()
        self.cfg = cfg
        self.project_root = project_root
        self.router = AudioRouter()
        self.engine = SpeakerEngine(cfg.speaker.model_path, threshold=cfg.speaker.threshold)
        self.pipeline: Optional[FilterPipeline] = None

        self._lock = threading.RLock()
        self._in_stream: Optional[sd.InputStream] = None
        self._out_stream: Optional[sd.OutputStream] = None
        self._out_buf: Optional[RingBuffer] = None
        self._in_dev = None
        self._out_dev = None
        self._running = False

        # Device-health polling (P1 error states: mic unplug / VB-CABLE gone).
        # Runs on the Qt GUI thread so it can safely query devices + emit signals.
        self._start_in_sub: Optional[str] = None
        self._start_out_sub: Optional[str] = None
        self._degraded = False
        self._degraded_reason = ""
        self._user_paused = False
        self._dev_poll = QTimer(self)
        self._dev_poll.setInterval(2000)
        self._dev_poll.timeout.connect(self._poll_devices)

        # Auto-load enrollment if it exists
        if Path(cfg.embedding_path).exists():
            try:
                self.engine.load_enrollment(cfg.embedding_path)
                self._emit_log(f"Loaded enrollment from {cfg.embedding_path}")
            except Exception as e:
                self._emit_log(f"Failed to load enrollment: {e}")

    # --- public API -------------------------------------------------------

    def has_enrollment(self) -> bool:
        return self.engine.is_enrolled()

    def embedding_path(self) -> Path:
        return Path(self.cfg.embedding_path)

    def list_input_devices(self):
        return self.router.list_input_devices()

    def list_output_devices(self):
        return self.router.list_output_devices()

    def has_cable(self) -> bool:
        return self.router.has_cable()

    def set_threshold(self, t: float) -> None:
        self.engine.set_threshold(t)
        self.cfg.speaker.threshold = t
        if self.pipeline is not None:
            self.pipeline.set_threshold(t)
        self._emit_log(f"Threshold set to {t:.3f}")

    def set_other_gain_db(self, db: float) -> None:
        self.cfg.speaker.other_gain_db = float(db)
        if self.pipeline is not None:
            self.pipeline.set_other_gain_db(db)
        self._emit_log(f"Other-gain set to {db:.1f} dB")

    def pause(self) -> None:
        self._user_paused = True
        if self.pipeline is not None:
            self.pipeline.pause()
        self._emit_state("已暂停（直通原始麦克风）")

    def resume(self) -> None:
        self._user_paused = False
        if self._degraded:
            self._emit_state(f"已暂停 — {self._degraded_reason}（等待设备恢复）")
            return
        if self.pipeline is not None:
            self.pipeline.resume()
        self._emit_state("运行中")

    def start(self, in_substring: str, out_substring: str) -> None:
        """Start the audio streams. Safe to call once; subsequent calls restart."""
        with self._lock:
            if self._running:
                self.stop()
            try:
                in_dev = self.router.find_input(in_substring)
                out_dev = self.router.find_output(out_substring)
            except DeviceNotFoundError as e:
                self._emit_error(str(e))
                return

            # Remember the substrings so device-health recovery can re-resolve
            # the same devices after an unplug/replug (idx may change; name won't).
            self._start_in_sub = in_substring
            self._start_out_sub = out_substring

            sr = self.cfg.audio.sample_rate
            frame = int(self.cfg.audio.frame_ms * sr / 1000)

            self._out_buf = RingBuffer(int(sr * 2))  # 2 s output cushion
            self.pipeline = FilterPipeline(
                self.cfg, self.engine, on_update=self._on_pipeline_update
            )
            if self.engine.is_enrolled():
                self.pipeline.resume()
            else:
                self.pipeline.pause()
                self._emit_log("未注册声纹 — 暂停中，请先完成注册。")

            try:
                self._in_stream = self.router.open_input_stream(
                    device_idx=in_dev.idx,
                    samplerate=sr,
                    blocksize=frame,
                    dtype="float32",
                    callback=self._on_input,
                )
                self._out_stream = self.router.open_output_stream(
                    device_idx=out_dev.idx,
                    samplerate=sr,
                    blocksize=frame,
                    dtype="float32",
                    callback=self._on_output,
                )
                self._in_stream.start()
                self._out_stream.start()
            except Exception as e:
                self._emit_error(f"打开音频流失败：{e}")
                self.stop()
                return

            self._in_dev = in_dev
            self._out_dev = out_dev
            self._running = True
            self._degraded = False
            self._degraded_reason = ""
            self._emit_state("运行中" if self.engine.is_enrolled() else "未注册 — 暂停中")
            self._emit_log(f"输入设备: {in_dev.name}")
            self._emit_log(f"输出设备: {out_dev.name}")
            self._dev_poll.start()

    def stop(self) -> None:
        with self._lock:
            self._dev_poll.stop()
            for s in (self._in_stream, self._out_stream):
                if s is not None:
                    try:
                        s.stop()
                        s.close()
                    except Exception:
                        log.exception("Error closing stream")
            self._in_stream = None
            self._out_stream = None
            self._running = False
            self._in_dev = None
            self._out_dev = None
            self._out_buf = None
            self.pipeline = None
            self._degraded = False
            self._degraded_reason = ""
        self._emit_state("已停止")

    def is_running(self) -> bool:
        return self._running

    def current_devices(self):
        return self._in_dev, self._out_dev

    # --- sounddevice callbacks (run on PortAudio thread) -----------------

    def _on_input(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            log.debug("input status: %s", status)
        if self.pipeline is None or self._out_buf is None:
            return
        frame = indata[:, 0].copy()
        out = self.pipeline.push(frame)
        self._out_buf.extend(out)

    def _on_output(self, outdata, frames, time_info, status):  # noqa: ARG002
        if self._out_buf is None:
            outdata.fill(0)
            return
        avail = len(self._out_buf)
        n = min(frames, avail)
        if n < frames:
            # Underrun — fill with zeros for the missing samples
            chunk = np.zeros(frames, dtype=np.float32)
            if n > 0:
                snap = self._out_buf.snapshot()
                chunk[:n] = snap[-n:]
            outdata[:, 0] = chunk
        else:
            snap = self._out_buf.snapshot()
            outdata[:, 0] = snap[-frames:]
            # We've already copied it; we don't need to "consume" since
            # the buffer is a snapshot for this callback, not a FIFO.

    # --- pipeline → GUI signal bridge ------------------------------------

    def _on_pipeline_update(self, stats: FilterStats) -> None:
        self.stats_changed.emit(stats)

    # --- device-health polling (P1: mic unplug / VB-CABLE disappearance) --

    def _poll_devices(self) -> None:
        """Every ~2 s while running, check that both devices are still present.

        Detects mid-run mic unplug and VB-CABLE disappearance (e.g. driver
        reload). On loss: pause the pipeline, emit error + grey-tray state.
        On recovery: restart the streams on the same devices. Runs on the Qt
        GUI thread, so it may call ``query_devices`` and emit signals safely.
        """
        if not self._running or self._in_dev is None or self._out_dev is None:
            return
        try:
            inputs = self.router.list_input_devices()
            outputs = self.router.list_output_devices()
        except Exception:  # pragma: no cover — transient PortAudio query error
            log.debug("device query failed; will retry next tick")
            return

        # Match by name: indices can reorder on unplug/replug, names don't.
        in_ok = any(d.name == self._in_dev.name for d in inputs)
        out_ok = any(d.name == self._out_dev.name for d in outputs)

        if in_ok and out_ok:
            if self._degraded:
                self._recover()
            return

        reasons = []
        if not in_ok:
            reasons.append(f"麦克风丢失：{self._in_dev.name}")
        if not out_ok:
            reasons.append(f"虚拟音频设备丢失：{self._out_dev.name}")
        msg = "；".join(reasons)
        if not self._degraded:
            self._enter_degraded(msg)

    def _enter_degraded(self, reason: str) -> None:
        self._degraded = True
        self._degraded_reason = reason
        if self.pipeline is not None:
            self.pipeline.pause()
        self._emit_error(f"已暂停过滤 — {reason}。等待设备恢复后自动重连。")
        self._emit_state(f"已暂停 — {reason}")

    def _recover(self) -> None:
        was_reason = self._degraded_reason
        self._degraded = False
        self._degraded_reason = ""
        self._emit_log(f"设备已恢复（{was_reason} 已解决），重新连接音频流…")
        # The old streams are likely dead; start() stops them and reopens on
        # the same device names (re-resolved by substring).
        if self._start_in_sub and self._start_out_sub:
            self.start(self._start_in_sub, self._start_out_sub)

    # --- helpers ---------------------------------------------------------

    def _emit_state(self, msg: str) -> None:
        self._emit_log(msg)
        self.state_changed.emit(msg)

    def _emit_log(self, msg: str) -> None:
        log.info(msg)
        self.log.emit(msg)

    def _emit_error(self, msg: str) -> None:
        log.error(msg)
        self.error.emit(msg)