# 第二十二步：异步任务和实时进度

## 目标

把模型调用从 FastAPI 请求生命周期中移出：

```text
Browser -> POST /cases/{id}/workflow -> 202 + job_id
                                      |
                                      v
                                 Redis / RQ
                                      |
                                      v
                                 RQ Worker
                                      |
                                      v
                         SQLite Job / Stage / Event
                                      |
                                      v
                              SSE -> Browser
```

FastAPI 不再等待多个 Agent 完成，因此模型慢、请求代理超时或浏览器断开不会产生原来的同步 `502`。Redis/RQ 负责投递，SQLite 仍是业务状态、重试结果和审计事件的权威来源。

## 技术选择

选择 RQ，而不是 Celery、Dramatiq 或 Temporal：

- 当前只有一个 Python Worker 队列，RQ 的运维面和学习成本更匹配；
- Redis 持久队列、延迟重试、失败注册表和 Worker 机制已经满足当前需求；
- 业务检查点已经保存在 SQLite，暂时不需要 Temporal 的独立工作流服务和完整 Replay 模型；
- `WorkflowQueue` 是边界接口，未来达到多队列、复杂调度或跨服务 Saga 规模时可以替换。

详细决策见 `docs/adr/0004-rq-durable-workflow-jobs.md`。

## 持久化模型

迁移 `c8a1f7d2e9b4` 新增：

- `workflow_jobs`：案件、RQ delivery、状态、尝试次数、心跳、超时、控制请求和最终结果；
- `workflow_stages`：每个 Agent/确定性阶段的独立状态和结果摘要；
- `workflow_job_events`：严格递增 sequence 的 SSE/审计事件。

活动 Job 使用唯一 `active_dedupe_key=case_id`，从数据库层阻止同一案件并发启动。进入终态后释放该键，允许后续补证或申诉创建新的 Job。

## HTTP 与 SSE 契约

创建任务：

```bash
curl -i -X POST http://127.0.0.1:8000/cases/case_clear_mismatch/workflow \
  -H 'Content-Type: application/json' \
  -d '{"actor_id":"workbench-reviewer"}'
```

响应为 `202 Accepted`，主体包含 `job_id`、`status`、`events_url`、尝试次数和阶段列表。

```bash
curl http://127.0.0.1:8000/workflow-jobs/<job_id>
curl http://127.0.0.1:8000/cases/case_clear_mismatch/workflow-jobs/latest
curl -N http://127.0.0.1:8000/workflow-jobs/<job_id>/events
```

SSE 支持 `Last-Event-ID` 和 `?after=<sequence>` 断线续传，并定期发送注释心跳。事件数据来自数据库，因此 FastAPI 重启不会丢失已经提交的进度。

控制任务：

```bash
curl -X POST http://127.0.0.1:8000/workflow-jobs/<job_id>/pause \
  -H 'Content-Type: application/json' \
  -d '{"actor_id":"reviewer_001","reason":"人工核验新材料"}'

curl -X POST http://127.0.0.1:8000/workflow-jobs/<job_id>/resume \
  -H 'Content-Type: application/json' \
  -d '{"actor_id":"reviewer_001","reason":"核验完成"}'

curl -X POST http://127.0.0.1:8000/workflow-jobs/<job_id>/cancel \
  -H 'Content-Type: application/json' \
  -d '{"actor_id":"reviewer_001","reason":"案件改由人工处理"}'
```

暂停和取消是协作式控制：排队任务可以直接停止；运行中的模型请求完成后，Worker 会在下一安全阶段边界生效。这样不会在 Agent 输出已写入但业务 phase 提交一半时强杀事务。

## Agent 阶段

每个 Job 独立记录：

- `BUYER_CASE_ANALYST`；
- `SELLER_CASE_ANALYST`；
- `EVIDENCE_POLICY_CLERK`；
- `EVIDENCE_GAP_PLANNER`；
- `ADJUDICATION_AGENT`；
- `DECISION_GUARD`。

买卖双方仍并行运行，但各自拥有独立 Stage 行。Worker 重试时，已有 Agent 输出通过原有输入指纹幂等复用，phase 提交也从案件检查点继续。

## 重试、心跳和恢复

- RQ delivery 默认最多执行 3 次，退避为 5 秒、30 秒；
- 单次 Job 默认超时 600 秒，模型本身仍有更短的 HTTP timeout；
- 独立线程默认每 5 秒更新 Job 和当前 Stage 心跳；
- Worker 启动时扫描超过 30 秒没有心跳的运行任务；
- `PAUSE_REQUESTED` 和 `CANCEL_REQUESTED` 会恢复到对应控制终点；
- 可重试的 stale Job 创建新的 delivery version，避免复用 Redis 中的遗留 delivery ID；
- Redis 投递失败返回 `503 QUEUE_UNAVAILABLE`，但数据库中的 Job 作为 outbox 保留；再次调用相同案件或启动 Worker 都会幂等重投同一个 Job。

## 本地运行

```bash
cp .env.example .env
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/alembic upgrade head
docker compose up -d redis
```

开两个终端：

```bash
.venv/bin/uvicorn dispute_agent.api:app --reload
```

```bash
.venv/bin/xianyu-worker
```

检查：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/workflow/health
docker compose ps
```

生产部署应使用受控 Redis、TLS/认证、独立 Worker 副本和监控告警；`inline` queue 只用于自动化测试，不能配置到生产环境。当前项目尚未实现统一登录鉴权，因此公开部署前还必须为 Job 查询、SSE 和控制接口增加案件级授权。

## 完成标准

- POST 契约为 `202 + job_id`；
- 同一案件只有一个活动 Job；
- 每个 Agent 阶段可审计；
- SSE 支持断线续传；
- 模型失败不再变成同步 HTTP 502；
- 重试、超时、心跳、stale recovery 可测试；
- 支持排队和运行中暂停、恢复与取消；
- 工作台刷新后可恢复当前 Job；
- downgrade/upgrade 和全量测试通过。
