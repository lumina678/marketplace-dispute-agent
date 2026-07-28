# 第二十一步：通用模拟案件接入

## 交付物

- `POST /transactions`：创建幂等模拟交易；
- `POST /transactions/{id}/listing-snapshots`：导入商品争议基线；
- `POST /transactions/{id}/messages:batch`：原子批量导入聊天；
- `POST /disputes`：提交争议、Claim 并写入 Router 审计；
- `POST /cases/{id}/evidence`：上传带原文和 extracted facts 的文字证据；
- `POST /cases/{id}/freeze`：冻结材料、固定政策、保存 Manifest Hash；
- `GET /cases/{id}/intake`：读取接入材料和 Manifest 完整性；
- Alembic 迁移 `b4d6e8f0a213`；
- API 使用手册 `docs/INTAKE_API.md`。

## 企业工程边界

- Intake Service 是唯一的初始材料写边界；
- Router、PolicyService 和 StateMachine 仍是独立服务，Intake 不复制其规则；
- 每个请求都进行权限、时区、关联对象、内容哈希和状态检查；
- 聊天批次在一个数据库事务中处理；
- 冻结使用行锁意图和 `expected_state_version`，SQLite 下由乐观版本检查提供确定性冲突语义；
- 冻结后基线只读，补问和申诉是独立的追加式生命周期。

## 开发分支

当前 PR-020 尚未合并，因此：

```text
main
└── feat/skill-driven-workflow       # PR-020
    └── feat/generic-case-intake     # PR-021（当前）
```

PR-020 合并后执行：

```bash
git fetch origin
git rebase --onto origin/main 20dcdde feat/generic-case-intake
```

更稳妥的实际操作应根据 PR-020 的最终 merge commit 再确认，不要机械复制旧 commit 边界。完成后 force-with-lease 更新 PR-021，并把 base 改为 `main`。

## 验证

```bash
.venv/bin/alembic upgrade head
.venv/bin/pytest tests/test_generic_intake.py
.venv/bin/pytest
.venv/bin/alembic check
git diff --check
```
