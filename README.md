# 声纹过滤 — Voiceprint Filter

一个完全跑在**本机 Windows** 上的"声纹门"：只保留**你的声音**传入腾讯会议 / Zoom / 飞书 / Discord，旁边其他人说话、电视、空调噪声会被极大衰减。和传统降噪不同，它按**说话人身份**过滤，不是按噪声类型过滤——所以"旁边有人插话"这种场景它管得了，Krisp / RTX Voice 管不了。

```
[真实麦克风] → [本程序：sherpa-onnx 声纹识别] → [VB-CABLE 虚拟麦克风] → [腾讯会议]
```

所有处理 100% 本地、离线、CPU 运行，**不联网、不上传任何音频**。

---

## 快速开始（普通用户）

> 需要从 [Releases](../../releases) 下载打包好的 `voiceprint-filter.exe`。首次发布前可先用下方"从源码运行"。

1. **装 VB-CABLE**（必须，免费）：到 <https://vb-audio.com/Cable/> 下载 `VBCABLE_Driver_Pack43.zip` → 解压 → 右键 `VBCABLE_Setup_x64.exe` **以管理员身份运行** → Install → **重启电脑**。
2. **下载并运行** `voiceprint-filter.exe`。首次启动会检测 VB-CABLE 和声纹模型，缺失会弹窗引导。
3. **注册声纹**：用日常说话音量朗读 20 秒。程序只保存 192 个浮点数（声纹向量），**不保存原始录音**。
4. **在会议里选麦克风**：腾讯会议 → 设置 → 音频 → 麦克风选 `CABLE Output (VB-Audio Virtual Cable)`。关闭腾讯会议自带的"麦克风降噪/声音美化"。
5. 看托盘图标变绿 = 正在过滤。别人说话会被压到几乎听不见，你说话原样通过。

## 从源码运行（开发者 / 高级用户）

```bash
git clone https://github.com/Zoe-King-dev/voiceprint-filter.git
cd voiceprint-filter
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python scripts/download_models.py   # 两个 ONNX 模型（约 30 MB）下到 ./models/
python main.py
```

> 不要额外 `pip install onnxruntime`——sherpa-onnx 的 wheel 自带 ONNX Runtime，同时装会冲突。Python ≥ 3.10。

自测声纹引擎（不开腾讯会议）：

```bash
python scripts/verify_pipeline.py --record    # 录两段 10s，打印 score
python scripts/measure_latency.py             # 测延迟预算
python -m pytest -q                           # 跑单元测试
```

## 调参

主窗口里：

| 控件 | 含义 | 建议值 |
|---|---|---|
| **判定阈值** | 余弦相似度低于此值视为"他人" | 0.58–0.66；想更严就调高 |
| **他人衰减** | 别人声音压低的分贝数 | -30 dB（默认，保留微弱存在感）；-∞ 几乎全静音 |

实时 score 条：绿色 = 我（保留）/ 红色 = 他人（衰减）/ 灰色 = 静音。最佳调法：开两个扬声器，自己说话时关一个，观察绿红 score 差取中点作阈值。

## 麦克风一致性（重要）

**注册时用什么麦，会议时必须用同一只麦**——声纹对麦克风频响非常敏感，换麦会让 score 暴跌。换麦了就重做一次注册。这是误判的头号原因。

## 常见问题 / 故障排查

| 现象 | 原因 / 解决 |
|---|---|
| 装了 VB-CABLE 但"声音"设置里看不到 | 必须**重启电脑**。驱动级安装要等 Windows 重新枚举设备。 |
| 启动弹窗"未检测到 VB-CABLE" | 没装或没重启。去 vb-audio.com/Cable 装，重启。 |
| 启动弹窗"模型文件缺失" | 联网下载失败。点"重试"，或从 GitHub Releases 手动下模型包放到 `%APPDATA%/voiceprint-filter/models/`。 |
| 自己说话也被衰减了 | 阈值调低（0.55 左右），或重新注册（同麦、同距离）。 |
| 别人说话还能听到 | "他人衰减"调到 -50 dB 或更低；或阈值调到 0.70+（太高会切到自己）。 |
| 托盘变灰 + 通知"麦克风丢失" | 麦克风被拔出/换 USB 口。重新插好会自动恢复。 |
| 托盘变灰 + 通知"虚拟音频设备丢失" | VB-CABLE 驱动被重载。等它恢复会自动重连。 |
| 程序崩溃 | 日志在 `%APPDATA%/voiceprint-filter/logs/voiceprint-filter.log`，托盘菜单"查看日志/报告问题"可直接打开。 |
| 延迟多大？ | 稳态反应约 0.5 s（一个 hop）；冷启动约 1 s（滑窗首次填满）。会议够用；游戏开黑要更低延迟，暂不支持。 |
| CPU 占用？ | i5/i7 单核约 5–10%，`num_threads=2` 已够。 |

> 想改延迟/窗口：编辑 `config/default.yaml`，`window_sec` 调到 0.6、`hop_sec` 调到 0.3 反应更快（误判风险略升）。

## 隐私

- 只保存 **192 个浮点数**（声纹 embedding），无法反向还原原始音频。
- 注册完成后立即丢弃原始录音，**不写盘**。
- 100% 本地处理，推理在 CPU 上，**不联网**（仅首次下载模型时联网）。
- 日志只记运行状态，不含音频。

## 目录结构

```
voiceprint-filter/
├── main.py                  # 入口
├── voiceprint-filter.spec   # PyInstaller 打包配置
├── config/                  # default.yaml（随程序）+ user.yaml（你的覆盖）
├── models/                  # ONNX 模型（脚本下载）
├── scripts/
│   ├── download_models.py   # 拉模型
│   ├── verify_pipeline.py   # 自测打分
│   └── measure_latency.py   # 延迟预算
├── tests/                   # pytest 单元测试
└── src/voicefilter/         # config / audio_router / speaker_engine / vad
                             # filter_pipeline / enrollment / main_window / tray_app / app / paths
```

冻结（exe）模式下，只读资源（模型、`default.yaml`）来自 PyInstaller 临时目录，可写数据（注册 embedding、日志）写到 `%APPDATA%/voiceprint-filter/`——由 `paths.PathResolver` 统一解析。

## 许可

本项目仅做整合与胶水代码，声纹识别归功于：
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache 2.0)
- [3D-Speaker](https://github.com/modelscope/3D-Speaker) (Apache 2.0)
- [Silero VAD](https://github.com/snakers4/silero-vad) (MIT)
- [VB-CABLE](https://vb-audio.com/Cable/) (donationware，个人免费)

本项目代码采用 **Apache 2.0**。

---

## English

A local **Windows** "voice gate": only **your** voice reaches 腾讯会议 / Zoom / 飞书 / Discord; other people talking nearby, the TV, AC hum are heavily attenuated. Unlike noise reduction, it filters by **speaker identity**, not noise type — so a "someone next to me chimes in" situation is handled here, not by Krisp / RTX Voice. 100% local, offline, CPU — no network, no audio uploaded.

**Quick start (end user):** install [VB-CABLE](https://vb-audio.com/Cable/) (free, then **reboot**), download `voiceprint-filter.exe` from [Releases](../../releases), run it, enroll your voice (20 s, only a 192-d vector is saved — **no raw audio**), then set your meeting app's microphone to `CABLE Output`. Tray icon green = filtering.

**From source:** `python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt && python scripts/download_models.py && python main.py` (Python ≥ 3.10; do **not** separately `pip install onnxruntime` — sherpa-onnx bundles its own ORT).

**Tuning:** threshold 0.58–0.66 (cosine similarity below it = "other"); other-gain -30 dB default. Keep the **same mic** for enrollment and meetings — embeddings are mic-frequency-response-sensitive; switching mics is the #1 cause of false rejects.

**Troubleshooting:** see the Chinese table above (VB-CABLE invisible until reboot; model-download retry; tray greys on mic/CABLE loss with auto-recover; crash logs at `%APPDATA%/voiceprint-filter/logs/`). Latency: ~0.5 s steady-state, ~1 s cold start — fine for meetings, not yet for gaming.

**Privacy:** only a 192-d embedding is stored; raw enrollment audio is discarded in memory and never written to disk; inference is on-device CPU; logs contain no audio.

**License:** Apache 2.0 for this project's glue code; recognition credited to sherpa-onnx / 3D-Speaker (Apache 2.0), Silero VAD (MIT), VB-CABLE (donationware).
