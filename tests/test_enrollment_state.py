"""Tests for FilterService's post-enrollment state signaling.

User-reported bug: after finishing enrollment, the status read "运行中"
for an hour with no progress, no clear success, no "what do I do next" --
users assumed the app had silently failed.

Root cause: the enrollment dialog used to call service.resume() on
completion. With no audio pipeline running yet (the user hadn't pressed
"启动过滤"), resume() still emitted "运行中", painting the status badge
green while no audio flowed. The fix splits the post-enrollment path into
a dedicated mark_enrollment_loaded() that emits an honest "已注册 — 等待
启动过滤" state + an enrollment_completed signal the UI can surface.

These tests pin that behavior without needing real audio devices: the
SpeakerEngine and AudioRouter are faked, QT_QPA_PLATFORM=offscreen avoids
a display, and we capture emitted signals via Qt slots.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from voicefilter.app import FilterService  # noqa: E402
from voicefilter.config import AppConfig  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    # session-scoped: creating QApplication more than once in a process
    # crashes Qt, so share one across all tests in this module.
    return QApplication.instance() or QApplication([])


class _FakeEngine:
    """Stand-in SpeakerEngine with scriptable enrollment state."""

    EMBEDDING_DIM = 192
    threshold = 0.62

    def __init__(self, *, enrolled=False):
        self._enrolled = enrolled
        self.load_calls = 0

    def is_enrolled(self) -> bool:
        return self._enrolled

    def load_enrollment(self, path):
        self.load_calls += 1
        self._enrolled = True

    def set_threshold(self, t):
        self.threshold = float(t)


def _make_service(qapp, *, enrolled=False, pipeline_running=False) -> FilterService:
    """Build a FilterService with faked engine + router; no audio I/O."""
    fake_engine = _FakeEngine(enrolled=enrolled)
    fake_router = MagicMock()

    cfg = AppConfig()  # defaults — no yaml needed
    cfg.embedding_path = "/tmp/fake-embedding.npy"  # type: ignore[attr-defined]

    with patch("voicefilter.app.SpeakerEngine", return_value=fake_engine), \
         patch("voicefilter.app.AudioRouter", return_value=fake_router):
        service = FilterService(cfg, project_root=None)  # type: ignore[arg-type]

    # Reflect the requested running state without opening real streams.
    if pipeline_running:
        service.pipeline = MagicMock()  # truthy → resume() treats it as live
        service._running = True
    return service


class _SignalCatcher:
    """Records every signal emission onto a list for assertions."""

    def __init__(self, service: FilterService):
        self.states: list[str] = []
        self.logs: list[str] = []
        self.enrollment_completed = 0
        service.state_changed.connect(self.states.append)
        service.log.connect(self.logs.append)
        service.enrollment_completed.connect(self._on_completed)

    def _on_completed(self) -> None:
        self.enrollment_completed += 1


def test_mark_enrollment_loaded_emits_completed_and_honest_state(qapp):
    """No pipeline running → must NOT say "运行中"; must fire completed signal."""
    service = _make_service(qapp, enrolled=False, pipeline_running=False)
    catcher = _SignalCatcher(service)

    service.mark_enrollment_loaded()

    assert catcher.enrollment_completed == 1, "enrollment_completed signal must fire"
    # The last state emission must be the honest "waiting to start" one,
    # not a misleading "运行中".
    assert any("已注册" in s and "等待启动" in s for s in catcher.states), \
        f"expected '已注册 — 等待启动过滤' in states, got {catcher.states}"
    assert not any("运行中" in s for s in catcher.states), \
        f"must not emit '运行中' with no pipeline running, got {catcher.states}"
    assert any("✅" in s and "注册成功" in s for s in catcher.logs), \
        f"expected a success log line, got {catcher.logs}"


def test_mark_enrollment_loaded_with_pipeline_running_resumes(qapp):
    """If a pipeline IS already running, enrollment completion resumes it."""
    service = _make_service(qapp, enrolled=False, pipeline_running=True)
    catcher = _SignalCatcher(service)

    service.mark_enrollment_loaded()

    assert catcher.enrollment_completed == 1
    service.pipeline.resume.assert_called_once()
    assert any("运行中" in s for s in catcher.states), \
        f"with a live pipeline, '运行中' is correct, got {catcher.states}"


def test_resume_without_pipeline_does_not_emit_running(qapp):
    """The original bug: resume() with no pipeline emitted '运行中'."""
    service = _make_service(qapp, enrolled=True, pipeline_running=False)
    catcher = _SignalCatcher(service)

    service.resume()

    assert not any("运行中" in s for s in catcher.states), \
        f"resume() must not claim '运行中' with no pipeline, got {catcher.states}"
    assert any("已注册" in s for s in catcher.states), \
        f"enrolled + no pipeline should say '已注册 — 等待启动过滤', got {catcher.states}"


def test_resume_without_enrollment_says_unenrolled(qapp):
    """No pipeline + not enrolled → must say so, not '运行中'."""
    service = _make_service(qapp, enrolled=False, pipeline_running=False)
    catcher = _SignalCatcher(service)

    service.resume()

    assert not any("运行中" in s for s in catcher.states)
    assert any("未注册" in s for s in catcher.states), \
        f"expected '未注册' guidance, got {catcher.states}"
