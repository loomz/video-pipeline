#!/usr/bin/env python3
"""auk-tts-shim.py — 用 AuK 模拟 fish-speech /v1/tts 接口, 供 pyvideotrans 当 TTS 后端

协议 (与 _fishtts.py 对齐):
  POST /v1/tts  {"text": str, "references": [{"audio": "<base64 wav>", "text": str}]} → 响应体=原始 WAV 字节
  GET  /health  模型加载完成后 200, 加载中 503

- 用 AuK venv 的 python 运行; 启动时加载一次 AukInfer (cpu_offload=True, 约 1-3 分钟)
- 请求用锁串行 (cpu_offload 非线程安全; 与 s2-pro 单 worker 行为一致)
- zero-shot TTS: "Say the following with the same voice: '<文本>'", 参考音频=客户端发来的那段
  (dub 流水线 clone 模式下是原英文行裁片 → 克隆原说话人音色说中文)
- gen_seconds 按文本长度估算 (中文~4.2字/秒, 英文~14字符/秒); AuK 会自适应语速塞满时长,
  略估长一点由 pyvideotrans voice_autorate(atempo) 拉回, 估短了会赶语速
环境变量: AUK_DIR / AUK_CKPT / AUK_QWEN / AUK_SHIM_PORT
"""
import base64
import io
import logging
import os
import re
import tempfile
import threading
import time
from contextlib import asynccontextmanager

import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

AUK_DIR = os.environ.get("AUK_DIR", "/home/loomz/workspace/money_code/videotrans/AuK")
CKPT = os.environ.get(
    "AUK_CKPT", f"{AUK_DIR}/ckpts/AuK/auk_base.safetensors"
)
QWEN = os.environ.get("AUK_QWEN", f"{AUK_DIR}/ckpts/Qwen2.5-Omni-3B")
PORT = int(os.environ.get("AUK_SHIM_PORT", "5998"))
CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("auk-shim")

from auk.infer.infer_auk import AukInfer  # noqa: E402

_ready = threading.Event()
_gen_lock = threading.Lock()
_engine = None


def _load():
    global _engine
    t0 = time.time()
    log.info(f"Loading AuK: ckpt={CKPT} qwen={QWEN} (cpu_offload=True)")
    _engine = AukInfer(
        config_path=os.path.join(os.path.dirname(os.path.abspath(CKPT)), "config.yaml"),
        ckpt_path=CKPT,
        qwen_path=QWEN,
        cpu_offload=True,
    )
    _ready.set()
    log.info(f"AuK loaded in {time.time() - t0:.0f}s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _load()  # 阻塞 1-3 分钟; 期间 uvicorn 已绑端口, /health 返回 503
    yield


app = FastAPI(lifespan=lifespan)


class Ref(BaseModel):
    audio: str = ""
    text: str = ""


class TTSRequest(BaseModel):
    text: str
    references: list[Ref] = []


def _estimate_seconds(text: str) -> float:
    cjk = len(CJK_RE.findall(text))
    other = len(re.sub(r"\s+", "", text)) - cjk
    return max(2.0, cjk / 4.2 + other / 14.0)


@app.get("/health")
def health():
    if not _ready.is_set():
        raise HTTPException(503, "loading")
    return {"status": "ok"}


@app.post("/v1/tts")
def tts(req: TTSRequest):
    if not _ready.is_set():
        raise HTTPException(503, "loading")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    ref_b64 = req.references[0].audio if req.references else ""
    ref_path = None
    try:
        if ref_b64:
            fd, ref_path = tempfile.mkstemp(suffix=".wav")
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(ref_b64))
        gen_seconds = _estimate_seconds(text)
        content = [{"type": "text", "text": f"Say the following with the same voice: '{text}'"}]
        if ref_path:
            content.append({"type": "audio", "audio": ref_path})
        messages = [{"role": "user", "content": content}]
        t0 = time.time()
        with _gen_lock:
            audio, sr = _engine.generate(
                messages, audio=ref_path, gen_seconds=gen_seconds
            )
        dt = time.time() - t0
        a = audio.detach().to(torch.float32).cpu()
        if a.ndim > 1:
            a = a[0]
        buf = io.BytesIO()
        sf.write(buf, a.numpy(), sr, subtype="PCM_16", format="WAV")
        log.info(
            f"TTS ok: {len(text)}字 gen_seconds={gen_seconds:.1f} "
            f"耗时={dt:.1f}s 输出={a.shape[-1] / sr:.1f}s@{sr}Hz {len(buf.getvalue()) // 1024}KB"
        )
        return Response(content=buf.getvalue(), media_type="audio/wav")
    except Exception:
        log.exception("TTS failed")
        raise HTTPException(500, "AuK generate failed")
    finally:
        if ref_path and os.path.exists(ref_path):
            os.unlink(ref_path)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
