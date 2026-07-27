# 第十九步：确定性争议类型 Router

## 交付目标

Router 在 Agent 调查前把每条 material Claim 绑定到不可变的 `skill_name@skill_version`。它不调用模型、不修改资金，也不允许低置信度分类静默进入调查。

架构决策见 [`docs/adr/0001-deterministic-claim-routing.md`](adr/0001-deterministic-claim-routing.md)，验收标准见 [`docs/engineering/issues/ISSUE-019-deterministic-claim-router.md`](engineering/issues/ISSUE-019-deterministic-claim-router.md)。

## 路由优先级

```text
审核员覆盖（独立授权操作）
→ 用户明确声明
→ 平台结构化原因码
→ 版本化确定性文本规则
→ 模型候选（只进入人工确认）
→ OTHER / 人工分类
```

用户声明、平台原因码和文本规则不会互相投票，而是按固定优先级短路。相同输入、相同 Router 版本和相同 Skill Registry 必须产生相同结果。

## 安全边界

- `COUNTERFEIT` 和 `OTHER` 没有自动调查 Skill，固定进入 `NEEDS_HUMAN`；
- 模型候选即使置信度为 1.0，也不能直接成为 `ROUTED`；
- 两个争议类型得分差小于 `ambiguity_margin` 时必须人工确认或拆分 Claim；
- Router 只能使用配置中声明且 Registry 唯一支持的 Claim 类型；
- 活动 Case Run 期间禁止原地修改 Skill 绑定；
- 多个 material Claim 绑定不同 Skill 时视为复合案件，第二十步完成前禁止自动启动；
- 未确认路由会保留在 `SUBMITTED`，不会调用任何调查 Agent。

## 版本化配置

Router 配置位于：

```text
config/dispute_router.json
schemas/dispute-router.schema.json
```

配置包含：

- Router ID 和版本；
- 最低规则置信度；
- 歧义分差；
- 平台原因码映射；
- 一级类型默认 Claim 类型；
- 确定性关键词规则。

任何规则、阈值或映射变化都应升级 Router 版本，并通过 Pull Request 评审，不能直接覆盖历史路由记录。

## 审计与幂等

`claim_routing_decisions` 为追加式历史表，保存：

- Claim 和案件 ID；
- 决定版本；
- Router ID 和版本；
- 路由来源、状态和置信度；
- 命中信号和可审核理由；
- Skill 名称和版本；
- 输入指纹和输出内容哈希；
- 操作者和形成时间。

相同 Claim、Router 版本和输入的重复调用会复用同一决定，不会增加重复历史。

## 编排器接入

启动案件时，编排器会：

1. 自动处理 `UNROUTED` Claim；
2. 检查所有 material Claim 是否完成路由；
3. 检查是否只有一个 Skill；
4. 从 Skill 的 `policy_scope` 选择政策族；
5. 将 Claim 路由快照写入 `CLAIM_EXTRACTION` checkpoint；
6. 只有路由就绪才进入双方 Agent 分析。

当前只有描述不符政策已经发布，因此其他三个 Skill 可以完成分类和绑定，但要运行完整调查仍需第二十步增加对应政策和 Skill 驱动流程。

## API

```text
GET  /cases/{case_id}/routing
POST /cases/{case_id}/routing
POST /cases/{case_id}/claims/{claim_id}/routing
POST /cases/{case_id}/claims/{claim_id}/routing-override
```

平台原因码示例：

```json
{
  "platform_reason_code": "MISSING_ACCESSORY"
}
```

模型候选示例，结果仍为 `NEEDS_HUMAN`：

```json
{
  "model_candidate": {
    "issue_type": "MISSING_PARTS",
    "confidence": 0.96,
    "rationale": "文本可能表达配件缺失"
  }
}
```

审核员覆盖示例：

```json
{
  "reviewer_id": "user_reviewer_demo",
  "issue_type": "SHIPPING_DAMAGE",
  "claim_type": "ITEM_DAMAGED_IN_TRANSIT",
  "reason": "人工核对物流和聊天后确认核心争议为运输损坏。"
}
```

## 验证

```bash
.venv/bin/alembic upgrade head
.venv/bin/python scripts/validate_foundation.py
.venv/bin/pytest tests/test_claim_router.py
.venv/bin/pytest
.venv/bin/alembic check
```
