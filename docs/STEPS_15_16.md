# 第十五至第十六步实现说明

## 1. 第十五步：可重复评测 Harness

`EvaluationService` 对已经落库的案件材料做只读评测，不改变案件状态、决定或资金。人工标签位于 [`evaluation/labels.json`](../evaluation/labels.json)，每个案件可以声明：

- 期望处置结果、期望获支持的一方和适用政策版本；
- 关键证据 ID；
- 是否应当补问、是否必须人工审核；
- 是否作为决定准确率样本计分。

评测报告包括：

- 处置建议正确率；
- 政策版本/引用准确率；
- 关键证据召回率；
- 无证据既定事实比例；
- 补问准确率；
- 人工审核要求准确率；
- 不合理买方/卖方偏向比例；
- 越权自动处置率；
- 重复退款次数；
- 申诉后纠错率；
- 工具调用数、失败调用数、耗时、Token 用量和运行数。

运行评测：

```bash
.venv/bin/python scripts/evaluate_cases.py
.venv/bin/python scripts/evaluate_cases.py --case-id case_clear_mismatch --output artifacts/evaluation.json
```

也可以读取 API：

```bash
curl http://127.0.0.1:8000/evaluation
curl 'http://127.0.0.1:8000/evaluation?case_id=case_clear_mismatch'
```

报告明确区分“没有决定、无法评分”和“已经评分但错误”，避免把等待补证误报成错误裁决。增加到约 50 个案件时，只需扩充标签文件和种子/导入数据，不需要修改评测逻辑。

## 2. 第十六步：案件工作台与演示包装

`WorkbenchService` 生成审核员视角的只读投影，聚合：

- 左侧：买卖双方主张、证据元数据和开放补问；
- 中间：交易、聊天、物流、证据提交和状态事件组成的统一时间线；
- 右侧：固定政策规则、裁决建议、Agent 调查轨迹、审核、执行和申诉；
- 证据关系图：证据—主张—规则的节点和边，可直接用于前端可视化。

接口和静态页面：

- `GET /cases/{case_id}/workbench`：返回完整工作台 JSON。
- `GET /workbench`：打开无外部依赖的本地工作台页面。
- `scripts/export_workbench.py`：导出可保存、可回放的工作台快照。

```bash
.venv/bin/python scripts/export_workbench.py case_clear_mismatch --output artifacts/case_clear_mismatch.workbench.json
open http://127.0.0.1:8000/workbench
```

工作台不提供直接执行退款的按钮；审核和执行仍通过已有 API，并继续受到 Guard、角色和幂等约束。这样演示第一屏是案件调查和证据链，而不是普通聊天窗口。

## 3. 本阶段边界

第十五、十六步不引入真实支付、真实物流、外部 BI 或第三方前端框架。评测读取的是本地持久化事实，工作台是只读投影；任何真实环境部署仍需接入可信身份认证、权限审计和数据脱敏。
