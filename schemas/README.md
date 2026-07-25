# Schema 使用说明

本目录使用 JSON Schema Draft 2020-12。

## 文件职责

- `case-file.schema.json`：案件存档的统一格式，同时在 `$defs` 中定义核心对象。
- `agent-output.schema.json`：四类 Agent 输出信封；Agent 不允许输出状态变更或直接执行动作。
- `state-machine.schema.json`：状态机配置格式。
- `policy-version.schema.json`：单个版本化政策文件格式。
- `policy-index.schema.json`：政策版本索引格式。

## 核心约束

1. 金额使用 `{ "currency": "CNY", "amount_minor": 320000 }`，禁止浮点金额。
2. `established_facts` 中每个事实必须至少引用一个 `evidence_id`。
3. 每项 `claim_finding` 必须引用至少一个具体政策条款。
4. 政策引用键固定为 `policy_id@version#rule_id`。
5. 所有分析材料包含 `case_id` 和 `case_run_id`，以支持暂停、恢复和重放。
6. 决定使用不可变的整数 `version`；申诉后新决定通过 `supersedes_decision_id` 连接旧决定。
7. MVP 中所有 `DecisionDraft.requires_human_review` 必须为 `true`。

## 对象引用示例

```text
Claim claim_memory_mismatch
  ├─ Evidence ev_listing_snapshot
  ├─ Evidence ev_device_report
  └─ PolicyCitation citation_remedy
       └─ marketplace.description_mismatch@2.0.0#DM-REMEDY-01
```

JSON Schema 能检查字段结构；跨数组的 ID 是否真实存在、政策版本是否适用于交易时间等语义约束由 `scripts/validate_foundation.py` 和后续 Decision Guard 检查。
