# 第二十步：Skill 驱动完整编排

## 交付内容

- `policies/missing_parts/1.0.0.json`：缺件政策；
- `policies/empty_package/1.0.0.json`：空包政策；
- `policies/shipping_damage/1.0.0.json`：运输损坏政策；
- `AgentRuntime` 按 Manifest 裁剪上下文并验证工具白名单；
- 规则基线按 Skill 分派分析、证据报告、补问和处置建议；
- `EvidenceGapQuestionPlanner` 只接受绑定 Skill 允许的补问写工具；
- `DecisionGuard 2.0.0` 读取 Skill Profile 并执行专属确定性检查；
- 13 个新增测试 Fixture，未扩充正式评测标签集。

## 上下文矩阵

| Skill | 读取重点 | 不读取 |
| --- | --- | --- |
| `description-mismatch` | 商品快照、聊天、设备/身份证据、物流、政策 | 无 |
| `missing-parts` | 商品快照、聊天、装箱/签收内容、物流、政策 | 无 |
| `empty-package` | 聊天、重量链、运单、打包/首次开包证据、物流、政策 | `listing.get_snapshot` |
| `shipping-damage` | 聊天、发货前状态、包装、签收损坏、报损时间、物流、政策 | `listing.get_snapshot` |

每个 Agent 输入中的 `skill_execution` 记录 required context、实际资源、延期资源、白名单和真实工具调用。缺失资源不会被假设为事实。

## 三类专属补问

- 缺件：约定包含什么、签收实际收到什么、卖方发货前装箱什么；
- 空包：揽收/运输/签收重量、卖方打包记录、买方首次开包记录；
- 运输损坏：发货前状态和包装、签收状态、首次发现和报损时间。

补问中的 `CLAIMANT`、`RESPONDENT` 会依据 Claim party 解析为买方或卖方，最终写入数据库的永远是 `BUYER` 或 `SELLER`。

## Guard Profile

- 缺件：高价值部件、内容重大冲突必须人工；部分退款金额必须由证据中的 `suggested_refund_amount_minor` 支持；
- 空包：重量链冲突、打包/开包记录争议、欺诈指控必须人工；全额退款必须有重量、打包、开包和运单链，且不得创建退货动作；
- 运输损坏：因果争议、延迟报损、承运责任判断必须人工；没有包装不足证据不能把物流损坏归责卖方；
- 所有 Skill 仍要求人工审核，禁止自动最终裁决。

## 验证

```bash
.venv/bin/python scripts/validate_foundation.py
.venv/bin/pytest tests/test_skill_driven_workflow.py
.venv/bin/pytest
.venv/bin/alembic check
git diff --check
```
