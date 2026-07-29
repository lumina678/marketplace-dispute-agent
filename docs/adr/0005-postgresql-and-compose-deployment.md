# ADR-0005：以 PostgreSQL + Compose 作为生产部署基础

- 状态：Accepted
- 日期：2026-07-29
- 关联步骤：第二十三步

## 背景

第二十二步已经把 Workflow 变成 Redis/RQ 驱动的持久化异步任务，但 API 与 Worker 仍默认使用 SQLite，只有单个 Redis Compose 服务，也没有 migration gate、Worker readiness 或 HTTPS 入口。多个进程同时写 SQLite 会放大锁竞争和恢复风险。

## 决策

1. staging 和 production 强制使用 PostgreSQL；SQLite 只保留给单元测试和离线演示。
2. Compose 统一编排 PostgreSQL、Redis、一次性 Alembic migration、API、Worker 和 Caddy。
3. migration 成功是 API/Worker 启动的前置条件。
4. API、Worker 镜像使用 UID/GID `10001` 的非 root 用户。
5. `/health` 与 `/ready` 分离；production readiness 还要求至少一个有效 RQ Worker 注册。
6. Caddy 负责域名、ACME HTTPS、SSE flush 和反向代理。
7. SQLite 到 PostgreSQL 使用“备份 → 空目标 schema → dry-run → 复制 → 行数核对”的一次性脚本。

## 备选方案

- 继续 SQLite：最简单，但多进程写锁、备份和水平扩展语义不适合公开使用。
- 直接上 Kubernetes/Temporal：能力更强，但当前个人 MVP 的运维成本过高。
- 只支持某个云 PaaS：上线方便但形成厂商绑定；Compose 更适合作为可移植参考实现。

## 后果

并发写入、迁移、Worker 重启和 HTTPS 有了清晰边界；代价是需要维护 PostgreSQL/Redis 数据卷、DNS、ACME 和密钥。正式开放前仍要补认证、限流、监控、备份与故障演练。
