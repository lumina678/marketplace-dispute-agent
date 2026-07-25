# 第五至第七步实现说明

## 1. 数据层

技术栈：SQLAlchemy 2、Alembic、SQLite。金额使用整数分，时间通过 `AwareDateTime` 统一以带时区的 UTC ISO 8601 文本保存。

### 业务表

- `users`
- `transactions`
- `listing_snapshots`
- `disputes`
- `claims`
- `messages`
- `evidence`
- `shipment_events`
- `policy_versions`
- `decisions`
- `appeals`
- `resolution_actions`

### 运行治理表

- `case_runs`：每次调查或补证恢复创建独立运行，保存预算、阶段和 checkpoint。
- `tool_calls`：记录工具、调用方、参数摘要、结果摘要、耗时和错误。
- `approvals`：审批绑定不可变的决定 ID 和内容哈希。
- `case_events`：追加式状态事件，支持乐观锁和重放。
- `open_questions`：保存补问目标、关联主张、缺失事实、证据类型和截止时间。

`alembic/versions/467f4d8206db_initial_dispute_schema.py` 是当前基线迁移。迁移支持从空库升级、降级和再次升级，`alembic check` 不应检测到模型漂移。

### 五个种子案件

| 案件 ID | 场景 |
| --- | --- |
| `case_clear_mismatch` | 商品承诺 16GB，同序列号第三方报告显示 8GB |
| `case_buyer_missing_evidence` | 买方只有主张，没有实际配置检测材料 |
| `case_seller_preshipment` | 买卖双方材料冲突，卖方有发货前记录 |
| `case_serial_conflict` | 发布页和检测报告序列号不同，且有调包指控 |
| `case_appeal_reversal` | 原决定驳回，新第三方报告触发申诉 |

种子导入是幂等的：重复运行 `xianyu-seed` 不会重复创建案件或政策版本。

## 2. 工具层

所有工具先实现为普通 Python `ToolService`，FastAPI 和 MCP 只是传输适配器。

| MCP 名称 | 类型 | 说明 |
| --- | --- | --- |
| `transaction.get` | 只读 | 查询交易、实付金额、订单和模拟资金状态 |
| `listing.get_snapshot` | 只读 | 查询锁定的商品快照及内容哈希 |
| `conversation.search` | 只读 | 在所属交易的聊天快照中检索 |
| `shipment.get_timeline` | 只读 | 查询有来源哈希的物流时间线 |
| `evidence.list` | 只读 | 列出单个案件的证据元数据 |
| `evidence.inspect` | 只读 | 查看证据来源、提取事实和完整性；明确标记内容不可信 |
| `policy.search` | 只读 | 只检索案件固定版本，不隐式使用最新版 |
| `policy.get_version` | 只读 | 获取指定不可变政策版本 |
| `case.get_state` | 只读 | 查询案件、活动运行、预算和开放问题 |
| `case.add_open_question` | 受控写 | 创建去重、关联主张且不超过三轮的补问 |
| `resolution.create_draft` | 受控写 | 创建带版本和内容哈希的处置草稿及动作草稿 |
| `resolution.execute_mock` | 高风险写 | 仅执行已审批、哈希一致、幂等键匹配的模拟动作 |

每次调用都会写入 `tool_calls`。失败调用同样保留审计记录；业务写入失败时先回滚业务事务，再单独提交失败审计。

## 3. 确定性编排器

状态只能由 `StateMachineService` 按 `config/case_state_machine.json` 修改。Agent 不能直接写 `disputes.state`。

### 启动

`CaseOrchestrator.start(case_id)` 会：

1. 校验交易、主张和锁定快照。
2. 按交易 `paid_at` 唯一选择并固定政策版本。
3. 创建新的 `case_run`。
4. 通过 `T01` 和 `T03` 进入 `UNDER_INVESTIGATION`。
5. 完成确定性的 `INTAKE`、`SNAPSHOT` 和 `CLAIM_EXTRACTION`。
6. 暂停在 `PARTY_ANALYSIS`，等待后续双方 Agent 提交结构化结果。

再次调用 `start` 不会创建重复活动运行，而是返回已有 checkpoint。

### 阶段协议

```text
PARTY_ANALYSIS
→ EVIDENCE_REVIEW
→ GAP_RESOLUTION
→ ADJUDICATION
→ GUARD_CHECK
→ HUMAN_REVIEW
```

每次 `submit_phase_result` 都校验：

- 案件和活动 `case_run` 是否一致。
- 提交阶段是否等于 checkpoint 的待处理阶段。
- Token 和工具调用预算是否越界。
- 阶段结果是否包含必要引用，例如裁决阶段必须引用当前 run 创建的 `decision_id`。

Guard 通过后，编排器先固化审核包哈希，再依次进入 `READY_FOR_REVIEW` 和 `HUMAN_REVIEW`。Guard 连续失败或预算耗尽会确定性转人工。

### 补证暂停与恢复

证据 Clerk 先通过 `case.add_open_question` 创建问题，再在 `GAP_RESOLUTION` 提交问题 ID。编排器根据目标进入 `WAITING_FOR_BUYER` 或 `WAITING_FOR_SELLER`。

收到新证据后，`resume_with_evidence` 会：

1. 校验证据属于当前案件且关联开放问题。
2. 将问题标记为已回答。
3. 完成旧 `case_run`，不覆盖其 checkpoint。
4. 创建新的 `case_run`。
5. 记录新增证据和受影响的 `dirty_claim_ids`。
6. 返回 `PARTY_ANALYSIS`，仅让后续 Agent 重评受影响内容。

### 恢复与重放

- `recover(case_id)` 从数据库读取活动 checkpoint；若进程在 `RUNNING` 时退出，只将其安全恢复为 `PAUSED`，不重复执行阶段。
- `replay(case_id)` 从 `SUBMITTED@1` 顺序应用 `case_events`，检查事件序号、起始状态和版本增量，结果必须与案件当前状态一致。
- 所有状态写入使用 `state_version` 乐观锁，并发修改时拒绝覆盖。

## 4. FastAPI 接口

- `GET /health`
- `GET /cases`
- `GET /cases/{case_id}`
- `GET /cases/{case_id}/events`
- `POST /cases/{case_id}/runs`
- `POST /cases/{case_id}/runs/{case_run_id}/phases/{phase}`
- `POST /cases/{case_id}/resume-evidence`
- `POST /cases/{case_id}/recover`
- `GET /cases/{case_id}/replay`
- `POST /tools/{tool_name}`

当前 API 是本地开发接口，没有实现生产身份认证。工具层仍会检查调用角色，但在真实部署前必须由可信认证层生成角色，不能接受客户端任意声明。

## 5. 本阶段边界

第五至第七步不包含模型 Prompt、双方分析 Agent、证据审查 Agent、裁决 Agent 或完整 Decision Guard。编排器只定义并执行这些组件必须遵循的阶段、预算、状态和数据契约。测试使用结构化占位结果验证流程，不把占位结果作为真实裁决。
