# 视频提交 / 轮询 / 下载

1. `quote` 只带模型、时长、分辨率，以及「现价去模型广场看」。不输出单价。
2. 没有 `--yes` 禁止 `submit-video`。确认话术：会扣费、现价以广场为准，问要不要提交。
3. `POST /v1/video/generations` → 记下 `id` 或 `task_id` 到 `.tudouni/jobs.jsonl`。
4. `GET /v1/video/generations/{task_id}`（失败再试 `/v1/tasks/{task_id}`）。
5. 状态：`pending|queued` → queued；`in_progress|processing` → in_progress；`completed|success` → completed；`failed|error` → failed。
6. `completed` 后立刻 `download`。URL 会过期。
7. 会话死了用 `jobs` 再 `poll`。

禁止把媒体 base64 打进对话。只回 task_id 和本地路径。问价就指向 https://tudouni-api.com 模型广场。
