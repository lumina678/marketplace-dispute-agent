# 第二十三步：生产部署基础

## 1. 目标与边界

这一步把第二十二步的异步 Workflow 从“本机可以启动”提升到“可重复部署到预发布环境”。核心原则：

- API、RQ Worker、Redis 和 PostgreSQL 以独立进程运行；
- API/Worker 镜像均以 UID/GID `10001` 的非 root 用户运行；
- 一次性 `migrate` 服务执行 `alembic upgrade head`，成功后 API 和 Worker 才启动；
- staging/production 强制 PostgreSQL，SQLite 只保留给快速单元测试和离线演示；
- `/health` 表示进程存活，`/ready` 检查数据库、队列和活跃 Worker；
- Caddy 负责域名、HTTPS、SSE 透传和反向代理；
- development、test、staging、production 使用不同配置，不提交真实密钥。

本阶段不引入 Kubernetes、Temporal、真实支付/物流 API、认证授权或完整可观测性。

## 2. 架构

```mermaid
flowchart TD
    B[Browser] -->|HTTPS| C[Caddy reverse proxy]
    C -->|internal HTTP| A[FastAPI API]
    A --> P[(PostgreSQL)]
    A --> R[(Redis / RQ)]
    W[RQ Worker] --> R
    W --> P
    W --> M[自有大模型]
    MIG[one-shot Alembic migration] --> P
    MIG -. success gate .-> A
    MIG -. success gate .-> W
```

API 只接收请求、读取案件并把 Workflow 入队；模型调用仍由 Worker 执行，因此慢模型不会占用一个长 HTTP 请求。

## 3. 交付物

| 文件 | 用途 |
| --- | --- |
| `deploy/docker/api.Dockerfile` | FastAPI/Alembic 镜像，非 root |
| `deploy/docker/worker.Dockerfile` | RQ Worker 镜像，非 root |
| `compose.yaml` | PostgreSQL、Redis、migration、API、Worker、Caddy 基线编排 |
| `deploy/compose.*.yaml` | development/test/staging/production override |
| `deploy/caddy/Caddyfile` | 域名、自动 HTTPS、SSE flush、反向代理 |
| `deploy/env/*.env.example` | 四种环境的非秘密配置模板 |
| `e3b7a1c9d5f2_use_postgresql_timestamptz.py` | PostgreSQL 原生 `TIMESTAMP WITH TIME ZONE` |
| `scripts/migrate_sqlite_to_postgres.py` | SQLite 到空 PostgreSQL 的复制和行数校验 |
| `deployment_health.py`、`healthcheck.py` | readiness 与容器健康探针 |

## 4. 环境配置矩阵

| 环境 | 数据库 | 队列 | Worker readiness | 对外入口 |
| --- | --- | --- | --- | --- |
| development | Compose PostgreSQL；单测可用 SQLite | RQ；单测显式 inline | 默认不要求 | `127.0.0.1:8000` 或本地 Caddy |
| test | PostgreSQL | RQ | 要求 | 容器内部 API |
| staging | PostgreSQL | RQ | 强制 | staging 域名 + HTTPS |
| production | PostgreSQL | RQ | 强制 | 生产域名 + HTTPS |

`Settings` 在 staging/production 启动时拒绝 SQLite、inline queue 和非 HTTPS `XIANYU_PUBLIC_BASE_URL`，让错误配置在启动期失败。

## 5. 本机启动生产形态开发环境

根目录 `.env` 是本地 Python 配置，不要直接拿它给 Compose 使用。第一次启动：

```bash
cd /Users/ahs/Documents/xianyu
cp deploy/env/development.env.example deploy/env/development.env
docker compose \
  --env-file deploy/env/development.env \
  -f compose.yaml \
  -f deploy/compose.development.yaml \
  up -d --build
```

查看服务与日志：

```bash
docker compose --env-file deploy/env/development.env ps
docker compose --env-file deploy/env/development.env logs -f migrate api worker
```

`migrate` 成功退出是正常现象。第一次需要演示数据时：

```bash
docker compose --env-file deploy/env/development.env \
  --profile tools run --rm seed
```

检查并打开工作台：

```bash
curl http://127.0.0.1:18000/health
curl http://127.0.0.1:18000/ready
curl http://127.0.0.1:18000/worker/health
open http://127.0.0.1:18000/workbench
```

Compose 也会启动 Caddy。`https://localhost:18443` 使用本地 CA，浏览器可能提示不受信任；开发时可直接使用 18000 端口。development 使用高位端口，以免和旧版 Redis、本机 PostgreSQL 或其他项目冲突。停止服务：

```bash
docker compose --env-file deploy/env/development.env \
  -f compose.yaml -f deploy/compose.development.yaml down
```

只有确定要删除全部本地 PostgreSQL/Redis 数据时才使用 `down -v`。

## 6. SQLite 迁移到 PostgreSQL

迁移脚本要求目标是空 PostgreSQL，并在结束后逐表核对行数；不会覆盖已有数据，也不会复制 `alembic_version`。

### 6.1 备份并升级源数据库

```bash
cp data/dispute_agent.db "data/dispute_agent.db.$(date +%Y%m%d%H%M%S).bak"
XIANYU_DATABASE_URL=sqlite:///data/dispute_agent.db \
  .venv/bin/alembic upgrade head
```

### 6.2 创建 PostgreSQL schema

```bash
docker compose --env-file deploy/env/development.env \
  -f compose.yaml -f deploy/compose.development.yaml \
  up -d postgres redis migrate
```

### 6.3 dry-run 和正式复制

宿主机运行时目标地址使用映射端口：

```bash
XIANYU_DATABASE_URL='postgresql+psycopg://xianyu:xianyu-local-only@127.0.0.1:15432/xianyu' \
  .venv/bin/python scripts/migrate_sqlite_to_postgres.py \
  --source sqlite:///data/dispute_agent.db --dry-run

XIANYU_DATABASE_URL='postgresql+psycopg://xianyu:xianyu-local-only@127.0.0.1:15432/xianyu' \
  .venv/bin/python scripts/migrate_sqlite_to_postgres.py \
  --source sqlite:///data/dispute_agent.db
```

也可以使用一次性容器：

```bash
docker compose --env-file deploy/env/development.env \
  --profile tools run --rm migrate-sqlite
```

迁移时应停止旧 SQLite API/Worker，避免旧库和新库同时产生写入。推荐顺序：停止旧进程 → 备份 → schema migration → data copy → smoke test → 切换入口。

## 7. staging 预发布环境

### 7.1 服务器和 DNS

准备可运行 Docker Compose 的 Linux 主机，开放 TCP 80/443，并为域名添加 A/AAAA 记录，例如：

```text
staging.example.com A <staging-server-public-ip>
```

复制配置模板并填写真实值：

```bash
cp deploy/env/staging.env.example deploy/env/staging.env
```

真实数据库密码和模型 API Key 不得提交 Git。密码写入数据库 URL 时必须 URL encode，正式环境优先使用部署平台 Secret Store。

### 7.2 启动前检查

```bash
docker compose \
  --env-file deploy/env/staging.env \
  -f compose.yaml -f deploy/compose.staging.yaml \
  config >/tmp/xianyu-staging.compose.yaml
```

确认公网 URL 为 HTTPS、数据库 URL 为 PostgreSQL、队列为 RQ，且 PostgreSQL/Redis/API 内部端口没有映射到公网。

### 7.3 发布和验收

```bash
docker compose \
  --env-file deploy/env/staging.env \
  -f compose.yaml -f deploy/compose.staging.yaml \
  up -d --build

curl --fail https://staging.example.com/health
curl --fail https://staging.example.com/ready
curl --fail https://staging.example.com/worker/health
```

Caddy 会通过 ACME 自动申请证书。申请失败时检查 DNS、生效时间、80/443、防火墙和端口占用。staging 还要走一遍创建交易、聊天导入、冻结、异步 Workflow、SSE 恢复、人工审批和模拟执行 smoke test。

## 8. 健康检查语义

| 探针 | 成功/失败 | 含义 |
| --- | --- | --- |
| `GET /health` | 200 | FastAPI 进程存活，不代表依赖可用 |
| `GET /ready` | 200/503 | DB、Redis 可用；staging/production 还要求 Worker |
| `GET /worker/health` | 200/503 | Redis 中存在活跃 RQ Worker 和有效注册 TTL |
| `GET /workflow/health` | 200/503 | Redis/RQ 队列本身可访问 |
| `xianyu-healthcheck api` | exit 0/1 | API 容器调用 `/ready` |
| `xianyu-healthcheck worker` | exit 0/1 | Worker 容器检查 DB 与 Worker 注册 |

数据库重启时 Python 进程仍可存活，但 readiness 会失败，Caddy 不再接入新流量；Worker 可以独立重启和恢复任务。

## 9. 故障和回滚

1. 迁移失败：API/Worker 不启动，先查看 `docker compose logs migrate`，不要手工删除迁移版本。
2. Worker 不健康：查看 Worker 日志和 `/worker/health`；第二十二步的 retry、outbox 和 stale recovery 继续生效。
3. PostgreSQL 故障：不要切回旧 SQLite 继续写同一案件；恢复数据库或备份后再做 smoke test。
4. 应用回滚：使用上一个镜像 tag。只有确认兼容且没有新版本数据写入时才执行 schema downgrade。
5. HTTPS 故障：保留 Caddy/ACME 日志并修复 DNS 或网络，不应长期关闭 HTTPS 上线。

## 10. 验证命令

```bash
.venv/bin/python -m compileall -q src scripts tests
.venv/bin/pytest
.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade c8a1f7d2e9b4
.venv/bin/alembic upgrade head
.venv/bin/alembic check
docker compose -f compose.yaml -f deploy/compose.development.yaml \
  --env-file deploy/env/development.env.example config
docker compose build api worker
```

设置 `XIANYU_TEST_POSTGRES_URL` 后会额外验证 PostgreSQL 原生 `timestamptz` 与 aware `datetime` 往返。

## 11. 企业式交付记录

- Issue：`docs/engineering/issues/ISSUE-023-production-deployment-foundation.md`
- ADR：`docs/adr/0005-postgresql-and-compose-deployment.md`
- PR：`docs/engineering/pull_requests/PR-023-production-deployment-foundation.md`
- 分支：`feat/production-deployment-foundation`

若第二十二步尚未合并，PR base 先设为 `feat/async-workflow-jobs`。第二十二步合并 main 后再 rebase 本分支，并把 PR base 改为 `main`。
