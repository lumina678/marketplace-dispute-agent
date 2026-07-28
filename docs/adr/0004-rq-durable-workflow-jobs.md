# ADR-0004：使用 RQ 投递、数据库持久化 Workflow Job

- 状态：Accepted
- 日期：2026-07-28

## 背景

同步 `/workflow` 会在一个 HTTP 请求里完成多次模型调用。上游代理、模型超时、浏览器断开或 FastAPI 重启都会使用户看到 502，同时无法可靠判断哪些阶段已经完成。

## 决策

1. FastAPI 只创建数据库 Job 并投递 RQ，返回 `202`；
2. Redis/RQ 仅作为 delivery plane，不能作为案件状态真相来源；
3. SQLite 持久化 Job、Agent Stage 和递增事件；
4. Workflow 通过 lifecycle hooks 汇报阶段，但不依赖 RQ；
5. 控制采用阶段边界上的协作式暂停/取消；
6. 活动案件使用唯一键去重；
7. Worker 用心跳识别 stale execution，并以新 delivery version 幂等恢复；
8. 浏览器使用 SSE，而不是业务状态高频轮询。

## 结果

优点：

- HTTP 生命周期与模型延迟解耦；
- 刷新、断线和应用重启后进度仍存在；
- 每个 Agent 阶段、重试和人工控制可审计；
- 可在当前单体规模下保持较小运维面。

代价：

- 本地运行新增 Redis 和独立 Worker；
- SQLite 并发写能力有限，不适合作为大规模多 Worker 的长期生产数据库；
- 暂停无法安全地即时中断正在进行的外部模型请求；
- 数据库 outbox 当前通过幂等重投实现，未来高吞吐场景应增加独立 dispatcher。

## 未选择方案

- Celery：功能完整，但当前 broker/result backend、配置和运维面偏大；
- Dramatiq：同样可用，但 RQ 与当前简单队列模型更直接；
- Temporal：适合长周期跨服务工作流，但当前已有持久化 Harness，直接引入会过度设计；
- FastAPI BackgroundTasks：不具备跨进程持久队列、可靠重试和 Worker 崩溃恢复。

## 演进条件

当出现 PostgreSQL、多业务队列、定时工作流、跨服务补偿、数百个并发长任务或严格 workflow replay 要求时，重新评估 Celery/Dramatiq/Temporal。
