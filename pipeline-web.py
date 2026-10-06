#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline-web.py — video-pipeline 本地 Web 控制台 (零依赖, 仅标准库)

功能:
  1. 任务列表: 扫描 ~/视频/outputs/*/pipeline.log, 解析 段进度/阶段/状态
  2. 启动任务: 改 config.env (自动备份 config.env.bak) → 独立进程组跑 dub.sh
  3. 日志 tail: 末 300 行, 5s 自动刷新
  4. GPU 状态: nvidia-smi 空闲显存/利用率/占卡进程
  5. 停止: SIGTERM 进程组 (仅当 dub.sh 是会话首进程; 手动启动的只杀主进程, 防误杀终端)
  6. 排障上下文: 一键生成 参数+日志末50行+nvidia-smi+进程 → 复制给 agent 分析

设计约束:
  - 页面本身不做任何 LLM 调用, 纯确定性操作, 批量运行期间零干扰
  - 同时只允许一条流水线 (GPU 互斥), 有任务运行时禁用启动按钮
  - 仅监听 127.0.0.1

用法:
  python3 pipeline-web.py [port]      # 默认 8090
  nohup python3 pipeline-web.py >/tmp/pipeline-web.log 2>&1 &
"""
import html
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
CONFIG_ENV = PIPELINE_DIR / "config.env"
CONFIG_BAK = PIPELINE_DIR / "config.env.bak"
DUB_SH = PIPELINE_DIR / "scripts" / "dub.sh"
OUTPUT_DIR = Path(os.path.expanduser("~/视频/outputs"))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
VERSION = "v0926a"   # 页面右上角显示; 每次改前端代码就 bump, 用户强刷后看到新版本号才算加载了新代码
TAIL_LINES = 300   # 日志 tail 行数
CTX_LINES = 50     # 排障上下文取日志行数
MAX_TASKS = 20

# ---------------------------------------------------------------- 后端逻辑

def read_config_env():
    cfg = {}
    try:
        for line in CONFIG_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return cfg


def set_config_value(key, value):
    text = CONFIG_ENV.read_text(encoding="utf-8")
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pat.search(text):
        text = pat.sub(f'{key}="{value}"', text, count=1)
    else:
        text = text.rstrip("\n") + f'\n{key}="{value}"\n'
    CONFIG_ENV.write_text(text, encoding="utf-8")


def run_cmd(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def find_dub_processes():
    """[(pid, cmdline)] 运行中的 dub.sh 进程"""
    out = run_cmd(["pgrep", "-af", "dub.sh"])
    procs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, cmd = line.partition(" ")
        if pid.isdigit():
            procs.append((int(pid), cmd))
    return procs


STAGE_RULES = [
    (r"等待释放", "等待GPU"),
    (r"TTS compile 预热", "compile预热"),
    (r"切分: 每", "切分"),
    (r"\[1/4\] ASR", "ASR"),
    (r"\[2/4\] sts", "翻译"),
    (r"QC 失败", "QC重翻"),
    (r"启动 AuK TTS shim", "AuK加载"),
    (r"\[4/4\]", "TTS+合成"),
    (r"合并 →", "最终合并"),
]


def parse_task(d, procs):
    logf = d / "pipeline.log"
    try:
        mtime = logf.stat().st_mtime
        text = logf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        mtime, text = 0, ""
    lines = text.splitlines()
    total = cur = done = 0
    stage = "启动中" if lines else "无日志"
    fatal_line = None
    finished = False
    input_path = None
    for ln in lines:
        m = re.search(r"共 (\d+) 段", ln)
        if m:
            total = int(m.group(1))
        m = re.search(r"\[(\d+)/(\d+)\] seg_\d+ 开始", ln)
        if m:
            cur = int(m.group(1))
        if re.search(r"seg_\d+ 完成", ln):
            done += 1
        if "FATAL" in ln and fatal_line is None:
            fatal_line = ln.strip()
        if "=== 完成" in ln:
            finished = True
        if input_path is None:
            m = re.search(r"=== dub\.sh 启动: 输入=(\S+)", ln)
            if m:
                input_path = m.group(1)
        for pat, s in STAGE_RULES:
            if re.search(pat, ln):
                stage = s
    if finished:
        stage = "已完成"
    out_str = str(d)
    running = any(
        out_str in cmd or (input_path and input_path in cmd)
        for _, cmd in procs
    )
    if running:
        status = "running"
    elif finished:
        status = "done"
    elif fatal_line:
        status = "failed"
    else:
        status = "stopped"
    return {
        "name": d.name, "out": out_str, "status": status, "stage": stage,
        "cur": cur, "total": total, "done": done,
        "last": lines[-1].strip() if lines else "",
        "mtime": mtime, "fatal": fatal_line,
    }


def scan_tasks():
    procs = find_dub_processes()
    tasks = []
    if OUTPUT_DIR.is_dir():
        for d in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir() and (d / "pipeline.log").is_file():
                tasks.append(parse_task(d, procs))
            if len(tasks) >= MAX_TASKS:
                break
    return tasks, bool(procs)


def gpu_status():
    g = run_cmd(["nvidia-smi",
                 "--query-gpu=memory.total,memory.used,utilization.gpu,temperature.gpu",
                 "--format=csv,noheader,nounits"]).strip()
    if not g:
        return {"error": "nvidia-smi 不可用"}
    try:
        total, used, util, temp = (int(x) for x in g.splitlines()[0].split(", "))
    except ValueError:
        return {"error": "nvidia-smi 输出异常"}
    apps = []
    a = run_cmd(["nvidia-smi",
                 "--query-compute-apps=pid,used_memory,process_name",
                 "--format=csv,noheader,nounits"])
    for line in a.splitlines():
        parts = line.split(", ", 2)
        if len(parts) == 3 and parts[1].isdigit():
            apps.append({"pid": parts[0], "mem": int(parts[1]), "name": parts[2]})
    return {"total": total, "used": used, "free": total - used,
            "util": util, "temp": temp, "apps": apps}


VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm",
              ".m4v", ".ts", ".wmv", ".mpg", ".mpeg"}


def browse_dir(path):
    p = Path(os.path.expanduser(path.strip() or "~")).resolve()
    if not p.is_dir():
        return {"error": f"不是目录: {p}"}
    dirs, files = [], []
    try:
        entries = list(p.iterdir())
    except OSError as e:
        return {"error": f"无法读取: {e}"}
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                dirs.append(e.name)
            elif e.suffix.lower() in VIDEO_EXTS:
                st = e.stat()
                files.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            continue
    dirs.sort(key=str.lower)
    files.sort(key=lambda f: -f["mtime"])   # 最近的在前
    parent = p.parent
    return {"path": str(p),
            "parent": str(parent) if str(parent) != str(p) else None,
            "dirs": dirs, "files": files}


def start_task(video, tts_model, voice_role, auk_flash):
    procs = find_dub_processes()
    if procs:
        return {"ok": False, "error": f"已有流水线在运行 (pid {procs[0][0]}), 同时只允许一条"}
    if not DUB_SH.is_file():
        return {"ok": False, "error": f"找不到 {DUB_SH}"}
    v = os.path.expanduser(video.strip())
    if not v:
        return {"ok": False, "error": "视频路径为空, 请先输入或点「浏览…」选择"}
    if not os.path.isfile(v):
        return {"ok": False, "error": f"视频文件不存在: {v}"}
    if tts_model not in ("auk", "s2-pro"):
        return {"ok": False, "error": f"未知 TTS 引擎: {tts_model}"}
    voice_role = voice_role.strip() or "clone"
    try:
        CONFIG_BAK.write_text(CONFIG_ENV.read_text(encoding="utf-8"), encoding="utf-8")
        set_config_value("TTS_MODEL", tts_model)
        set_config_value("VOICE_ROLE", voice_role)
        set_config_value("AUK_FLASH", "1" if auk_flash else "0")
    except OSError as e:
        return {"ok": False, "error": f"修改 config.env 失败: {e}"}
    name = Path(v).stem
    out = OUTPUT_DIR / name
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # 启动前先卸载 LLM 释放显存 (失败不阻塞启动, dub.sh 自己也会等 GPU)
    try:
        swap = read_config_env().get("SWAP_BASE", "http://127.0.0.1:8080")
        req = urllib.request.Request(f"{swap}/api/models/unload/qwen3.8-27b", method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            r.read()
        print(f"[start_task] 已卸载 qwen3.8-27b", flush=True)
    except Exception as e:
        print(f"[start_task] unload 失败(继续启动): {e}", flush=True)
    try:
        proc = subprocess.Popen(
            ["bash", str(DUB_SH), v, "--out", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(PIPELINE_DIR),
        )
    except OSError as e:
        return {"ok": False, "error": f"启动失败: {e}"}
    return {"ok": True, "task": name, "pid": proc.pid, "out": str(out)}


def stop_task(task_name=None):
    procs = find_dub_processes()
    if task_name:
        target = str(OUTPUT_DIR / task_name)
        procs = [(p, c) for p, c in procs if target in c]
    if not procs:
        return {"ok": False, "error": "没有匹配的流水线进程"}
    pids = [p for p, _ in procs]
    for pid in pids:
        try:
            pgid = os.getpgid(pid)
            if pgid == pid:
                os.killpg(pgid, signal.SIGTERM)   # 会话首进程(本工具启动) → 杀整组
            else:
                os.kill(pid, signal.SIGTERM)      # 手动启动 → 只杀主进程, 防误杀终端
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(3)
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            pass
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return {"ok": True, "term_sent": pids, "still_alive_after_3s": alive}


def troubleshoot_context(task_name):
    d = OUTPUT_DIR / task_name
    logf = d / "pipeline.log"
    p = [
        "=== 排障上下文 (video-pipeline) ===",
        f"生成时间: {time.strftime('%F %T')}",
        f"任务: {task_name}",
        f"输出目录: {d}",
        "",
        "--- config.env (相关项) ---",
    ]
    cfg = read_config_env()
    for k in ("SWAP_BASE", "TRANSLATE_MODEL", "TTS_MODEL", "AUK_FLASH", "TTS_TYPE",
              "VOICE_ROLE", "VOICE_REF_TEXT", "ASR_MODEL", "TTS_THREADS", "SUBTITLE_TYPE"):
        if k in cfg:
            p.append(f"{k}={cfg[k]}")
    p += ["", f"--- pipeline.log 最后 {CTX_LINES} 行 ---"]
    if logf.is_file():
        try:
            lines = logf.read_text(encoding="utf-8", errors="replace").splitlines()
            p.extend(lines[-CTX_LINES:])
        except OSError as e:
            p.append(f"(读取失败: {e})")
    else:
        p.append("(无 pipeline.log)")
    p += ["", "--- nvidia-smi ---"]
    p.append(run_cmd(["nvidia-smi"], timeout=10) or "(nvidia-smi 无输出)")
    p += ["", "--- 相关进程 ---"]
    p.append(run_cmd(["bash", "-c",
                      'pgrep -af "dub.sh|auk-infer|auk-tts-shim" || echo "(无相关进程)'])
             or "(无相关进程)")
    return "\n".join(p)


def _safe_name(name):
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name

# ---------------------------------------------------------------- 前端页面

HTML_DASH = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="/video-pipeline.png">
<title>video-pipeline 控制台</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0f1216;color:#d5dbe3;font:14px/1.5 system-ui,"Noto Sans CJK SC",sans-serif;padding:16px 20px 40px}
h1{font-size:18px;margin:0 0 12px}
h1 span{color:#5c6773;font-weight:normal;font-size:13px;margin-left:8px}
#gpu{background:#161b22;border:1px solid #262d37;border-radius:8px;padding:10px 14px;margin-bottom:16px;font-family:ui-monospace,monospace;font-size:13px}
#gpu b{color:#7ec699}
form#start{background:#161b22;border:1px solid #262d37;border-radius:8px;padding:12px 14px;margin-bottom:16px;display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end}
form#start label{font-size:12px;color:#8b96a3;display:block;margin-bottom:3px}
form#start input[type=text]{background:#0d1117;border:1px solid #2c3542;color:#d5dbe3;border-radius:6px;padding:6px 8px;font-size:13px}
#video{width:340px}
select{background:#0d1117;border:1px solid #2c3542;color:#d5dbe3;border-radius:6px;padding:6px 8px}
button{background:#238636;border:0;color:#fff;border-radius:6px;padding:7px 16px;font-size:13px;cursor:pointer}
button:disabled{background:#2c3542;color:#8b96a3;cursor:not-allowed}
button.danger{background:#b62324}
button.ghost{background:#21262d;border:1px solid #2c3542;color:#d5dbe3;padding:3px 10px;font-size:12px}
#msg{font-size:12px;width:100%;margin:0}
#msg.err{color:#f85149}#msg.ok{color:#7ec699}
table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #262d37;border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #21262d;font-size:13px;vertical-align:top}
th{background:#1c232c;color:#8b96a3;font-weight:normal;font-size:12px}
tr:last-child td{border-bottom:0}
td.last{font-family:ui-monospace,monospace;font-size:12px;color:#8b96a3;max-width:420px;word-break:break-all}
.st{padding:2px 8px;border-radius:10px;font-size:12px;white-space:nowrap}
.st.running{background:#12351f;color:#7ec699}
.st.done{background:#1c232c;color:#8b96a3}
.st.failed{background:#3d1517;color:#f85149}
.st.stopped{background:#3a2d12;color:#d29922}
a{color:#58a6ff;text-decoration:none}
.empty{color:#5c6773;padding:20px;text-align:center}
.brow{padding:4px 16px;cursor:pointer;border-radius:4px;margin:1px 8px}
.brow:hover{background:#21262d}
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:10px">
  <h1 style="margin:0"><img src="/video-pipeline.png" width="22" height="22" style="vertical-align:-5px;margin-right:8px;border-radius:4px">video-pipeline 控制台<span>127.0.0.1 · 5s 自动刷新 · 页面零 LLM 调用, 排障交给 agent · <a href="/" title="页面代码更新后需强刷一次 (Ctrl+Shift+R)">__VERSION__</a></span></h1>
  <div style="display:flex;gap:8px">
    <button class="ghost" type="button" onclick="refresh()" title="立即刷新监控信息">⟳ 刷新</button>
    <button class="ghost" type="button" id="unloadBtn" onclick="unloadModel()" title="POST llama-swap /api/models/unload/qwen3.8-27b, 释放显存给流水线">卸载模型</button>
  </div>
</div>
<div id="gpu">GPU: 加载中…</div>
<form id="start">
  <div><label>视频路径</label><div style="display:flex;gap:6px"><input type="text" id="video" name="video" placeholder="/home/loomz/视频/downloads/xxx.mp4"><button type="button" class="ghost" onclick="openBrowse()">浏览…</button></div></div>
  <div><label>TTS 引擎</label><select name="tts_model"><option value="auk">auk</option><option value="s2-pro">s2-pro</option></select></div>
  <div><label>音色</label><input type="text" id="voice" name="voice_role" value="clone" style="width:110px"></div>
  <div><label>AuK-Flash</label><label style="display:flex;gap:4px;align-items:center;margin:0;font-size:12px;color:#8b96a3"><input type="checkbox" name="auk_flash" value="1"><span>快速8x</span></label></div>
  <div><button id="startBtn" type="submit">启动流水线</button></div>
  <p id="msg"></p>
</form>
<table>
<thead><tr><th>任务</th><th>状态</th><th>进度</th><th>当前阶段</th><th>最新日志</th><th>操作</th></tr></thead>
<tbody id="rows"><tr><td colspan=6 class="empty">加载中…</td></tr></tbody>
</table>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PAGE_V="__VERSION__";   // 随 /api/start 提交, 服务端日志可核对前端版本
let cfgLoaded=false;
async function refresh(){
  let s;
  try{ s=await (await fetch('/api/state')).json(); }
  catch(e){ $('#gpu').textContent='状态: 刷新失败 '+e; return; }
  if(s.cfg){
    DL_DIR=s.cfg.DOWNLOAD_DIR||'/home/loomz/视频';
    if(!cfgLoaded){
      cfgLoaded=true;
      if(s.cfg.VOICE_ROLE) $('#voice').value=s.cfg.VOICE_ROLE;
      if(s.cfg.TTS_MODEL==='s2-pro'||s.cfg.TTS_MODEL==='auk')
        document.querySelector('select[name=tts_model]').value=s.cfg.TTS_MODEL;
    }
  }
  const g=s.gpu;
  if(g.error){ $('#gpu').textContent='GPU: '+g.error; }
  else{
    const apps=(g.apps||[]).map(a=>`${a.name.replace(/.*\\//,'')} ${a.mem}MiB(pid ${a.pid})`).join(', ')||'无';
    $('#gpu').innerHTML=`GPU0: 空闲 <b>${(g.free/1024).toFixed(1)}</b> / ${(g.total/1024).toFixed(1)} GB · 已用 ${(g.used/1024).toFixed(1)} GB · util ${g.util}% · ${g.temp}°C &nbsp;|&nbsp; 占卡: ${esc(apps)}`;
  }
  $('#startBtn').disabled = !!s.running;
  const rows=$('#rows');
  if(!s.tasks.length){ rows.innerHTML='<tr><td colspan=6 class="empty">暂无任务 (outputs/ 下没有 pipeline.log)</td></tr>'; return; }
  rows.innerHTML=s.tasks.map(t=>{
    const st={running:'运行中',done:'已完成',failed:'失败',stopped:'已停止'}[t.status]||t.status;
    const prog=t.total?`段 ${t.cur||0}/${t.total} (完成 ${t.done})`:'—';
    const act=[`<a href="/task/${encodeURIComponent(t.name)}">日志</a>`];
    if(t.status==='running') act.push(`<button class="danger" onclick="stopT('${esc(t.name)}')">停止</button>`);
    act.push(`<a href="/ctx/${encodeURIComponent(t.name)}"><button class="ghost">排障</button></a>`);
    return `<tr><td><a href="/task/${encodeURIComponent(t.name)}" title="${esc(t.out)}">${esc(t.name)}</a></td>
      <td><span class="st ${t.status}">${st}</span></td><td>${prog}</td><td>${esc(t.stage)}</td>
      <td class="last">${esc(t.last)}</td><td>${act.join(' ')}</td></tr>`;
  }).join('');
}
async function stopT(name){
  if(!confirm(`停止任务 ${name} ? (SIGTERM 整个进程组)`)) return;
  const r=await (await fetch('/api/stop?task='+encodeURIComponent(name),{method:'POST'})).json();
  alert(JSON.stringify(r));
  refresh();
}
$('#start').addEventListener('submit',async e=>{
  e.preventDefault();
  const msg=$('#msg');
  const v=$('#video').value.trim();
  if(!v){
    // 空路径: 直接打开浏览弹窗引导用户点文件 (最常见的误操作是"进了目录就以为选中了")
    msg.className='err'; msg.textContent='视频路径为空: 在下方列表里点一个 🎬 视频文件名即可选中';
    openBrowse();
    return;
  }
  // 显式逐字段组装, 不依赖表单 DOM 结构
  const f=new FormData();
  f.append('video',v);
  f.append('tts_model',document.querySelector('select[name=tts_model]').value);
  f.append('voice_role',$('#voice').value.trim()||'clone');
  f.append('auk_flash',document.querySelector('input[name=auk_flash]').checked?'1':'0');
  f.append('page_v',PAGE_V);   // 服务端日志里可见: 若 POST 里没有 page_v 或值旧 → 用户标签页跑的是旧 JS
  msg.className=''; msg.textContent='启动中…';
  const r=await (await fetch('/api/start',{method:'POST',body:f})).json();
  if(r.ok){ msg.className='ok'; msg.textContent=`已启动 pid=${r.pid} → 任务 ${r.task}`;
    setTimeout(()=>location.href='/task/'+encodeURIComponent(r.task),800); }
  else{ msg.className='err'; msg.textContent=r.error||'启动失败'; }
});
let DL_DIR='';
function openBrowse(dir){ $('#browseMask').style.display='block'; loadBrowse(dir||DL_DIR||'~'); }
function closeBrowse(){ $('#browseMask').style.display='none'; }
function pickFile(p){
  $('#video').value=p; closeBrowse();
  try{ localStorage.setItem('vp_video',p); }catch(_){}
  $('#video').style.borderColor='#3fb950';
  const m=$('#msg'); m.className='ok'; m.textContent='已选择: '+p;
}
// 刷新/重开页面后恢复上次选中的路径
try{ const _sv=localStorage.getItem('vp_video'); if(_sv){ $('#video').value=_sv; $('#video').style.borderColor='#3fb950'; } }catch(_){}
async function loadBrowse(dir){
  let r;
  try{ r=await (await fetch('/api/browse?path='+encodeURIComponent(dir))).json(); }
  catch(e){ $('#browseList').innerHTML='<p style="padding:14px;color:#f85149">加载失败: '+esc(e)+'</p>'; return; }
  if(r.error){ $('#browsePath').textContent=dir; $('#browseList').innerHTML='<p style="padding:14px;color:#f85149">'+esc(r.error)+'</p>'; return; }
  $('#browsePath').textContent=r.path;
  const sz=b=>b>=1073741824?(b/1073741824).toFixed(1)+' GB':b>=1048576?(b/1048576).toFixed(0)+' MB':Math.ceil(b/1024)+' KB';
  const dt=t=>{const d=new Date(t*1000),p=n=>String(n).padStart(2,'0');return (d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());};
  let h='';
  if(r.parent) h+=`<div class="brow" onclick="loadBrowse('${esc(r.parent)}')">.. (上一级)</div>`;
  h+=r.dirs.map(d=>`<div class="brow" onclick="loadBrowse('${esc(r.path)}/${esc(d)}')"><span style="color:#58a6ff">📁</span> ${esc(d)}/</div>`).join('');
  h+=r.files.map(f=>`<div class="brow" onclick="pickFile('${esc(r.path)}/${esc(f.name)}')"><span style="color:#7ec699">🎬</span> <b>${esc(f.name)}</b> <span style="color:#5c6773;font-size:12px;white-space:nowrap">${sz(f.size)} · ${dt(f.mtime)}</span><span style="float:right;color:#3fb950;font-size:12px;white-space:nowrap">点文件名选中 →</span></div>`).join('');
  if(!r.dirs.length&&!r.files.length) h+='<div style="padding:14px 16px;color:#5c6773">（无子目录和视频文件）</div>';
  $('#browseList').innerHTML=h;
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeBrowse(); });
async function unloadModel(){
  const m='qwen3.8-27b';
  if(!confirm(`卸载模型 ${m} ?\n\n会释放 ~23GB 显存给流水线;\n卸载后 agent(共用此模型)不可用, 直到模型重新加载。`)) return;
  const btn=$('#unloadBtn'); btn.disabled=true; btn.textContent='卸载中…';
  let r;
  try{ r=await (await fetch('/api/unload?model='+encodeURIComponent(m))).json(); }
  catch(e){ r={ok:false,error:String(e)}; }
  btn.disabled=false; btn.textContent='卸载模型';
  alert(r.ok?`已卸载 ${r.model}:\n${r.response||'(无响应体)'}`:`卸载失败:\n${r.error||''}`);
  refresh();
}
refresh(); setInterval(refresh,5000);
</script>
<div id="browseMask" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:10" onclick="if(event.target===this)closeBrowse()">
  <div style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:min(700px,92vw);max-height:80vh;background:#161b22;border:1px solid #2c3542;border-radius:10px;display:flex;flex-direction:column">
    <div style="padding:10px 14px;border-bottom:1px solid #262d37;display:flex;justify-content:space-between;align-items:center;gap:10px">
      <b style="font-family:ui-monospace,monospace;font-size:13px;color:#8b96a3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" id="browsePath">…</b>
      <button class="ghost" type="button" onclick="closeBrowse()">关闭</button>
    </div>
    <div style="padding:6px 14px;border-bottom:1px solid #262d37;color:#d29922;font-size:12px">🎬 点视频文件名 = 选中(自动填入上方输入框) · 📁 点文件夹只是进入, 不算选中 · .. 返回上级</div>
    <div id="browseList" style="overflow:auto;padding:8px 0;font-size:13px"></div>
  </div>
</div>
</body>
</html>
""".replace("__VERSION__", VERSION)

HTML_TASK = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="/video-pipeline.png">
<title>pipeline 日志</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0f1216;color:#d5dbe3;font:14px/1.5 system-ui,"Noto Sans CJK SC",sans-serif;padding:16px 20px}
.top{display:flex;gap:12px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
a{color:#58a6ff;text-decoration:none}
#sum{font-size:13px;color:#8b96a3;margin-bottom:8px;font-family:ui-monospace,monospace}
pre{background:#0d1117;border:1px solid #262d37;border-radius:8px;padding:12px;height:76vh;overflow:auto;font:12px/1.6 ui-monospace,monospace;white-space:pre-wrap;word-break:break-all;margin:0}
button{background:#238636;border:0;color:#fff;border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer}
button.danger{background:#b62324}
button.ghost{background:#21262d;border:1px solid #2c3542;color:#d5dbe3}
</style>
</head>
<body>
<div class="top">
  <a href="/">← 返回</a>
  <b id="name"></b>
  <a href="__CTX_HREF__"><button class="ghost">排障上下文</button></a>
  <button class="danger" id="stopBtn" onclick="stopT()">停止任务</button>
  <button class="ghost" type="button" onclick="refresh()">⟳ 刷新</button>
</div>
<div id="sum">加载中…</div>
<pre id="log"></pre>
<script>
const TASK=__TASK_JSON__;
document.getElementById('name').textContent=TASK;
async function refresh(){
  let r;
  try{ r=await (await fetch('/api/log?task='+encodeURIComponent(TASK)+'&lines=300')).json(); }
  catch(e){ document.getElementById('sum').textContent='刷新失败: '+e; return; }
  if(r.error){ document.getElementById('sum').textContent=r.error; return; }
  const t=r.task||{};
  const st={running:'运行中',done:'已完成',failed:'失败',stopped:'已停止'}[t.status]||'—';
  const prog=t.total?` 段 ${t.cur||0}/${t.total} (完成 ${t.done})`:'';
  document.getElementById('sum').textContent=`${st}${prog} · 阶段: ${t.stage||'—'}${r.note?' · '+r.note:''}`;
  document.getElementById('stopBtn').style.display = t.status==='running'?'':'none';
  const el=document.getElementById('log');
  const nearBottom = el.scrollHeight-el.scrollTop-el.clientHeight < 60;
  el.textContent=(r.lines||[]).join('\\n')||'(空)';
  if(nearBottom) el.scrollTop=el.scrollHeight;
}
async function stopT(){
  if(!confirm('停止任务 '+TASK+' ? (SIGTERM 整个进程组)')) return;
  const r=await (await fetch('/api/stop?task='+encodeURIComponent(TASK),{method:'POST'})).json();
  alert(JSON.stringify(r)); refresh();
}
refresh(); setInterval(refresh,5000);
</script>
</body>
</html>
""".replace("__VERSION__", VERSION)

HTML_CTX = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/png" href="/video-pipeline.png">
<title>排障上下文</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0f1216;color:#d5dbe3;font:14px/1.5 system-ui,"Noto Sans CJK SC",sans-serif;padding:16px 20px}
.top{display:flex;gap:12px;align-items:center;margin-bottom:10px}
a{color:#58a6ff;text-decoration:none}
button{background:#238636;border:0;color:#fff;border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer}
pre{background:#0d1117;border:1px solid #262d37;border-radius:8px;padding:12px;overflow:auto;font:12px/1.6 ui-monospace,monospace;white-space:pre-wrap;word-break:break-all;margin:0}
</style>
</head>
<body>
<div class="top">
  <a href="__BACK_HREF__">← 返回</a>
  <b>排障上下文 — __TASK__</b>
  <button onclick="navigator.clipboard.writeText(document.getElementById('c').textContent).then(()=>{this.textContent='已复制 ✓';setTimeout(()=>this.textContent='复制全部',2000)})">复制全部</button>
</div>
<p style="color:#8b96a3;font-size:13px">把下面内容整段贴给 agent (Claude Code / OpenClaw) 即可分析。</p>
<pre id="c">__CTX__</pre>
</body>
</html>
"""

# ---------------------------------------------------------------- HTTP

def _parse_post_body(raw: bytes, content_type: str) -> dict:
    """解析 POST body。浏览器 fetch(FormData) 发的是 multipart/form-data,
    parse_qs 只能解析 urlencoded —— 之前用 parse_qs 解析 multipart 导致 video 恒为空
    (用户选了文件也报"视频路径为空")。这里两种都支持。"""
    ct = (content_type or "").lower()
    if ct.startswith("multipart/form-data"):
        from email.parser import BytesParser
        from email.policy import default as _ep
        msg = BytesParser(policy=_ep).parsebytes(
            b"Content-Type: " + (content_type or "").encode("latin-1", "replace")
            + b"\r\n\r\n" + raw)
        out = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name is None:
                continue
            p = part.get_payload(decode=True)
            out[name] = p.decode("utf-8", "replace") if isinstance(p, bytes) else (p or "")
        return out
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}


class Handler(BaseHTTPRequestHandler):
    server_version = "PipelineWeb/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 30

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if ctype.startswith("text/html"):
            # 页面代码经常更新, 禁止浏览器缓存 HTML (否则旧 JS 会一直留在用户标签页里)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{time.strftime('%F %T')}] {self.address_string()} {fmt % args}\n")

    def handle_one_request(self):
        # 慢请求告警: 超过 1s 就打一行 (抓 nvidia-smi 卡 D 状态 / 文件系统抖动这类偶发问题)
        t0 = time.monotonic()
        super().handle_one_request()
        dt = time.monotonic() - t0
        if dt > 1.0:
            print(f"[slow] {getattr(self,'command','?')} {getattr(self,'path','?')} took {dt:.1f}s", flush=True)

    @staticmethod
    def _utf8(s):
        """http.server 按 latin-1 解码请求行; 若客户端发了未编码的 UTF-8 字节则还原"""
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        if path == "/":
            self._send(200, HTML_DASH)
        elif path.startswith("/task/"):
            name = self._utf8(urllib.parse.unquote(path[len("/task/"):]))
            if not _safe_name(name):
                self._send(400, "bad task name", "text/plain"); return
            self._send(200, HTML_TASK
                       .replace("__TASK_JSON__", json.dumps(name, ensure_ascii=False))
                       .replace("__CTX_HREF__", "/ctx/" + urllib.parse.quote(name)))
        elif path.startswith("/ctx/"):
            name = self._utf8(urllib.parse.unquote(path[len("/ctx/"):]))
            if not _safe_name(name):
                self._send(400, "bad task name", "text/plain"); return
            text = troubleshoot_context(name)
            self._send(200, HTML_CTX
                       .replace("__CTX__", html.escape(text))
                       .replace("__TASK__", html.escape(name))
                       .replace("__BACK_HREF__", "/task/" + urllib.parse.quote(name)))
        elif path == "/api/state":
            tasks, running = scan_tasks()
            self._json({"tasks": tasks, "running": running,
                        "gpu": gpu_status(), "cfg": read_config_env()})
        elif path == "/api/browse":
            q = urllib.parse.parse_qs(u.query)
            p = self._utf8((q.get("path") or [""])[0])
            self._json(browse_dir(p))
        elif path == "/api/unload":
            q = urllib.parse.parse_qs(u.query)
            model = self._utf8((q.get("model") or ["qwen3.8-27b"])[0])
            if not re.fullmatch(r"[A-Za-z0-9._\-]+", model):
                self._json({"ok": False, "error": f"非法模型名: {model}"}, 400)
                return
            swap = read_config_env().get("SWAP_BASE", "http://127.0.0.1:8080")
            try:
                req = urllib.request.Request(
                    f"{swap}/api/models/unload/{model}", method="POST")
                with urllib.request.urlopen(req, timeout=180) as r:
                    body = r.read().decode("utf-8", "replace")
                    status = r.status
                self._json({"ok": True, "status": status, "model": model,
                            "response": body[:300]})
            except urllib.error.HTTPError as e:
                self._json({"ok": False, "status": e.code, "model": model,
                            "error": e.read().decode("utf-8", "replace")[:300]})
            except Exception as e:
                self._json({"ok": False, "model": model, "error": str(e)})
        elif path == "/api/log":
            q = urllib.parse.parse_qs(u.query)
            name = self._utf8((q.get("task") or [""])[0])
            try:
                n = int((q.get("lines") or [TAIL_LINES])[0])
            except ValueError:
                n = TAIL_LINES
            n = max(1, min(n, 2000))
            if not _safe_name(name):
                self._json({"error": "bad task name"}, 400); return
            logf = OUTPUT_DIR / name / "pipeline.log"
            if not logf.is_file():
                self._json({"lines": [], "note": "pipeline.log 尚未生成 (dub.sh 启动中)"}); return
            try:
                lines = logf.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as e:
                self._json({"error": str(e)}, 500); return
            self._json({"lines": lines[-n:], "task": parse_task(OUTPUT_DIR / name, find_dub_processes())})
        elif path == "/video-pipeline.png":
            icon = PIPELINE_DIR / "video-pipeline.png"
            if icon.is_file():
                self._send(200, icon.read_bytes(), "image/png")
            else:
                self._send(404, "icon not found", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if u.path == "/api/start":
            form = _parse_post_body(raw, self.headers.get("Content-Type", ""))
            video = form.get("video", "")
            tts = form.get("tts_model", "auk")
            voice = form.get("voice_role", "clone")
            flash = form.get("auk_flash", "0") in ("1", "on", "true")
            print(f"[api/start] page_v={form.get('page_v','<旧版前端/无>')!r} video={video!r} "
                  f"tts={tts!r} voice={voice!r} flash={flash} body_len={len(raw)}", flush=True)
            self._json(start_task(video, tts, voice, flash))
        elif u.path == "/api/stop":
            q = _parse_post_body(raw, self.headers.get("Content-Type", ""))
            name = q.get("task") or None
            self._json(stop_task(name))
        else:
            self._send(404, "not found", "text/plain")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"pipeline-web {VERSION} 监听 http://127.0.0.1:{PORT} (Ctrl-C 退出)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
