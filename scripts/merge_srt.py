#!/usr/bin/env python3
"""合并 SRT 中连续的短块，让 TTS 生成连续自然的语音（消除逐句硬静音间隔）。

用法: merge_srt.py <file.srt> [--gap 1000] [--max-dur 15000] [--max-chars 60]

行为:
- 连续块之间 gap <= --gap 且累计时长 <= --max-dur 且累计字数 <= --max-chars 时合并
- 无损: 合并前后全部文本(去空白)必须完全一致，否则退出码 1
- 幂等: 首次运行备份原文件为 <file>.raw；之后每次从 .raw 重新合并
  （所以重复运行、或翻译重跑后运行，结果都稳定）
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

TS_RE = re.compile(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})')


def to_ms(ts: str) -> int:
    m = TS_RE.match(ts.strip())
    if not m:
        raise ValueError(f"bad timestamp: {ts}")
    h, mi, s, ms = map(int, m.groups())
    return ((h * 3600 + mi * 60 + s) * 1000) + ms


def to_srt_ts(ms: int) -> str:
    h, rem = divmod(ms, 3600000)
    mi, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{mi:02d}:{s:02d},{ms:03d}"


def parse(path: Path):
    raw = path.read_text(encoding='utf-8', errors='replace').replace('\r\n', '\n')
    blocks = []
    for chunk in re.split(r'\n{2,}', raw.strip()):
        lines = [l for l in chunk.strip().split('\n') if l.strip() != '']
        if len(lines) < 2:
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        m = re.match(r'(\S+)\s*-->\s*(\S+)', lines[0])
        if not m:
            continue
        try:
            start, end = to_ms(m.group(1)), to_ms(m.group(2))
        except ValueError:
            continue
        text = ' '.join(lines[1:]).strip()
        blocks.append({'start': start, 'end': end, 'text': text})
    return blocks


def has_cjk(s: str) -> bool:
    return bool(re.search(r'[一-鿿]', s))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('srt')
    ap.add_argument('--gap', type=int, default=1000, help='合并的最大块间间隔 ms (默认 1000)')
    ap.add_argument('--max-dur', type=int, default=15000, help='合并块的最大累计时长 ms (默认 15000)')
    ap.add_argument('--max-chars', type=int, default=0, help='合并块的最大累计字数 (默认: 中文90/英文150)')
    a = ap.parse_args()

    path = Path(a.srt)
    if not path.exists():
        sys.exit(f"文件不存在: {path}")
    raw_path = path.with_name(path.name + '.raw')

    # 幂等: 有 .raw 就从 .raw 重新合并
    if raw_path.exists():
        src = raw_path
    else:
        shutil.copy2(path, raw_path)
        src = path

    blocks = parse(src)
    if len(blocks) < 2:
        print(f"[merge] {path.name}: 仅 {len(blocks)} 块，无需合并")
        return

    cjk = has_cjk(''.join(b['text'] for b in blocks))
    joiner = '' if cjk else ' '
    max_chars = a.max_chars if a.max_chars > 0 else (90 if cjk else 150)

    merged = []
    cur = None
    for b in blocks:
        if not b['text']:
            if cur is not None:
                merged.append(cur); cur = None
            merged.append(dict(b))
            continue
        if cur is None:
            cur = dict(b)
            continue
        gap = b['start'] - cur['end']
        dur = b['end'] - cur['start']
        chars = len(re.sub(r'\s', '', cur['text'] + joiner + b['text']))
        if gap <= a.gap and dur <= a.max_dur and chars <= max_chars:
            cur['end'] = b['end']
            cur['text'] = cur['text'] + joiner + b['text']
        else:
            merged.append(cur)
            cur = dict(b)
    if cur is not None:
        merged.append(cur)

    # 无损校验
    before = re.sub(r'\s', '', ''.join(b['text'] for b in blocks))
    after = re.sub(r'\s', '', ''.join(b['text'] for b in merged))
    if before != after:
        sys.exit(f"[merge] 错误: 合并前后文本不一致 (before={len(before)} after={len(after)})，未写入")

    out = []
    for i, b in enumerate(merged, 1):
        out.append(f"{i}\n{to_srt_ts(b['start'])} --> {to_srt_ts(b['end'])}\n{b['text']}\n")
    path.write_text('\n'.join(out), encoding='utf-8')
    avg = sum(len(re.sub(r'\s', '', b['text'])) for b in merged) / max(len(merged), 1)
    print(f"[merge] {path.name}: {len(blocks)} 块 -> {len(merged)} 块 (平均 {avg:.0f} 字/块, 无损校验通过)")


if __name__ == '__main__':
    main()
