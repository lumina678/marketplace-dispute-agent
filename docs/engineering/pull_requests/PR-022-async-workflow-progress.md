# PR-022：异步任务和实时进度

## 依赖关系

当前分支 `feat/async-workflow-jobs` 基于 `feat/generic-case-intake`。PR-021 合并前，PR-022 的 base 应设为 `feat/generic-case-intake`；合并后按 stacked PR 流程 rebase 到 `main` 并修改 base。

## 主要变化

- `/workflow` 从同步执行改为 `202 + durable job`；
- 新增 Redis/RQ Worker 和本地 Compose Redis；
- 新增 Job、Stage、Event 三张表及 Alembic 迁移；
- 新增任务重试、心跳、单次超时和 stale recovery；
- 新增案件级活动 Job 唯一键；
- 新增 Job 查询、SSE、暂停、恢复、取消和健康接口；
- Workflow lifecycle hooks 记录六个 Agent/Guard 阶段；
- 工作台从 900ms 轮询切换到 SSE，并支持刷新恢复；
- 补证自动续跑也改为异步 Job。

## 风险与回滚

- 部署现在要求 Redis 和独立 Worker；API/Worker 配置或代码版本不一致会导致任务失败；
- SQLite 仅适合当前低并发模拟环境；
- downgrade 会删除 Workflow Job、Stage 和 Event 审计记录，执行前必须导出；
- 回滚应用前应先停止 Worker，再 downgrade 到 `b4d6e8f0a213`。

## 安全审查

- Redis 不保存案件业务真相，只保存 delivery；
- API Key 不进入 Job payload、事件或错误响应；
- 模型错误不会静默回退规则结果；
- 暂停/取消只在安全边界生效；
- 生产公开部署前仍需给 Job/SSE/控制接口增加统一身份认证与案件级授权。

## 验证结果

- `.venv/bin/python -m compileall -q src tests`：通过；
- `.venv/bin/python scripts/validate_foundation.py`：通过；
- `.venv/bin/pytest -q`：92 项测试通过，其中第二十二步专项测试 7 项；
- RQ 2.10 + fakeredis 适配测试：enqueue、retry、timeout、health、cancel 通过；
- Alembic `upgrade c8a1f7d2e9b4 -> downgrade b4d6e8f0a213 -> upgrade c8a1f7d2e9b4`：通过；
- `.venv/bin/alembic check`：通过，无待生成迁移；
- `.venv/bin/pip check`：通过；
- 工作台 JavaScript `node --check`：通过；
- 浏览器读取已解决和待调查案件：通过，无控制台 error；
- `git diff --check`：通过。

本机 Docker CLI 可用，但 Docker Desktop daemon 未启动，因此没有把真实 Redis 容器联调标记为通过。合并前的手工 smoke test 应在 Docker 启动后执行 `docker compose up -d redis`，分别启动 API/Worker，再从工作台运行一个种子案件。
