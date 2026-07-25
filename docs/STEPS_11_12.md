# 第十一至第十二步实现说明

## 1. 第十一步：确定性 Decision Guard

`DecisionGuard` 位于裁决 Agent 和人工审核之间。它不调用模型，也不根据多数投票判断结果，而是对已经生成的固定草稿执行可重复检查。

当前检查包括：

- 决定内容哈希与数据库记录一致；
- 草稿绑定当前 `case_run` 的裁决 Agent 输出，且核心字段没有被修改；
- 交易支付时点适用的政策版本与草稿引用一致；
- 每项关键主张都有且只有一个认定；
- 已认定事实必须引用当前案件证据；
- 每项主张认定都能追溯到正确政策版本的引用；
- 不存在阻塞性开放问题；
- 全额退款等于实付金额，部分退款位于合法范围；
- 运费承担、退款动作和决定结果一致；
- 处置动作不重复、金额不越界、幂等键格式正确；
- 序列号或调包争议必须升级人工，不能把序列号差异写成买方调包事实；
- MVP 决定始终保留 `requires_human_review=true`。

Guard 违规分为：

- `BLOCK`：不能进入审核包，编排器把案件退回 `ADJUDICATION`；
- `WARN`：允许进入人工审核，但必须在审核包中显示，例如建议退款超过政策自动建议风险阈值。

结果写入不可变的 `decision_guard_reports` 表，保存 Guard 版本、检查明细、违规、决定哈希和结果哈希。同一决定重复检查会返回原报告。

合法结果由编排器执行：

```text
UNDER_INVESTIGATION / GUARD_CHECK
→ READY_FOR_REVIEW
→ HUMAN_REVIEW
```

进入 `HUMAN_REVIEW` 前会基于决定哈希和完整 Guard 结果生成 `review_package_hash`。

## 2. 第十二步：审核包与人工审核

`HumanReviewService` 提供完整审核包，其中包含：

- 固定决定版本及内容哈希；
- Guard 报告和全部警告；
- 买卖双方及 Clerk、裁决 Agent 输出；
- 主张、证据摘要和政策引用；
- 待执行动作、金额、幂等键和当前状态；
- 已有审核记录；
- 审核包哈希完整性结果。

只有数据库中角色为 `REVIEWER` 的用户可以执行审核动作。

### 批准

审核员必须明确指定 `decision_id`，不能批准“最新版本”这种可变引用。批准前会再次检查：

- Guard 已通过；
- 决定哈希与 Guard 记录一致；
- 审核包哈希完整；
- 决定和动作仍处于待审核状态。

批准后决定和动作进入 `APPROVED`，但本步骤不会执行退款、退货或放款。

### 拒绝

拒绝必须填写原因。决定进入 `REJECTED`，草稿中的动作全部变为 `CANCELLED`。拒绝草稿不等于自动认定买方主张不成立。

### 退回调查

审核员可以指定受影响的 `claim_ids` 和调查指令。系统会：

1. 保留原决定、Guard 和审核包；
2. 将原决定标记为 `REVISION_REQUESTED`；
3. 取消原草稿动作；
4. 创建新的 `case_run`；
5. 将指定主张写入 `dirty_claim_ids`；
6. 回到 `UNDER_INVESTIGATION / PARTY_ANALYSIS`。

## 3. API

- `POST /cases/{case_id}/guard`：单独运行当前 Guard；
- `GET /cases/{case_id}/guard-results`：读取 Guard 历史；
- `GET /cases/{case_id}/review-package`：读取冻结审核包；
- `POST /cases/{case_id}/reviews/approve`：批准固定决定；
- `POST /cases/{case_id}/reviews/reject`：拒绝草稿；
- `POST /cases/{case_id}/reviews/return-to-investigation`：按指定主张退回调查。

模拟执行和执行结果回读不属于本步骤。批准后的订单和资金状态不会自动变化。
