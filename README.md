# video-pipeline

英文视频 → 中文配音流水线：**取片 → ASR → LLM 翻译 → 合并短句 → TTS → 合成**，自动切段、断点续跑、自动 QC、最终合并。附 Web 控制台与 agent skills。

单卡 RTX 5090 D 24GB，GPU 模型由 [llama-swap](http://127.0.0.1:8080) 热切换（LLM 与 TTS 互斥，脚本自动等待/卸载）。

## 目录结构

```
videotrans/
├── video-pipeline/            # 本工程: 编排脚本 + Web 控制台 + agent skills
│   ├── config.env             # 共享配置 (路径 / TTS 引擎 / 音色 / 并发), fetch.sh 与 dub.sh 共用
│   ├── service.sh             # Web 控制台管理: start|status|stop|logs|restart
│   ├── pipeline-web.py        # Web 控制台 (零依赖纯标准库, :8090, 仅本机)
│   ├── sync.sh                # 把 skills 同步到 Claude Code(软链) / OpenClaw(安装)
│   ├── scripts/
│   │   ├── fetch.sh           # 取片: yt-dlp 下载或本地路径透传, 末行输出 mp4 路径
│   │   ├── dub.sh             # 主流水线: ASR→sts→QC→合并短句→vtv→合并
│   │   ├── merge_srt.py       # 合并 SRT 连续短块 (无损/幂等, .raw 备份)
│   │   ├── auk.sh             # AuK 单片段音频生成/编辑包装器 (zero-shot/Instruct TTS/改词/去口音…)
│   │   ├── auk-tts-shim.py    # AuK 模拟 fish /v1/tts 接口 (TTS_MODEL=auk 时 dub.sh 的 TTS 后端)
│   │   └── warmup-s2pro-compile.sh  # s2-pro torch.compile 预热 (幂等, marker 命中秒过)
│   ├── video-dub/SKILL.md     # agent skill: 配音流水线 + AuK 音频工具
│   └── video-fetch/SKILL.md   # agent skill: 取片
├── pyvideotrans/              # 引擎 (fork): cli.py 提供 stt/sts/vtv 任务; 翻译 prompt 与逐行重翻补丁在此
├── s2-pro/                    # fish-speech S2 Pro 权重 + venv (由 llama-swap 拉起, 端口 5807)
└── AuK/                       # 腾讯 AuK (base/Flash) + Qwen2.5-Omni-3B 编码器 + venv
```

## 快速开始

```bash
cd ~/workspace/money_code/videotrans/video-pipeline

./service.sh                 # 起 Web 控制台 → http://127.0.0.1:8090
# llama-swap 单独管理 (与 agent 共用, 本脚本不碰): ~/start-llama-swap.sh {start|status|logs}
```

命令行跑一条视频（必须 nohup 后台）：

```bash
# 1) 取片 (URL 或本地路径; 末行输出 = mp4 路径)
scripts/fetch.sh "https://weibo.com/..." 

# 2) 配音 (自动切段+翻译+TTS+合成+合并)
nohup scripts/dub.sh /home/loomz/视频/downloads/weibo_<ID>.mp4 \
    --out /home/loomz/视频/outputs/weibo_<ID> >/dev/null 2>&1 &

# 3) 查进度
tail -20 /home/loomz/视频/outputs/weibo_<ID>/pipeline.log
```

最终产物：`<out>/<名>_zh.mp4`（带中文字幕）。

## 流水线细节（dub.sh）

每段（默认 30 分钟，NVENC 切分）：

1. **ASR** — faster-whisper `large-v3` → `en.srt`（已存在则跳过）
2. **翻译** — `sts` 任务，本地 LLM qwen3.8-27b（llama-swap）→ `zh-cn.srt`（已存在则跳过）
3. **合并前 QC** — en/zh 行数 1:1（容差 2%）+ 译文空行 ≤2，抓 LLM 拆行错位/漏行
4. **合并短句** — `merge_srt.py` 对 en+zh 各跑一次：间隔≤1s、累计≤15s、≤90 字(中)/150 字(英) 的连续块合成一句；TTS 调用次数减少约 45%，消除逐句硬静音
5. **vtv** — TTS + 对齐 + 字幕 + 合成（跳过 ASR/翻译）
6. 全部段完成后 `ffmpeg concat -c copy` 合并，总时长 ±10s 校验

**先翻译后合并**是关键顺序：合并产生的长段会诱发 LLM 拆行，而 `_run_text` 按行号 zip 输出 → 内容错位丢失 → 长静音。配套补丁在 `pyvideotrans/videotrans/translator/_base.py`（行数不匹配时逐行重翻）与翻译 prompt（`prompts/text/localllm.txt`）。

**断点续跑**：段输出存在跳过；`en.srt` 存在跳过 ASR；`zh-cn.srt` 存在跳过翻译。重跑某步 = 删对应文件。翻译重跑前若 `en.srt` 已合并，自动从 `en.srt.raw` 还原。

**注意**：翻译 prompt **不在**缓存 key 里，改 prompt 后必须删 `pyvideotrans/tmp/translate_cache/`。

## TTS 引擎（config.env `TTS_MODEL`）

| 引擎 | 说明 | 速度 |
|---|---|---|
| `s2-pro`（默认） | fish-speech S2 Pro，走 llama-swap（端口 5807） | RTF≈2.4-2.5（本机实测） |
| `auk` | 腾讯 AuK zero-shot，本地 shim（端口 5998，模拟 fish 接口） | base 32 步慢；`AUK_FLASH=1` 用 4 步蒸馏快 ~8x |

两引擎共用音色配置；TTS 缓存 key 含引擎（tts_type 30/36 隔离），换引擎自动重新合成，无需清缓存。AuK 需 `--cpu_offload`（24GB 卡），每段额外 ~2-4 分钟加载。

## 音色（config.env `VOICE_ROLE`）

- `clone`（默认）：克隆**原视频英文说话人**说中文 → 带外国口音（逐行裁原英文音频当参考）
- `<名字>`：固定中文参考音色（自然中文）。把 10-20s 干净中文人声放 `pyvideotrans/f5-tts/<名字>`，`VOICE_REF_TEXT` 填文字稿。换音色后旧缓存不命中，自动重合成

## Web 控制台（pipeline-web.py，:8090）

- 任务列表（扫 `~/视频/outputs/*/pipeline.log`：段进度/阶段/状态）、启动（自动改 config.env，先备份 `.bak`）、停止（SIGTERM 进程组）
- 日志 tail 300 行（5s 刷新）、GPU 占卡状态、`/ctx/<任务>` 一键复制排障上下文（参数+日志+nvidia-smi+进程）
- 页面零 LLM 调用、不占 GPU；有任务运行时启动按钮自动禁用（GPU 互斥）
- 前端更新后看右上角版本号（如 `v0926a`），强刷（Ctrl+Shift+R）加载新代码

## ⚠️ 硬约束

1. **流水线/AuK 运行期间不要找任何 agent 说话**（Claude Code 与 OpenClaw 共用 llama-swap 的 qwen3.8-27b，与 TTS 模型互斥，发消息会触发换模型打断任务）
2. 不要打开 pyvideotrans GUI（关窗会把 cfg.json 写回默认值；脚本每段前自动断言修复，但 GUI 仍会干扰）
3. 同时只跑一条流水线 / 一个 AuK 任务
4. GPU 互斥（24GB 单卡）：LLM 23.7GB / s2-pro ~18.5GB / AuK ~17GB / faster-whisper ~5GB，脚本自动等待+主动卸载，期间 agent 不可用

## 排障

```bash
tail -20 <out>/pipeline.log              # dub.sh 所有输出都在这
tail -20 <out>/auk-shim.log              # AuK 引擎日志 (TTS_MODEL=auk)
pgrep -af "dub.sh|auk-infer|auk-tts-shim"
nvidia-smi                               # 查占卡进程
./service.sh logs                        # Web 控制台自身日志
```

任务失败：网页打开 `/ctx/<任务名>` → "复制全部" → 整段贴给 agent 分析。

| 症状 | 原因 | 处理 |
|---|---|---|
| 一句话后大量空白/丢句 | LLM 拆行错位 | 已修（先翻译后合并+逐行重翻+QC）；复发则删该段 `zh-cn.srt` 重跑 |
| 翻译结果没变（改了 prompt） | prompt 不在缓存 key | 删 `pyvideotrans/tmp/translate_cache/` |
| AuK shim 启动即退出 | 权重缺失或 OOM（LLM 未卸载） | 看 `<out>/auk-shim.log`；`nvidia-smi` 查残留进程 |
| s2-pro 加载超时 | 首次冷编译超 healthCheckTimeout | 跑 `scripts/warmup-s2pro-compile.sh`（幂等） |
