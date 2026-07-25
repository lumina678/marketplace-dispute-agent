# 第十七、十八步：争议分类与 Skill 框架

## 目标

这两步把原先只有案件级 `DESCRIPTION_MISMATCH` 的 MVP，扩展为 Claim 级争议分类和版本化调查 Skill。Skill 是受控调查手册，不是自由 Prompt，也不会直接执行退款或修改案件状态。

第十九步的确定性 Router 尚不属于本次范围。本次先建立 Router 可以安全写入和读取的分类、版本与约束合同。

## 第十七步：Claim 级争议分类

稳定的一级争议类型定义在 `src/dispute_agent/dispute_types.py`：

- `DESCRIPTION_MISMATCH`
- `MISSING_PARTS`
- `EMPTY_PACKAGE`
- `SHIPPING_DAMAGE`
- `COUNTERFEIT`
- `OTHER`

每条 Claim 新增：

- `issue_type`：一级争议类型；
- `issue_subtype`：可选的细分类；
- `routing_source`：用户声明、平台原因码、确定性规则、模型候选、审核员覆盖或历史迁移；
- `routing_reason`：可审核的路由理由；
- `routing_confidence`：0–1 或空；
- `routing_status`：`UNROUTED`、`ROUTED`、`NEEDS_HUMAN`、`OVERRIDDEN`；
- `skill_name` 和 `skill_version`：必须成对为空或成对存在。

数据库约束禁止未知枚举、无效置信度和不完整的 Skill 绑定。Alembic 迁移会把历史 Claim 安全回填为：

```text
DESCRIPTION_MISMATCH
description-mismatch@1.0.0
LEGACY_MIGRATION
ROUTED
```

Case Run 的 `CLAIM_EXTRACTION` checkpoint 会保存完整 `claim_routes`，因此后续 Prompt、Skill 或 Router 升级不会改变历史运行的路由事实。

## 第十八步：版本化 Skill 框架

`SkillManifest` 是不可变 Pydantic 合同，包含：

- 支持的争议类型和 Claim 类型；
- 必要上下文；
- 工具白名单；
- 证据要求；
- 政策范围；
- 调查步骤；
- 补问规则；
- 允许的处置结果；
- Guard Profile 和强制升级条件。

注册表使用 `name@version` 唯一寻址，拒绝重复注册和不完整绑定，并能按争议类型列出候选 Skill。

当前内置：

| Skill | 争议类型 | 典型调查重点 |
| --- | --- | --- |
| `description-mismatch@1.0.0` | `DESCRIPTION_MISMATCH` | 交易前承诺、收到状态、设备身份、调包抗辩 |
| `missing-parts@1.0.0` | `MISSING_PARTS` | 约定内容、签收内容、发货前打包记录 |
| `empty-package@1.0.0` | `EMPTY_PACKAGE` | 重量链、物流异常、打包和首次开包记录 |
| `shipping-damage@1.0.0` | `SHIPPING_DAMAGE` | 发货前状态、包装、物流异常、签收和报损时间 |

`COUNTERFEIT` 和 `OTHER` 已进入分类体系，但当前没有自动调查 Skill，应由未来 Router 标记为人工处理或后续扩展。

## Agent 接入

Agent Runtime 会从持久化 Claim 读取 Skill 绑定，验证 Claim 类型与 Skill 是否兼容，并把以下字段加入每个角色的结构化输入：

- `claim_skill_bindings`
- `bound_skills`

系统 Prompt 明确要求 Agent 只能在绑定 Skill 的主张类型、证据要求、工具权限、政策范围、补问规则和允许结果内工作。

当前描述不符主流程保持兼容。其他三个 Skill 已完成调查合同和注册，但要在真实案件中自动启用，还需要第十九步 Router、对应政策版本以及第二十步 Skill 驱动编排。

## 只读 API

```text
GET /dispute-types
GET /skills
GET /skills?dispute_type=EMPTY_PACKAGE
GET /skills/description-mismatch?version=1.0.0
```

工作台会显示每条主张绑定的 `skill_name@skill_version`。

## 校验

```bash
.venv/bin/alembic upgrade head
.venv/bin/python scripts/validate_foundation.py
.venv/bin/pytest
.venv/bin/alembic check
```

