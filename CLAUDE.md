# video-pipeline

## 项目背景

视频 → 中文配音流水线，将视频（或音频）先转录为文字，再翻译为中文，最后用 TTS 合成配音。

**组件：**
- **ASR（音频 → 文字）：** `pyvideotrans` 的 `--task stt`，底层 faster-whisper `large-v3`
- **翻译（文字 → 中文）：** `pyvideotrans` 的 `--task sts`，本地 LLM `qwen3.8-27b`（走 llama-swap）
- **TTS（中文 → 语音）：** fish-speech S2 Pro（默认，走 llama-swap）或 腾讯 AuK（zero-shot，本地 shim）
- **视频合成：** `pyvideotrans` 的 `--task vtv`（TTS + 原视频音轨混合 + 字幕嵌入）

## 为什么是 OpenClaw skill + Web 页面，而非在对话里跑？

OpenClaw / Claude Code 对话本身也占用同一个本地 LLM（qwen3.8-27b，走 llama-swap）和 TTS 模型（s2-pro / AuK），与流水线在 **24GB 单卡上互斥**——一边跑流水线一边找 agent 说话会触发 llama-swap 换模型、打断任务。

所以把「启动/监控」做成 **零 LLM 调用的 Web 页面**（`pipeline-web.py`，仅标准库），跑批量时不用碰 agent。

## 硬约束（运行期间）

1. **别找任何 agent 说话**（共用 llama-swap 的 qwen3.8-27b，与 TTS 互斥）
2. 不要打开 pyvideotrans GUI（关窗会写回默认 cfg.json）
3. 同时只跑一条流水线 / 一个 AuK 任务
4. GPU 互斥（24GB 单卡）：LLM ~23.7GB / s2-pro ~18.5GB / AuK ~17GB / faster-whisper ~5GB
5. 查进度：`tail -20 <out>/pipeline.log`；`pgrep -af "dub.sh|auk-infer|auk-tts-shim"`
6. 排障：打开 `/ctx/<任务名>` → 复制全部 → 贴给 agent 分析

## 用法

```bash
# 整片（自动切段+配音+合并）
~/workspace/money_code/videotrans/video-pipeline/scripts/dub.sh /path/to/video.mp4 --out /home/loomz/视频/outputs/<名>
# 后台启动：
nohup <命令> >/dev/null 2>&1 &

# 音频 → 中文文本（只转录+翻译，不合成视频）
# 通过 Web 页面上传 m4a/mp3/wav → 选择源语言 → 转录并翻译
# http://127.0.0.1:8090
```

## Web 控制台

```bash
nohup python3 ~/workspace/money_code/videotrans/video-pipeline/pipeline-web.py >/tmp/pipeline-web.log 2>&1 &
# → http://127.0.0.1:8090
```

功能：
- 任务列表（段进度/阶段/状态，5s 自动刷新）
- 启动任务（改 config.env，先备份 .bak）
- 日志 tail 300 行
- GPU 状态 + 卸载模型
- 停止（SIGTERM 进程组）
- 排障上下文（参数+日志+nvidia-smi+进程 → 一键复制给 agent）
- **音频 → 中文文本**（上传音频，选择源语言，ASR+翻译，不碰 TTS）

## 语言支持

翻译走 LLM 通道（qwen3.8-27b），`LANG_CODE` 中 42 个语言码任意源→任意目标：

`zh-cn` `zh-tw` `yue`(粤语) `en` `ja` `ko`(韩语) `fr` `de` `es` `es-419` `pt` `pt-br` `ru` `uk` `it` `nl` `pl` `ro` `hu` `cs` `sv` `fi` `nb` `el` `tr` `ar` `fa` `he` `hi` `id` `ms` `th` `vi` `km` `lo` `kk` `ur` `uz` `bn` `fil` `bg` + `auto`

**短板是 TTS**：s2-pro / AuK 以中文向为主，粤语/韩语等非中文合成未验证。纯「音频→文本」不碰 TTS，所以全语言通。

## TTS 引擎选择（config.env `TTS_MODEL`）

- `"s2-pro"`（默认）：fish-speech S2 Pro，走 llama-swap；`--compile` 后 ~10-18s/请求
- `"auk"`：腾讯 AuK zero-shot，本地 shim（端口 5998）；质量更高但慢（~2-4min/段）

## 音色（config.env `VOICE_ROLE`）

- `"clone"`：克隆原视频英文说话人的音色说中文（带外国口音）
- `"<文件名>"`（如 `zhvoice.wav`）：用固定中文参考音色，更自然
  - 文件放 `pyvideotrans/f5-tts/<VOICE_ROLE>`
  - `VOICE_REF_TEXT` 填那段音频里说的话

## 断点续跑

段输出 `seg_NNN.mp4` 存在 → 整段跳过；`en.srt` 存在 → 跳过 ASR；`zh-cn.srt` 存在 → 跳过翻译。
重跑某段：删对应 `seg_NNN.mp4`（保留 srt）。

## 配置文件

`config.env`：`ASR_MODEL` `TRANSLATE_MODEL` `TTS_MODEL` `TTS_TYPE` `VOICE_ROLE` `SEGMENT_MIN` `SWAP_BASE` `TRANSLATE_TYPE` 等。
每次启动任务前自动备份为 `config.env.bak`。
