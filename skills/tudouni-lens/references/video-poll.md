# 视频完成与恢复

首选 video --model ... --prompt ... --yes，一次完成提交、查询和下载。已有生成授权不固定再问确认。

提交 POST /v1/video/generations；查询 GET /v1/video/generations/{task_id}。仅查询返回404/405时尝试 /v1/tasks/{task_id}。查询失败不重新提交视频。

返回pending后按next继续同一任务；会话中断后jobs找到记录，再video --task-id。已完成视频恢复时先重新查询，取得新下载地址。

本地回执先于收费提交写入；结果未知保留记录。媒体CDN不带API Key；下载临时文件完整成功后才替换目标文件，保留证书校验。
