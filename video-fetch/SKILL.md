---
name: video-fetch
description: 下载微博/B站/抖音/YouTube/Twitter 等链接视频到本地，标准命名(站点_ID.mp4)、ffprobe QC、幂等、末行输出路径。视频配音流水线的取片标准入口。Use when the user gives a video URL to download, or when the dubbing pipeline (video-dub) needs to fetch a video first.
---

# Video Fetch（取片）

## 何时使用
- 用户给视频链接（微博/B站/抖音/YouTube/Twitter/…）要求下载
- 配音流水线（video-dub）的输入是链接而非本地文件时，先跑本 skill

## 用法
```bash
~/workspace/money_code/videotrans/video-pipeline/scripts/fetch.sh <url 或本地路径> [-o 输出目录]
```
- **stdout 最后一行 = 最终 mp4 的本地路径**，直接拿这个路径给后续步骤
- 本地路径输入：ffprobe 验证后原样透传（不复制）
- 已下载过的文件自动跳过（幂等，可重复执行）
- 默认输出目录 `/home/loomz/视频/downloads/`，命名 `weibo_<ID>.mp4` / `bilibili_<BV号>.mp4` …

## 注意
- 个别微博/B站视频需要登录：在 `~/workspace/money_code/videotrans/video-pipeline/config.env` 配置 `WEIBO_COOKIES` / `BILI_COOKIES`（cookies.txt 路径）
- 日常随手下载也可以用 yt-dlp skill；本 skill 是配音流水线专用入口（统一命名/QC/路径输出）
