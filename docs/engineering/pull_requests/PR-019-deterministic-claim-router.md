# PR-019：Claim 级确定性争议类型 Router

## 目的

把第十七、十八步已经存在的 Claim 分类字段和 Skill Registry 接入一个可重放、可审计的确定性 Router，并阻止未确认路由进入 Agent 调查。

## 主要变化

- 增加版本化 Router JSON 配置和 Draft 2020-12 Schema；
- 增加用户声明、平台原因码、关键词规则、模型候选的固定优先级；
- 增加歧义阈值、假货和 OTHER 的人工边界；
- 增加 `claim_routing_decisions` 追加式审计表；
- 增加输入指纹、内容哈希和幂等复用；
- 增加审核员覆盖并验证 REVIEWER/ADMIN 身份；
- 编排器启动前自动路由 `UNROUTED` Claim；
- 多 Skill、未确认和不兼容绑定禁止启动 Agent；
- 政策族从绑定 Skill 的 `policy_scope` 读取，不再由编排器写死；
- 增加路由查询、执行和人工覆盖 API；
- 工作台展示路由状态、置信度、理由和 Skill 版本。

## 数据库变化

- 新迁移：`7a9c1e3f5b24_add_claim_routing_decision_history.py`
- 新表：`claim_routing_decisions`
- 不修改既有 Claim 数据；历史绑定继续可读；
- downgrade 会删除路由历史表，不影响 Claim 当前绑定字段。

## 安全与兼容性

- 模型候选永远不能自动成为 `ROUTED`；
- 活动 Case Run 期间禁止原地修改路由；
- 现有描述不符案件继续使用 `description-mismatch@1.0.0`；
- 其他三个 Skill 可以完成分类，但正式政策和完整工作流留到第二十步；
- 当前身份仍来自请求中的 `reviewer_id`，真实认证将在后续 RBAC 阶段替换；服务层已经验证该用户必须是 REVIEWER 或 ADMIN。

## 验证结果

- `scripts/validate_foundation.py`：通过；
- Router Schema 和配置：通过；
- 全量测试：62 passed；
- 本地 SQLite：升级到 `7a9c1e3f5b24`；
- `alembic check`：No new upgrade operations detected；
- `/cases/case_clear_mismatch/routing`：200，现有案件路由就绪。

## 回滚

```bash
.venv/bin/alembic downgrade 2e4c6a8b0d12
```

回滚会删除 Router 历史表。执行前应导出 `claim_routing_decisions`，否则审计记录不可恢复。
