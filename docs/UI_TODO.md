# UI Improvement TODO

A backlog of UI/UX improvements for the PyQt6 surface, captured during the
2026-06-28 motion pass. Each item lists what to do, where to put it, and the
design principle it serves. Items are ordered by impact-per-effort — top of
each tier is the highest leverage.

Related: see `src/voicefilter/theme.py` for the current design system, and
`tests/ui_snapshots/` for the visual baseline this work was measured against.

---

## Tier 1 — high impact, contained change

### T1.1 — Real-time level meter
- **Where**: `MainWindow._build_ui` in `src/voicefilter/main_window.py`,
  inside the "声纹判定" `SectionCard`, replacing or sitting just above the
  current `ScoreBar`.
- **What**: Custom `QWidget` subclass that paints a 60-frame RMS history as
  a horizontal bar with peak-hold markers (like a DAW meter). Pull RMS from
  the existing `FilterStats` signal (already emitted at the hop cadence); if
  we need sub-hop resolution, add a separate `level_db` signal on
  `FilterService` sourced from `FilterPipeline.push`.
- **Why**: The current `ScoreBar` only changes state every 0.5 s, which feels
  dead. A live meter gives constant "is my mic picking up audio?" feedback —
  this is the single most-asked support question for tools like this.

### T1.2 — Threshold tick on the score bar
- **Where**: `ScoreBar` in `src/voicefilter/theme.py`.
- **What**: Subclass that draws a vertical tick at the threshold position
  (e.g. 62 / 100), and dims the region below the tick as "rejection zone".
- **Why**: Right now you see a number under the bar but no spatial sense of
  where the cutoff sits. Spatial encoding is read 3× faster than text.

### T1.3 — Enrollment progress as a circular ring
- **Where**: `EnrollmentDialog._build_ui` in `src/voicefilter/main_window.py`.
- **What**: Replace the linear `QProgressBar` for recording time with a
  `QPainter`-drawn ring that fills as elapsed_sec / 20 progresses. Keep the
  linear bar as a small inline element under the ring.
- **Why**: Linear bars feel like a download. A ring says "you're inside a
  recording session" — much more on-brand for a mic tool. Pair with the
  existing fade-in motion for a complete entrance.

### T1.4 — Settings persistence
- **Where**: New `QSettings("voiceprint-filter", "voiceprint-filter")` calls
  in `MainWindow` (`closeEvent` to save, `__init__` to load). Persist: window
  geometry, last selected input/output substrings, `other_gain_db` override.
- **Why**: Today every launch shows the first devices in the list and the
  default 720×520 window. Users who always pick the same headset mic have to
  re-pick it every time. Two-line change, big perceived-quality bump.

---

## Tier 2 — medium impact, bigger refactor

### T2.1 — Theme variants (dark / light / high-contrast)
- **Where**: `theme.py`. Split `GLOBAL_QSS` into `THEMES = {"dark": …,
  "light": …, "high_contrast": …}`; `apply_theme()` becomes `apply_theme(app,
  name="dark")`.
- **Why**: Some users hate dark UI (especially in well-lit meeting rooms);
  some need high-contrast. The token system is already in place — this is a
  straightforward second skin. Surface via a tray-menu item, not a settings
  dialog (a dialog would be overkill for one option).

### T2.2 — Diagnostic overlay (developer / power-user mode)
- **Where**: New `DiagnosticOverlay` widget toggled by Ctrl+D, lives on top
  of `MainWindow`. Shows: live RMS, VAD probability, last embedding distance
  (raw, not just normalized), current gain in dB, frames/sec, missed frames.
- **Why**: "Why is my voice being rejected?" is currently answered by asking
  the user to copy a log file. An in-app inspector means self-debugging.
  Hidden behind a chord keypress so non-power users never see it.

### T2.3 — Score history sparkline
- **Where**: Inside the "声纹判定" `SectionCard`, above `ScoreBar`.
- **What**: 200-pt mini-line chart of the last ~30 s of `last_score` values,
  recolored to match state (green/red/grey) per segment. `QPainter`-only
  (no matplotlib).
- **Why**: Single-sample score is noisy. A sparkline makes the signal-vs-
  noise visible without needing a full history graph.

### T2.4 — First-run onboarding
- **Where**: New `OnboardingWizard(QWizard)` shown from `tray_app.run()` when
  `not service.has_enrollment()` and a marker file
  `~/.voiceprint-filter/.onboarded` doesn't exist. Three pages: "选择麦克风",
  "在安静环境朗读 20 秒", "完成 — 试试暂停/继续".
- **Why**: Today the first launch dumps the user into a technical-looking
  window with a 6-line instruction block. An onboarding wizard is the
  difference between "installed it" and "actually configured it".

### T2.5 — Internationalization
- **Where**: New `src/voicefilter/i18n.py` with `tr()`; every visible string
  in `main_window.py` / `tray_app.py` / `enrollment.py` wrapped.
- **Why**: All UI strings are hardcoded Chinese. An English (or bilingual)
  variant is straightforward with Qt's `QTranslator` once strings are
  routed through one function. Low urgency but cheap if done while changes
  are still fresh.

---

## Tier 3 — polish & accessibility

### T3.1 — Animated button press
- Sub-class `QPushButton` to scale 0.97 on `:pressed` via
  `QPropertyAnimation`. The current QSS just changes background; a 60 ms
  scale feels more tactile.

### T3.2 — Tooltips on every control
- `QToolTip` on `thr_spin` ("Threshold for accepting your voice — higher
  rejects more aggressively"), `other_gain` ("Attenuation applied to other
  voices, in decibels"), `pause` toggle, etc. Currently zero tooltips.

### T3.3 — Tray menu shows live counts
- `act_pause` label flips between "暂停过滤 (12.4k frames)" /
  "恢复过滤 (12.4k frames)" via the existing `stats_changed` signal. Tiny
  detail, big "this app is alive" payoff.

### T3.4 — Re-entrancy-safe pulse timer
- `VoiceprintTrayApp._build_tray` in `tray_app.py` constructs
  `_TrayPulse(self.tray)` before `self.tray` is fully shown; the first
  `setIcon` in `_make_tray_icon` inside the timer can race. Guard with
  `self.tray.isVisible()` or move construction to after `self.tray.show()`.
- **Status**: not currently buggy but worth a defensive check.

### T3.5 — High-DPI score bar text
- The `ScoreBar.setFormat("%v / 100")` text uses Qt's default font; on a
  4K screen with 150% scaling the labels render small. Force the score
  card's labels to a min 12pt via `setStyleSheet("font-size: 12pt;")`.

### T3.6 — Replace emoji with proper icons
- `⚠` (warning) and `✅` (success) glyphs render inconsistently across
  Windows font fallbacks. Bundle 4–5 small `QPainter`-drawn icons in
  `theme.py` (warning, error, info, mic, cable) and reference them from
  a new `Icon` namespace.

---

## Bugs / correctness surfaced while doing this work

Not strictly "future" — these were noticed but not fixed because they're
orthogonal to the motion pass. Each is small enough to fold into the next
PR that touches the area.

- **`MainWindow._on_state`** (`main_window.py:340`): the `if "暂停" in msg`
  branch swallows the degraded state message "已暂停 — 麦克风丢失…(等待
  设备恢复)" with the amber color instead of the purple degraded color.
  Reorder the checks: degraded first, then ok, then paused, then default.

- **`StatusBadge.paintEvent`** (`theme.py`): paints at `x=2`, but the label's
  text padding isn't adjusted, so the dot overlaps the first character of
  short messages ("●  X"). Add `setIndent(16)` to the label.

- **`_fade_in` references `widget.pos()`** before `show()` runs in
  `EnrollmentDialog.__init__`. Currently works because of the
  `QTimer.singleShot(0, …)` deferral, but the comment is wrong. Make the
  function take an explicit "shown" signal or document the dependency.

- **`tests/ui_snapshots/` is committed**: the user explicitly asked for
  visual baselines, so this is intentional. If it ever bloats (>1 MB per
  PNG), add `tests/ui_snapshots/` to `.gitignore` and use a separate
  artifact store.

---

## Process notes

- All UI work should be screenshotted with `QT_QPA_PLATFORM=offscreen`
  before/after and dropped into `tests/ui_snapshots/`. The screenshot
  pipeline used in this work is a ~30-line script — worth promoting to
  `scripts/snapshot_ui.py` if anyone picks up Tier 1 work.
- Design tokens are now centralized in `theme.py`; **do not** add new
  inline `setStyleSheet` calls outside `theme.py`. New widget styles
  belong as either: (a) a new themed widget in `theme.py`, or (b) a
  `[role="…"]` selector added to `GLOBAL_QSS`.
- Motion timings (`_ANIM_DIALOG_MS`, `_ANIM_SCORE_MS`) are defined at
  the top of `main_window.py`; if a second consumer appears, promote them
  to `theme.Motion`.
