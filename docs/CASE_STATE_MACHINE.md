# 案件状态机

## 1. 设计原则

- 状态由确定性编排器写入；Agent 只能建议事件或下一步，不能直接改状态。
- 所有未列出的状态转换默认禁止。
- 每次转换必须附带 `case_event_id`、操作者、原因、时间和期望的 `state_version`。
- 使用乐观锁：写入方提交 `expected_state_version`，成功后版本加一。
- 外部证据、审核和执行可暂停；恢复时创建新的 `case_run_id`，不覆盖旧运行。
- `CLOSED` 是普通终态；管理员因有效新证据重开属于受审计的例外动作。

机器可读定义见 [`../config/case_state_machine.json`](../config/case_state_machine.json)。

## 2. 状态说明

| 状态 | 所有者 | 进入条件 | 允许工作 | 离开条件 |
| --- | --- | --- | --- | --- |
| `SUBMITTED` | 系统 | 已创建争议和至少一项主张 | 校验身份、交易归属、重复案件 | 校验通过后锁定快照；撤回或重复则关闭 |
| `EVIDENCE_LOCKED` | 系统 | 商品、聊天、交易基线已不可变保存 | 生成内容哈希、选择政策版本 | 快照和政策版本唯一确定 |
| `UNDER_INVESTIGATION` | 编排器 | 有活动 `case_run` | 运行双方分析、证据审查、规则检索 | 需要补证或材料已足够 |
| `WAITING_FOR_BUYER` | 买方 | 存在发给买方的开放问题 | 接收、校验并关联新证据 | 买方答复、超时或审核员跳过 |
| `WAITING_FOR_SELLER` | 卖方 | 存在发给卖方的开放问题 | 接收、校验并关联新证据 | 卖方答复、超时或审核员跳过 |
| `READY_FOR_REVIEW` | 系统 | 有完整建议且 Guard 通过结构检查 | 固化 decision version | 将材料提交人工审核 |
| `HUMAN_REVIEW` | 审核员 | 审核包已生成 | 核验证据、修改结果、要求补证 | 批准、拒绝草稿或退回调查 |
| `APPROVED` | 系统 | 审核员批准特定 decision version | 生成只读执行计划和幂等键 | 无执行动作则直接解决；否则执行 |
| `REJECTED` | 审核员 | 审核员拒绝当前草稿 | 记录原因 | 退回调查或关闭案件 |
| `EXECUTING` | 执行器 | 已批准且存在待执行动作 | 仅执行已批准的模拟动作 | 全部成功、部分失败或永久失败 |
| `EXECUTION_FAILED` | 执行器 | 执行动作失败或结果不确定 | 查询动作状态、幂等重试、人工介入 | 恢复执行或确认完成 |
| `RESOLVED` | 系统 | 处置完成并核验；或无需资金动作 | 开放申诉窗口 | 有效申诉或窗口到期 |
| `APPEALED` | 系统 | 期限内收到合格申诉 | 分类申诉、检查是否有新证据 | 接受重开或维持并关闭 |
| `REOPENED` | 审核员/管理员 | 申诉带来新证据或发现重大错误 | 创建新 case run 和 decision version | 返回调查 |
| `CLOSED` | 系统 | 申诉期结束、撤回、重复或最终维持 | 只读审计 | 仅管理员基于新证据例外重开 |

## 3. 关键转换约束

### 调查与补证

- `UNDER_INVESTIGATION → WAITING_FOR_*`：必须至少有一个面向该方、状态为 `OPEN` 的结构化问题。
- `WAITING_FOR_* → UNDER_INVESTIGATION`：收到新证据、答复期限届满或审核员记录跳过原因。
- 同时需要双方补证时，问题分别保存；当前状态表示下一位阻塞方，编排器可在两个等待状态之间切换。
- 补问达到 3 轮或预算耗尽时，不得继续自动追问，应生成 `ESCALATE_TO_HUMAN` 建议。

### 审核

- `UNDER_INVESTIGATION → READY_FOR_REVIEW`：不存在阻塞性开放问题，建议引用有效证据和政策，Guard 没有 `BLOCK` 级违规。
- `READY_FOR_REVIEW → HUMAN_REVIEW`：必须固定 `decision_version` 和材料哈希。
- `HUMAN_REVIEW → APPROVED`：审核员明确批准某一版本；不能批准“最新版本”这种可变引用。
- `HUMAN_REVIEW → REJECTED`：必须填写拒绝原因；拒绝草稿不等于自动驳回买方主张。

### 执行

- `APPROVED → EXECUTING`：所有动作均带幂等键，金额未超过实付金额，动作与批准版本一致。
- `EXECUTING → RESOLVED`：每项动作都通过回读核验，不能只依赖调用返回值。
- 执行结果未知时进入 `EXECUTION_FAILED`，先查询幂等键状态，再决定是否重试。

### 申诉

- `RESOLVED → APPEALED`：申诉在政策规定期限内且给出理由。
- `APPEALED → REOPENED`：存在新证据、证据遗漏、规则版本错误或重大执行错误。
- 重开不删除旧决定；新决定版本必须记录 `supersedes_decision_id`。

## 4. 事件最小字段

```json
{
  "case_event_id": "evt_001",
  "case_id": "case_001",
  "event_type": "SNAPSHOTS_LOCKED",
  "from_state": "SUBMITTED",
  "to_state": "EVIDENCE_LOCKED",
  "actor_type": "SYSTEM",
  "actor_id": "orchestrator",
  "reason_code": "BASELINE_CAPTURED",
  "occurred_at": "2026-07-15T10:05:00+08:00",
  "expected_state_version": 1,
  "new_state_version": 2,
  "metadata": {}
}
```

编排器应把事件追加到不可覆盖的 `case_events`，再更新案件当前状态。重放事件必须得到相同状态，否则视为数据完整性错误。
