# 第十三至第十四步实现说明

## 1. 第十三步：审核后模拟执行与结果核验

第十三步把人工批准的决定交给一个确定性的执行服务。它不是 Agent，也不接受客户端直接指定退款金额；执行上下文必须同时满足：

- 决定是当前案件的最新版本；
- 决定状态为 `APPROVED`；
- 存在绑定相同 `decision_content_sha256` 的人工批准记录；
- 每个动作的幂等键仍等于 `case_id:action_type:decision_version`；
- 金额和动作已经通过 Guard，并且案件处于允许执行的状态。

执行器按固定顺序处理动作：创建退货、冻结资金、退款或放款。所有动作通过 `ToolService` 的 `resolution.execute_mock` 写入本地模拟账本：

- `FULL_REFUND` / `PARTIAL_REFUND` 更新交易资金状态和订单状态；
- `RELEASE_FUNDS` 向卖方模拟余额放款；
- `CREATE_RETURN` 创建退货请求；
- `FREEZE_FUNDS` 保持资金冻结。

动作成功后保存模拟外部引用和结果快照。执行结束会重新读取交易与动作状态，逐项核验结果；核验失败或工具异常会进入 `EXECUTION_FAILED`，并保留已成功动作，不会把它们重新执行。

失败恢复要求先通过幂等状态检查，再调用 `retry`。已经成功的动作只读重放，失败或未知且没有外部引用的动作才允许重试。案件进入 `RESOLVED` 后再次执行只返回原结果，不会产生新外部引用。

已结算交易的申诉新版本不能通过新的幂等键重复退款、放款或创建退货；这种纠正必须进入人工/运营处理，避免“新决定版本”绕过重复资金动作保护。

## 2. 第十四步：申诉、期限和二次调查

`AppealService` 负责从已解决案件进入申诉窗口，并保留完整的决定与证据来源：

1. 只接受交易买方或卖方本人提交的申诉。
2. 从案件支付时点固定的政策版本读取申诉期限；不会因为规则库新增版本而改变历史案件的期限。
3. `NEW_EVIDENCE` 必须提交至少一项与当前主张关联的证据；证据元数据保存哈希、来源、提取事实和不可信内容边界。
4. 同一案件同时只能有一个开放申诉；超时后案件可由窗口任务转为 `CLOSED`。
5. 审核员可以接受或驳回申诉。接受的依据必须是新证据、证据遗漏、政策错误或执行错误中的实质问题。
6. 接受后保留原决定、Guard、审核和执行事件，创建新的 `case_run`，记录 `prior_decision_id`、新增证据和 `dirty_claim_ids`，从 `PARTY_ANALYSIS` 开始二次调查。
7. 二次调查产生新决定版本，通过 `supersedes_decision_id` 与原决定形成版本链；原始执行记录不覆盖、不删除。

申诉相关状态流转为：

```text
RESOLVED
  ├─ VALID_APPEAL_RECEIVED → APPEALED
  │    ├─ APPEAL_ACCEPTED → REOPENED → REINVESTIGATION_STARTED → UNDER_INVESTIGATION
  │    └─ APPEAL_DENIED   → CLOSED
  └─ APPEAL_WINDOW_EXPIRED → CLOSED
```

申诉截止时间、原决定哈希、政策 ID/版本和审核原因都写入 `appeals` 表；案件事件追加记录支持重放和审计。

## 3. API 与 MCP

### FastAPI

- `POST /cases/{case_id}/execution`：执行指定已批准决定。
- `POST /cases/{case_id}/execution/retry`：在幂等状态检查通过后重试失败执行。
- `GET /cases/{case_id}/execution`：读取交易、动作、外部引用和回读结果。
- `POST /cases/{case_id}/appeals`：提交申诉和可选新证据。
- `GET /cases/{case_id}/appeals`：读取案件申诉历史。
- `POST /cases/{case_id}/appeals/{appeal_id}/accept`：审核员接受并重开。
- `POST /cases/{case_id}/appeals/{appeal_id}/deny`：审核员驳回并关闭案件。
- `POST /cases/{case_id}/appeal-window/close`：关闭已过期申诉窗口。

### MCP

已有命名空间工具保持不变；`resolution.execute_mock` 额外接受可选 `case_id`，用于把执行审计明确绑定到案件。MCP 与 FastAPI 都只能触发模拟账本，不能连接真实支付或物流系统。

## 4. 验收场景

- 买方获批全额退款：退货动作和退款动作各执行一次，交易回读为 `REFUND_COMPLETED/REFUNDED`，再次调用只返回幂等重放。
- 第二个动作失败：案件进入 `EXECUTION_FAILED`，重试不重复第一个已成功动作。
- 序列号冲突：批准无资金动作决定后可完成状态闭环，但交易仍保持 `DISPUTED/HELD`。
- 已解决案件在政策期限内提交新证据：案件进入 `APPEALED`，接受后创建新运行和新决定版本，旧版本与执行事件仍可查询。
- 没有实质新依据的申诉不能重开，但可以被审核员驳回并关闭。
- 过期申诉窗口不能提交新申诉，窗口任务可将案件关闭。

第十三、十四步仍不接入真实支付、真实物流或自动最终裁决；资金纠正、追偿和超出模拟账本能力的争议必须人工处理。
