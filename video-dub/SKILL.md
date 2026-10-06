---
name: video-dub
description: 英文视频→中文配音流水线：ASR→LLM翻译→合并短句→TTS(s2-pro/AuK 双引擎，可在提示词指定)→合成，自动30分钟切段、断点续跑、自动QC(字幕行数/时长)、最终合并。另含 AuK 单片段音频处理(zero-shot/Instruct TTS、改词/改歌词/去口音/降噪/人声提取等)。用户要求把视频配成中文/翻译配音、指定TTS引擎、或对单段音频做TTS/编辑时使用。English video to Chinese dubbing pipeline (dual TTS engines: s2-pro/AuK, prompt-selectable) plus AuK single-clip audio generation/editing.
---

# Video Dub（配音流水线 + AuK 音频工具）

## 何时使用
- 用户要求把（英文）视频配成中文 / 翻译配音 → dub.sh
- 用户在提示词里指定 TTS 引擎（"用AuK配音" / "用s2-pro"）→ 先改 config.env（见下）
- 单片段音频处理（TTS/克隆/改词/改歌词/去口音/降噪/提取人声）→ AuK 辅助工具（见下），不走整片流水线
- 输入是链接时：先用 video-fetch 取片，再把本地路径传给 dub.sh

## 用法
```bash
# 整片（自动切段+配音+合并）
~/workspace/money_code/videotrans/video-pipeline/scripts/dub.sh /path/to/video.mp4 --out /home/loomz/视频/outputs/<名>

# 已有切分目录（断点续跑）
~/workspace/money_code/videotrans/video-pipeline/scripts/dub.sh /path/to/segments_dir --segments-dir --out <out> --final <最终文件路径>
```
必须后台启动（nohup；机器可能没装 tmux，别用 tmux）：
```bash
nohup <上面的完整命令> >/dev/null 2>&1 &
```

## Web 控制台（pipeline-web.py，零依赖标准库）
- 启动：`nohup python3 ~/workspace/money_code/videotrans/video-pipeline/pipeline-web.py >/tmp/pipeline-web.log 2>&1 &` → http://127.0.0.1:8090（仅本机）
- 功能：任务列表（段进度/阶段/状态，扫 outputs/*/pipeline.log）+ 启动（自动改 config.env，先备份 config.env.bak）+ 日志 tail 300 行（5s 刷新）+ GPU 占卡状态 + 停止（SIGTERM 进程组）
- 排障：任务失败后打开 `/ctx/<任务名>` → "复制全部"（参数+日志末50行+nvidia-smi+进程）→ 整段贴给 agent 分析
- 页面本身零 LLM 调用、不占 GPU；有流水线运行时启动按钮自动禁用（GPU 互斥，同时只跑一条）

## TTS 引擎选择（提示词指定）
config.env `TTS_MODEL`（dub.sh 启动前按用户要求改这一行）：
- `"s2-pro"`（**默认**）：fish-speech S2 Pro，走 llama-swap；--compile 后单请求 10-18s
- `"auk"`：腾讯 AuK zero-shot，本地 shim（`scripts/auk-tts-shim.py`，端口 5998，模拟 fish /v1/tts 接口）；质量更高但慢（base 32 步 + cpu_offload；每段额外 ~2-4 分钟：等 GPU + 加载模型）
- 用户说"用 AuK 配音/用 AuK 当 TTS 引擎"→ `TTS_MODEL="auk"`；说"用 s2-pro"或未指定 → `"s2-pro"`
- AuK 提速：config.env `AUK_FLASH=1`（AuK-Flash 4 步蒸馏，快 ~8x，质量略降）
- 两引擎共用同一音色配置（VOICE_ROLE，见下）；TTS 缓存 key 含引擎（tts_type 30/36 隔离），换引擎自动重新合成，无需手动清缓存

## ⚠️ 硬约束（启动前必须告知用户）
1. **流水线/AuK 运行期间不要找任何 agent 说话**（Claude Code 与 OpenClaw 共用 llama-swap 的 qwen3.8-27b，与 TTS 模型 s2-pro、AuK 互斥，发消息会触发换模型打断任务）
2. 不要打开 pyvideotrans GUI（关窗会把 cfg.json 写回默认值；脚本每段前会自动断言修复，但 GUI 仍会干扰）
3. 同时只跑一条流水线 / 一个 AuK 任务
4. GPU 互斥（24GB 单卡）：LLM 23.7GB / s2-pro ~18.5GB / AuK ~17GB(cpu_offload) / faster-whisper ~5GB，脚本自动等待+主动卸载，但期间 agent 不可用
5. 查进度：`tail -20 <out>/pipeline.log`（dub.sh 所有输出都写这里；AuK shim 日志 `<out>/auk-shim.log`）；`pgrep -af "dub.sh|auk-infer|auk-tts-shim"` 看是否还在跑

## 音色（config.env，两引擎通用）
- `VOICE_ROLE="clone"`：克隆**原视频英文说话人**的音色说中文 → 带外国口音（逐行裁原英文音频当参考）
- `VOICE_ROLE="<名字>"`（如 `zhvoice.wav`）：用固定中文参考音色，自然中文。需要：
  1. 准备 10-20s 干净中文人声（单人、无 BGM/噪音），放 `pyvideotrans/f5-tts/<名字>`
  2. `VOICE_REF_TEXT` 填这段音频里说的话（文字稿）
  3. 改完 config.env 即可，dub.sh 每段前自动断言 params.json
- 换音色后旧 TTS 缓存不命中（缓存 key 含音色），会自动重新合成

## 合并短句（merge_srt.py，自动）
- **翻译后、TTS 前**自动合并 `en.srt`+`zh-cn.srt`（必须先翻译后合并：长段会诱发 LLM 拆行 → 按行号 zip 错位 → 丢句+长静音）：间隔≤1s、累计≤15s、≤90字(中)/150字(英) 的连续块合成一句
- 作用：TTS 连续自然（消除逐句硬静音间隔）、减少为对齐原时长而加速、TTS 调用次数减少约 45%
- 无损（合并前后文本校验一致才写入）、幂等；`*.srt.raw` 是合并前备份，重复运行从 .raw 重新合并
- 调参：`scripts/merge_srt.py <srt> --gap 1000 --max-dur 15000 --max-chars 90`

## 效率
- TTS 是瓶颈（实测占单段 ~90%）。fish-speech 服务端完全串行（async 阻塞事件循环 + 单 worker + KV cache batch=1），`TTS_THREADS` 无效，不要指望并发；AuK shim 同为锁串行
- s2-pro 已优化（2026-09-13）：
  1. s2-pro 带 `--compile`（torch.compile + inductor 磁盘缓存，llama-swap config.yaml）→ 单请求 27.6s → 预期 10-18s
  2. 每请求 `empty_cache/gc` 默认关闭（`FISH_TTS_EMPTY_CACHE=1` 恢复）
  3. **dub.sh 第 0 步自动预热**（`scripts/warmup-s2pro-compile.sh`，幂等）：首次运行等 GPU 空闲 → 建 inductor 缓存（一次性 ~10-20min）→ 写 marker；之后毫秒级跳过。marker: `~/.cache/s2pro-compile-warmup.done`
  4. dub.sh ASR 前自动等 GPU ≥5GB（防 LLM 占卡 OOM）；QC 行数不一致自动重翻一次（防 LLM 拆行错位）
- 预期：s2-pro 每 30 分钟段约 22-35 分钟（优化前 ~47min），3 小时视频约 2.5-3.5 小时；AuK 引擎更慢（每段多 ~2-4 分钟加载 + 32 步生成）

## 断点续跑语义
- 段输出 `<out>/seg_NNN/seg_NNN.mp4` 存在 → 整段跳过
- `en.srt` 存在 → 跳过 ASR；`zh-cn.srt` 存在 → 跳过翻译（翻译前若 en.srt 已合并，自动从 `en.srt.raw` 还原）
- 重跑某段 TTS/合成：删该段的 `seg_NNN.mp4`、`zh-cn.m4a`（保留 srt）
- 重跑某段翻译：删 `zh-cn.srt`（en.srt 若已合并会自动从 `en.srt.raw` 还原）
- 换翻译模型/改 prompt 后：清空 `pyvideotrans/tmp/translate_cache/`（prompt 不在缓存 key 里，必须手动清）
- 换 TTS 引擎/音色：无需手动清（缓存 key 含 tts_type 与音色，自动重新合成）

## 自动 QC
- 每段：合并前块数（`*.srt.raw`）en vs zh 偏差 ≤ max(5, 2%)（抓丢句；模型合并短句属正常）；输出时长 ≈ 输入 ±5s
- 合并：总时长 ≈ 输入总时长 ±10s
- 任一 QC 失败 → 流水线停止并报错

## AuK 辅助工具（单片段音频处理）
腾讯开源语音基础模型（2026-09），14 个任务共用自然语言指令接口。脚本：`scripts/auk.sh`（自动等 GPU + `--cpu_offload` + 校验输出）。
```bash
~/workspace/money_code/videotrans/video-pipeline/scripts/auk.sh \
  --instruction "Say the following with the same voice: '你好，世界'" \
  --audio /path/ref.wav --gen_seconds 3 [--flash] [--output /path/out.wav]
```
- `--flash`：AuK-Flash（4 步，快 ~8x）；默认 base（32 步，质量高）
- `--gen_seconds`：目标时长秒。**无 --audio（Instruct TTS）时必填**；编辑类可省略（与输入等长）；TTS 按文本估（中文约 4 字/秒，宁多勿少）
- 输出默认 `~/视频/outputs/auk/<时间戳>_<base|flash>.wav`，同目录 `.log` 为完整日志
- 耗时较长时后台跑：`nohup <命令> >/dev/null 2>&1 &`，然后 `tail -f ~/视频/outputs/auk/<输出>.log`

### 指令模板（COOKBOOK，直接套用）
| 任务 | 指令模板 | 备注 |
|---|---|---|
| Zero-shot TTS | `Say the following with the same voice: "{文本}"` | --audio=参考音频 + --gen_seconds |
| Instruct TTS | `请基于下面的描述: "{声音描述}",生成语音内容"{文本}".` | 无 --audio，--gen_seconds 必填 |
| 内容编辑-替换 | `把'{原文}'改成'{新文}'` / `Replace '{orig}' with '{new}'.` | --audio=原录音 |
| 内容编辑-插入 | `在'{锚点}'前面/后面加上'{内容}'` | |
| 内容编辑-删除 | `删掉'{内容}'` | |
| 歌词编辑 | `把这段歌词中的"{原歌词}"改成"{新歌词}".` | 输入必须干声人声（有 BGM 先提取人声） |
| 音调 | `将音调升高/降低{1/2/3}个半音。` | |
| 语速 | `将语速调整为{0.5/0.75/1.25/1.5/2.0}倍。` | 不变调 |
| 音量 | `将音量升高/降低{5/10/15}分贝。` | |
| 情感 | `将情感转变为{开心/愤怒/悲伤/恐惧/惊讶/厌恶/平静/兴奋}。` | |
| 音色 | `请将这段音频的音色修改为符合以下描述的声音："{音色描述}".` | |
| 去口音 | `请去掉这段语音里的方言口音，保持说话人音色一致。` | |
| 非语言删除 | `删除音频中所有的{换气声/笑声/咳嗽声等}。` | |
| 耳语 | `用小声耳语的方式把这段话说出来。` | 正常→耳语 |
| 降噪 | `请只去除背景噪声，保留其他内容，输出等长结果。` | |
| 说话人分离 | `只保留第{N}个开始说话的人，去掉其余说话人。` | |
| 人声提取 | `请只保留歌声，其余声音都去掉。` | |
| 目标说话人 | `请只保留说"{内容}"的人，去掉其他说话人。` | |

### Prompt Enhancer（自由形式请求）
PE 本身需要 LLM（llama-swap qwen3.8-27b）→ **两阶段**：先跑 PE（此时 LLM 已加载，勿卸载）生成命令，再跑 auk.sh（自动卸载 LLM）。
```bash
cd ~/workspace/money_code/videotrans/AuK
# .env（一次性创建，若不存在）:
# LLM_API_KEY=dummy
# LLM_BASE_URL=http://127.0.0.1:8080/upstream/qwen3.8-27b/v1
# LLM_MODEL_NAME=qwen3.8-27b
set -a; source ./.env; set +a
.venv/bin/python src/auk/infer/pe.py --audio in.wav --instruction "自由形式请求" --asr auto
# 输出可直接运行的 auk-infer 命令 → 转成等价的 auk.sh 调用
```

## 排障表
| 现象 | 原因 | 处理 |
|---|---|---|
| 首次运行卡在 "TTS compile 预热" 十几分钟 | 一次性 inductor 冷编译（正常，仅 s2-pro） | 等；超时则 GPU 空闲时手动 `bash scripts/warmup-s2pro-compile.sh` |
| 预热后 s2-pro 加载仍超 120s 健康检查 | inductor 缓存被清/torch 升级 | 删 `~/.cache/s2pro-compile-warmup.done` 重新预热 |
| Could not parse response ... length limit | max_tokens 不够 | 脚本自动断言 localllm_max_token=4096 |
| 翻译正文为空 finish_reason=length | 思考模式吃 token | _localllm.py 已带 enable_thinking:false，勿删 |
| clone 参考音频不存在 | FISHTTS 不在 SUPPORT_CLONE | tts/__init__.py 已修，勿回退 |
| 翻译阶段秒过没翻译 | 残留旧 zh-cn.srt | 删掉重跑 |
| 译文乱抄 prompt / 丢行 | 弱模型或旧缓存 | 用 qwen3.8-27b + 清 translate_cache |
| 字幕挡画面 | subtitle_type | config.env SUBTITLE_TYPE=0 |
| 中文像外国人说的 | VOICE_ROLE=clone 克隆的是英文说话人 | 换中文参考音色（见"音色"节） |
| 连续短句之间长停顿 | 逐行 TTS 后按时间轴补静音 | merge_srt.py 已自动合并短句；还明显则调大 --max-dur |
| 一句话后大量空白/丢句 | 合并前翻译: 长段诱发 LLM 拆行, 按行号 zip 错位 | 已修: 先翻译后合并 + 行数不匹配逐行重翻 + 合并前 QC; 仍复发则删该段 zh-cn.srt 重跑 |
| 个别句突然加速 | 中文比原句时长长，为对齐被 atempo 加速 | 已缓解（合并短句+prompt 要求简洁）；仍多则检查译文是否啰嗦 |
| AuK: FATAL 权重/编码器缺失 | 文件被删/移动 | 按报错路径手动放回 |
| AuK: shim 启动即退出 / 健康检查超时 | 权重缺失或 OOM（LLM 未卸载） | 看 `<out>/auk-shim.log`；`nvidia-smi` 查残留进程 |
| AuK: 输出时长太短/语速赶 | --gen_seconds 与文本长度不匹配 | shim 按中文~4.2字/秒自动估；auk.sh 手动调 --gen_seconds |
| AuK: 中文质量一般 / 生成慢 | base 32 步 + cpu_offload | 用 --flash（4 步，快 ~8x） |
| AuK: PE 报错 | .env 未配置或 LLM 未加载 | 检查 .env；PE 阶段 LLM 必须加载（AuK 运行中不能跑 PE） |
