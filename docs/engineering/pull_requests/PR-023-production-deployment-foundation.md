# PR-023：生产部署基础

## 变更摘要

将部署形态从“本机 API + Redis”提升为 PostgreSQL、Redis/RQ、migration gate、非 root API/Worker 镜像和 Caddy HTTPS 入口，并提供 development/test/staging/production 配置模板。

## 设计决策

- PostgreSQL 在 staging/production 由 `Settings` 强制校验；SQLite 仍服务单元测试。
- 时间字段在 SQLite 保持 ISO 文本，在 PostgreSQL 使用原生 `TIMESTAMP WITH TIME ZONE`。
- `/health` 不依赖外部服务；`/ready` 是流量切入条件；Worker 使用 RQ registry TTL 进行独立探针。
- migration 是一次性 Compose 服务，以 `service_completed_successfully` 作为启动门槛。
- Caddy 承担 ACME HTTPS 和 SSE 透传，不把 API/数据库/Redis 暴露到生产公网。

## 验证记录

```text
compileall: PASS
pytest: PASS (96 passed, 1 PostgreSQL opt-in test skipped by default)
alembic upgrade/downgrade/upgrade: PASS
alembic check: PASS
compose config: PASS
Docker API/Worker build and non-root check: PASS
PostgreSQL integration and SQLite data copy: PASS
Caddy local HTTPS and SSE proxy: PASS
containerized six-stage Workflow smoke test: PASS
```

## 发布与回滚

先在依赖第二十二步的 stacked branch review；第二十二步合并 main 后 rebase 本分支。staging 通过 health、Workflow、SSE、审批和模拟执行 smoke test 后再打 release tag。迁移失败时 API/Worker 不启动；应用回滚优先回滚镜像，不自动 downgrade 数据库。
