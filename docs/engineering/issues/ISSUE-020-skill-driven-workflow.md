# ISSUE-020：Skill 驱动完整编排

## 背景

第十九步已经合并到 `main`，Router 会把每个 material Claim 绑定到不可变的 `skill_name@version`。但运行时仍把四类争议当作同一种描述不符案件：它们读取相同上下文、产生相同补问，Guard Profile 也只停留在 Manifest 元数据中。这样会让“Skill”成为标签而不是可执行的调查合同。

## 目标

- 发布缺件、空包、运输损坏三个版本化政策族；
- 让每个 Skill 声明的上下文成为实际读取边界；
- 让每个 Skill 的工具白名单控制运行时和编排写步骤；
- 让规则基线和模型输入都能获得 Skill 专属证据要求和补问规则；
- 让 Decision Guard 根据绑定 Skill 的 Profile 执行不同的确定性检查；
- 保持文本证据闭环，不扩充正式评测数据集，不引入图像/视频模型；
- 保留人工审核、暂停恢复、政策版本锁定、幂等和申诉重开语义。

## 验收标准

- [ ] 三个政策文件可由 `paid_at` 唯一选版，并通过 Schema、索引和基础校验；
- [ ] `empty-package` 和 `shipping-damage` 不调用 `listing.get_snapshot`；
- [ ] Skill 运行上下文记录 required context、实际资源、工具白名单和实际调用；
- [ ] 未授权工具调用在运行时或编排写步骤被拒绝；
- [ ] 缺件、空包、运输损坏分别生成内容、重量/开包、签收/报损专属补问；
- [ ] 缺件部分退款金额必须来自证据，不能由 Agent 自行估价；
- [ ] 空包全额退款要求重量、打包、首次开包和运单关联，且不创建无实物退货动作；
- [ ] 运输损坏不能仅凭物流异常自动归责卖方；
- [ ] Guard 记录 Skill Profile、命中条件和对应规则 ID；
- [ ] 高价值缺件、重量链冲突、延迟报损分别强制转人工；
- [ ] 描述不符现有闭环和模型 Backend 回归通过；
- [ ] 不新增正式 `evaluation/labels.json` 案件；新增测试 Fixture 覆盖三类流程；
- [ ] `validate_foundation.py`、全量测试、`alembic check` 和 `git diff --check` 通过。

## 非目标

- 不做图片、视频或 OCR/视觉模型；
- 不实现多 Skill 投票或复合 Claim 自动合并；
- 不接真实支付、物流或平台数据；
- 不允许自动最终裁决，所有结果仍需人工审核。

## 依赖与交付流程

第十九步已合并到 `main`（`a5a93e6`）。第二十步使用普通功能分支 `feat/skill-driven-workflow`，通过一个 PR 合并回 `main`，不是 stacked PR。实现顺序是政策契约、Runtime、规则基线、Guard、测试和文档。
