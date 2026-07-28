# ISSUE-022：异步任务和实时进度

## Problem

`POST /cases/{id}/workflow` 同步调用多个模型，容易受 HTTP timeout、502、应用重启和浏览器断开影响，且用户看不到可靠阶段进度。

## Scope

- Redis + RQ 持久任务投递；
- Job/Stage/Event 数据模型和迁移；
- 202 创建契约、Job 查询和 SSE；
- 重试、心跳、超时、Worker stale recovery；
- 案件活动任务去重；
- 暂停、恢复和取消；
- 工作台实时进度及刷新恢复；
- 运维、ADR 和 API 文档。

## Out of scope

- Temporal；
- 图片和视频分析；
- 正式生产身份认证、Redis 集群和 PostgreSQL；
- 增加评测案件数量。

## Acceptance criteria

- [x] POST 立即返回 `202 + job_id`；
- [x] Redis/RQ Worker 执行 Workflow；
- [x] 模型错误后台重试且不再同步 502；
- [x] 同一案件防止重复启动；
- [x] 六个 Agent/Guard 阶段独立持久化；
- [x] SSE 支持事件 sequence 和断线续传；
- [x] 支持心跳、超时和 stale recovery；
- [x] 支持人工暂停、恢复和取消；
- [x] 工作台使用 SSE 并可刷新恢复；
- [x] 全量测试、迁移回归和最终 PR 检查通过。
