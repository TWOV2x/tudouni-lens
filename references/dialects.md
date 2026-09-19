# 模型方言

规格编译后再按模型说话。HOLD 货直接拒绝。

| 模型 | 接口 | 硬约束 |
|---|---|---|
| 图（`gpt-image-*`、`nano-banana-*`） | `POST /v1/images/generations` 或 `/edits` | 以这把 Key 的活目录为准；优先 1K |
文字模型（Claude / GPT 聊天 / Grok 等）禁止走本 skill。
| `sd2-fast` / `sd2-pro` / `sd2.5` | `POST /v1/video/generations` | 默认 `720p`；`duration` 秒 |
| `H3video-2k` | 同上 | **锁死 15 秒 2K**，禁止改时长/分辨率 |
| `MiniMax-H3` | 同上 | 按用户秒数；不要跟 H3video-2k 混 |

`seedance2-pro` / `newtoken` / Mini 系列：HOLD，CLI 拒绝。
