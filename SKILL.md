---
name: tudouni-lens
description: Use when the user wants 土豆泥图视, tudouni, tudouni-api.com, image, video, or audio generation, gpt-image, nano-banana, Seedance, MiniMax-H3, H3video-2k, sd2, 1k/2k/4k, saved PNG/MP4, /tudouni-lens. Never use for text/chat/LLM models and never call /v1/chat/completions. Do not use built-in image_gen when the user did not name tudouni or 土豆泥图视.
argument-hint: 出图或出视频
---

# 土豆泥图视

Claude Code：`/tudouni-lens`。Codex：`$tudouni-lens`。只出图、视频、以后的音频。

铁律：禁止文字模型和 `/v1/chat/completions`。禁止手写 curl。只连接 tudouni-api.com，拒绝其它中转 Base URL。货盘实时拉这把 Key。换 Key 写钥匙盒，不用关软件。

对客户：短、好懂。**价只在本会话第一次打开时提一次。** 客户已经在出图、出视频、改图时，禁止再提广场、现价、扣费、多少钱——打断消费。

## 每次打开（还没点具体画面）

先跑 `python scripts/tudouni.py doctor`。把 `speak` 说给人听：货盘 + **呼出菜单** + **广场看价（仅此一次）**。型号报 ID 原文，不要概括成「几条」。没上的品类（例如音频为空）一句都不要提。

呼出菜单必须出现一次：

> 下次直接说「出一张…」或「出一条视频…」，或再打 `/tudouni-lens`（Codex 用 `$tudouni-lens`）。想看货盘说「我能出什么」。

没有具体画面时，问出图还是出视频。已经点了要出什么：跳过菜单和价，直接干。

没 Key / 401：只说怎么写钥匙盒，价仍只提广场一次，然后闭嘴。

## 出图 / 出视频（消费中）

只回结果路径、`mapped`（若改了档）、下一句能怎么改（比例、2K、再来一张）。**不要**再提模型广场、现价、登录网站。视频提交前只需一句「确认就出」，不要借机讲价。

客户主动问多少钱，才答：去 tudouni-api.com 模型广场。只答这一句。

## 出图技术

`generate --prompt ... --tier 1k|2k|4k`。客户说「image-2.5 2k」→ `--model gpt-image-2.5 --tier 2k`。默认 1K。

## 出视频技术

确认后 `submit-video --yes`。

## 不用

写代码、聊天、文字模型、没点名土豆泥时的官方 `image_gen`。

## 落盘

`output/tudouni/` · 钥匙盒 `%USERPROFILE%\.tudouni\api_key`
