# PR-020：Skill 驱动完整编排

## 关联 Issue

`ISSUE-020`：实现缺件、空包、运输损坏政策和 Skill 驱动上下文/补问/Guard。

第十九步已合并到 `main`（`a5a93e6`），本 PR 是从最新 `main` 创建的普通功能分支，不是 stacked PR。

## 变更摘要

- 新增三套 `2025-01-01` 起生效的版本化政策，并泛化 Schema、校验器和种子加载；
- Manifest 为四类 Skill 声明不同 required context 和 tool allowlist；空包、运输损坏不读取商品快照；
- 规则基线按 Skill 产生不同的证据分析、补问和处置建议；
- Planner 校验 Skill 允许的 `case.add_open_question`；编排器校验 `resolution.create_draft`；
- Decision Guard 升级到 `2.0.0`，记录 Profile、命中升级条件并执行三类专属边界；
- 新增文本 Fixture 测试，不改变正式评测标签。

## 风险与回滚

- 运行时默认仍使用 `rule-based-baseline-v1`，自有模型继续经过相同 Schema 和 Guard；
- 任意 Skill 上下文/工具不匹配会阻止调查，不静默降级；
- 政策为新增 policy ID，不改写描述不符历史版本；
- 回滚代码后需同时回滚本 PR 的政策文件和种子加载变更；数据库无需新增迁移，旧政策记录保留。

## 验证结果

- `scripts/validate_foundation.py`：通过，5 个政策版本和 9 个 Schema 实例有效；
- `tests/test_skill_driven_workflow.py`：14 passed；
- 全量测试：76 passed；
- `alembic check`：No new upgrade operations detected；
- `git diff --check`：通过。
