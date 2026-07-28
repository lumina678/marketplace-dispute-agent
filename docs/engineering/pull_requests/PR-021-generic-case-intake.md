# PR-021：通用模拟案件接入

## 依赖关系

当前分支 `feat/generic-case-intake` 基于尚未合并的 `feat/skill-driven-workflow`，因此 PR 初始 base 应设为 `feat/skill-driven-workflow`。PR-020 合并后再 rebase，并把 base 改为 `main`。

## 主要变化

- 新增七个通用接入和读取 API；
- 新增严格 Pydantic Intake Schema；
- 新增幂等、权限和哈希校验的 `CaseIntakeService`；
- 冻结时执行 Router、政策选版、SYSTEM Evidence 生成、消息锁定和状态转换；
- 新增 `Evidence.content_text`、案件 Manifest JSON/Hash 和冻结时间；
- 新增 Alembic 迁移 `b4d6e8f0a213`；
- Agent 工具和工作台可读取文字证据原文；
- 冻结后的初始写接口关闭，已有补问/申诉接口保持不变。

## 安全审查

- 文字证据原文明确标记为不可信数据；
- 普通交易参与方不能替另一方提交 Claim 或证据；
- 客户端哈希必须与服务端规范化哈希一致；
- 批量聊天发生冲突时不会部分写入；
- 路由未确认或多 Skill 案件不能冻结；
- 冻结状态使用 optimistic concurrency；
- 相同 ID 不允许覆盖已保存内容。

## 数据库变化

- `disputes.intake_manifest_json`；
- `disputes.intake_manifest_sha256`；
- `disputes.materials_frozen_at`；
- `evidence.content_text`。

所有字段均可空，保证已有种子案件和历史数据库平滑升级。downgrade 会删除这些字段，执行前应导出接入 Manifest 和文字证据原文。

## 验证结果

- `.venv/bin/python -m compileall -q src tests`：通过；
- `.venv/bin/python scripts/validate_foundation.py`：通过（16 个 JSON、15 个状态、32 条转换、5 个政策版本、5 条 Router 规则、6 个 Schema、9 个实例）；
- `.venv/bin/pytest -q`：85 项测试通过；
- Alembic 从 `b4d6e8f0a213` downgrade 到 `7a9c1e3f5b24`，再 upgrade 到 `b4d6e8f0a213`：通过；
- `.venv/bin/alembic check`：通过，无待生成迁移；
- `git diff --check`：通过。
