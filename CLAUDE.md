# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**voiceprint-filter** — a local Windows "voice gate" that only forwards your voice to virtual-meeting apps (腾讯会议 / Zoom / 飞书 / Discord). It sits between your real microphone and a VB-CABLE virtual mic, identifies the speaker every 0.5 s using a 192-d speaker embedding (sherpa-onnx 3D-Speaker), and attenuates non-matching audio (~-30 dB). All processing is offline on CPU.

Audio chain: `[real mic] → [this program] → [VB-CABLE Input] → [meeting app reads CABLE Output]`.

## Common commands

There is **no test suite, linter, or formatter configured** in this repo — `tests/` is empty, and `pyproject.toml` has no tool config. The only "run" surface is the GUI itself plus two helper scripts.

```bash
# one-time setup
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python scripts/download_models.py        # downloads 2 ONNX models into ./models/

# run the app
python main.py                           # tray + main window; auto-opens window on first launch

# self-test the speaker engine end-to-end
python scripts/verify_pipeline.py --record       # records two 10s clips from default mic, prints scores
python scripts/verify_pipeline.py me.wav other.wav --threshold 0.62
```

Dependency notes:
- **Do not** add a separate `onnxruntime` pin — `sherpa-onnx`'s wheel bundles its own ORT; explicit pins cause version conflicts.
- Python ≥ 3.10 (pydantic v2 + PyQt6).
- `pyinstaller` is in `requirements.txt` but no `.spec` file is checked in; freezing is left as a manual step.

## High-level architecture

The program is a single-process PyQt6 app with three layers and one shared service object.

```
scripts/  ──┐
            ▼
        main.py ──► voicefilter.tray_app.run()
                         │
                         ▼
        VoiceprintTrayApp (QObject, owns everything below)
            ├─ FilterService   ◄── single source of truth for audio + state
            │     ├─ AudioRouter        (sounddevice device lookup + streams)
            │     ├─ SpeakerEngine      (sherpa-onnx 3D-Speaker, 192-d cosine verify)
            │     ├─ FilterPipeline     (sliding-window VAD + verify → gain_db decision)
            │     └─ RingBuffer (utils) (2 s output cushion feeding the output callback)
            ├─ MainWindow        (device pickers, threshold slider, live score bar, log)
            └─ QSystemTrayIcon   (green/grey circle + open/pause/enroll/quit menu)
```

### Key data flow per audio frame (~30 ms, `audio.frame_ms`)

1. sounddevice **input callback** (`FilterService._on_input`, PortAudio thread) hands a mono frame to `FilterPipeline.push`.
2. `FilterPipeline` appends the frame to its sliding `RingBuffer` (size = `window_sec`, default 1 s) and feeds the VAD.
3. Every `hop_sec` (default 0.5 s), if the window is full, it calls `SpeakerEngine.verify(window)` → `(is_me, score)`, picks a gain from `my_gain_db` / `other_gain_db` / `no_speech_gain_db`, and stores it as `_latest_gain_db`.
4. The same input frame is multiplied by `db_to_linear(_latest_gain_db)` and pushed into the **output `RingBuffer`** in `FilterService`.
5. sounddevice **output callback** (`FilterService._on_output`) drains that ring buffer into the VB-CABLE Input device.

### Critical invariants / gotchas

- **Hot path is single-threaded on the audio thread.** `FilterPipeline._latest_gain_db` is a precomputed float; verification is hop-gated, not per-frame. Never block in the input callback — no allocation-heavy work there.
- **`SpeakerEngine.EMBEDDING_DIM = 192`** is the contract shared by `load_enrollment` (shape validation) and `extract_embedding` (length check, returns zeros if audio < 300 ms).
- **`FilterPipeline.set_threshold` only updates `engine.threshold`**; `FilterService.set_threshold` also writes back to `cfg.speaker.threshold`. If you bypass `FilterService`, persist the new threshold yourself or the UI will revert.
- **Pause = pure bypass** (`FilterPipeline.push` returns the frame unchanged, no VAD/verify work). Useful for "let everything through" mode but means no stats are emitted.
- **Enrollment uses the same `SpeakerEngine.compute`** as verification — do not fork the embedding code. The wizard saves a 192-d `.npy` and immediately reloads it; raw audio is **never** persisted (privacy guarantee documented in README).
- **VB-CABLE is required.** `AudioRouter.has_cable()` returns False until the driver is installed and the system has been rebooted (Windows re-enumerates devices after a reboot). `MainWindow` shows a red warning banner when it's missing.
- **VB-CABLE endpoints are NOT recording sources.** VB-CABLE is a one-way virtual cable — `CABLE Input` is what we *write* to (filter pipeline output), `CABLE Output` is what meeting apps *read* from as their mic. Neither has a physical transducer behind it, so opening an InputStream on either returns silence / stale buffer residue (this looks like a "too quiet" enrollment error but is really "wrong device"). `AudioRouter.list_input_devices` filters VB-CABLE endpoints out, and `find_input("CABLE Output")` raises `DeviceNotFoundError` with an explicit hint.
- **Microphone must match between enrollment and meeting use** — speaker embeddings are very sensitive to mic frequency response. README warns this; it is the #1 cause of false rejects.

### Module map (`src/voicefilter/`)

| Module | Role |
|---|---|
| `config.py` | Pydantic models (`AudioConfig`, `VADConfig`, `SpeakerConfig`, `AppConfig`); deep-merges `config/default.yaml` + `config/user.yaml`, resolves relative paths against project root. |
| `app.py` | `FilterService` QObject — owns engine/pipeline/streams, emits `stats_changed` / `state_changed` / `error` / `log` Qt signals. |
| `audio_router.py` | `AudioRouter` wraps `sounddevice.query_devices`; substring-matched `find_input`/`find_output`; `DeviceNotFoundError` lists available devices. |
| `speaker_engine.py` | `SpeakerEngine` wraps `sherpa_onnx.SpeakerEmbeddingExtractor`. Lazy-loads the extractor so missing models don't block GUI startup. |
| `vad.py` | `SileroVAD` (preferred, via sherpa-onnx) + `EnergyVAD` (CPU fallback). `make_vad()` auto-selects; falls back if `models/silero_vad.onnx` is missing. |
| `filter_pipeline.py` | `FilterPipeline` — the real-time decision loop described above. Emits `FilterStats` via `on_update` callback. |
| `enrollment.py` | `EnrollmentWizard` (record → extract → save), `EnrollmentRecorder`, `PROMPT_TEXT`. 20 s guided recording with RMS sanity check (raises if < 0.005). |
| `main_window.py` | `MainWindow` + `EnrollmentDialog` (Qt widgets). `closeEvent` hides to tray; tray keeps app alive via `setQuitOnLastWindowClosed(False)`. |
| `tray_app.py` | `VoiceprintTrayApp` boots Qt, builds tray icon (drawn programmatically — no asset files), wires signals. Module-level `run()` is the entry point used by `main.py`. |
| `utils/ringbuffer.py` | Thread-safe fixed-size float32 ring buffer; `snapshot()` returns a contiguous copy. Used for both the sliding window and the output cushion. |
| `utils/power.py` | `linear_to_db`, `db_to_linear`, `rms_db` — pure numpy, allocation-light. |

### Configuration layering

`config/default.yaml` is shipped; `config/user.yaml` (if present) deep-merges on top and wins. Relative paths (`speaker.model_path`, `embedding_path`) are resolved against the project root, not cwd. The three gain values are the main knobs — defaults: `my_gain_db=0`, `other_gain_db=-30`, `no_speech_gain_db=-6`.

### Adding a new audio effect / threshold

The clean place to plug in is `FilterPipeline._decide()` (the hop-triggered branch). It owns the `is_speech / is_me / score` triple and writes `_latest_gain_db`. Anything you put there runs once per `hop_sec`, not per frame.

### Scripts

- `scripts/download_models.py` — pulls the embedding model tarball (auto-extracts, deletes the archive) and the Silero VAD into `./models/`. Idempotent — skips files that already exist.
- `scripts/verify_pipeline.py` — offline end-to-end check using `tests/audio/me_10s.wav` + `tests/audio/other_10s.wav`. With `--record`, captures fresh clips from the default mic. Prints `score / threshold → ACCEPT|REJECT (ms)`.

## gstack

**Use the `/browse` skill from gstack for all web browsing.** Never use `mcp__claude-in-chrome__*` tools — gstack's headless Chromium is the browser of record for this project.

Available skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/setup-gbrain`, `/retro`, `/investigate`, `/document-release`, `/document-generate`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`.

If `gstack` is missing on a teammate's machine, have them run:

```bash
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
cd ~/.claude/skills/gstack && ./setup
```

(On Windows, `setup` requires `node` in addition to `bun`, and the Windows bun installer only ships `bun.exe` — `cp bun.exe bunx.exe` next to it as a shim if `bunx` isn't found.)