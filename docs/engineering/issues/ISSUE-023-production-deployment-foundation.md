# ISSUE-023：生产部署基础

## 目标

让第二十二步的异步案件 Workflow 可以在接近生产的 staging 环境运行：PostgreSQL、Redis/RQ、API/Worker 容器、自动迁移、健康检查、非 root 和 HTTPS。

## 验收标准

- [x] API 与 Worker 有独立 Dockerfile 且非 root 运行。
- [x] PostgreSQL、Redis、migration、API、Worker、Caddy 可由 Compose 编排。
- [x] staging/production 配置拒绝 SQLite、inline queue 和 HTTP 公网 URL。
- [x] `/health`、`/ready`、`/worker/health` 与容器 probe 可用。
- [x] Alembic migration 成功后 API/Worker 才启动。
- [x] SQLite 可 dry-run 并复制到空 PostgreSQL，逐表校验行数。
- [x] staging DNS/HTTPS、发布和回滚步骤有文档。
- [x] 单元测试、迁移往返和 Compose/Docker 验证通过。

## 非目标

认证授权、生产密钥服务、限流、完整日志指标、云托管数据库、Kubernetes 和真实交易系统接入留到后续迭代。
