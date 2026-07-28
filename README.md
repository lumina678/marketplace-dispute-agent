# 二手交易争议调解与裁决辅助 Agent

本项目是一个面向二手交易争议的多 Agent 调查与裁决辅助系统，通过双方观点分离、证据链构建、版本化规则检索、人工审批和幂等执行，处理普通客服流程无法解决的复杂案件。

> 当前仓库完成了实施计划的前二十一步：基础规格、SQLite 数据层、本地 MCP 工具层、确定性案件编排器、多 Agent 调查、补证恢复、裁决草稿、确定性 Decision Guard、人工审核、模拟执行与回读、申诉重开和二次调查、可重复评测 Harness、案件工作台、Claim 级争议分类、版本化 Skill 框架、确定性 Router、Skill 驱动完整编排和通用模拟案件接入。系统不替代法院、仲裁机构或平台人工裁决员。

## 当前 MVP

- 场景：二手笔记本电脑的描述不符、缺件、空包和运输损坏文本争议；假货和未知类型固定转人工。
- 语言与币种：简体中文、人民币。
- 决策方式：Agent 调查与建议 + 确定性校验 + 人工审批。
- 执行方式：仅连接模拟账户和本地交易/物流数据，不接入真实支付或物流系统。

## 已交付能力

| 实施步骤 | 交付物 | 作用 |
| --- | --- | --- |
| 1. MVP 边界 | [`docs/MVP_SPEC.md`](docs/MVP_SPEC.md) | 固定目标、输入输出、权限边界、非目标和验收条件 |
| 2. 状态机 | [`docs/CASE_STATE_MACHINE.md`](docs/CASE_STATE_MACHINE.md)、[`config/case_state_machine.json`](config/case_state_machine.json) | 定义案件生命周期、流转条件、角色和恢复语义 |
| 3. 统一 Schema | [`schemas/`](schemas/) | 约束案件材料、Agent 输出、状态机和规则版本 |
| 4. 规则库 | [`policies/`](policies/) | 提供两个互不重叠的“描述不符”规则版本及确定性选版方法 |
| 5. 数据层 | [`src/dispute_agent/models.py`](src/dispute_agent/models.py)、[`alembic/`](alembic/) | 18 张 SQLite 表、Alembic 迁移和 5 个可重复导入的种子案件 |
| 6. 工具层 | [`src/dispute_agent/services/tools.py`](src/dispute_agent/services/tools.py)、[`src/dispute_agent/mcp_server.py`](src/dispute_agent/mcp_server.py) | 12 个带权限、预算、审计和案件隔离的本地工具 |
| 7. 编排器 | [`src/dispute_agent/services/orchestrator.py`](src/dispute_agent/services/orchestrator.py) | 状态机驱动、checkpoint、暂停恢复、预算升级和事件重放 |
| 8. 调查 Agent | [`src/dispute_agent/agents/runtime.py`](src/dispute_agent/agents/runtime.py) | 买卖双方独立分析、证据审查、时间线和版本化政策引用 |
| 9. 补证循环 | [`src/dispute_agent/agents/question_planner.py`](src/dispute_agent/agents/question_planner.py)、[`src/dispute_agent/services/evidence_submission.py`](src/dispute_agent/services/evidence_submission.py) | 语义去重、单方单轮补问、暂停恢复和受影响主张重评 |
| 10. 裁决建议 | [`src/dispute_agent/agents/workflow.py`](src/dispute_agent/agents/workflow.py) | 生成证据与规则可追溯、必须人工审核的处置草稿，并停在 Guard 前 |
| 11. Decision Guard | [`src/dispute_agent/services/decision_guard.py`](src/dispute_agent/services/decision_guard.py) | 确定性检查主张覆盖、证据引用、政策版本、金额、动作和权限边界 |
| 12. 人工审核 | [`src/dispute_agent/services/human_review.py`](src/dispute_agent/services/human_review.py) | 固化审核包，允许审核员批准、拒绝或按主张退回新一轮调查 |
| 13. 模拟执行与回读 | [`src/dispute_agent/services/execution.py`](src/dispute_agent/services/execution.py)、[`src/dispute_agent/services/tools.py`](src/dispute_agent/services/tools.py) | 只执行哈希一致的已批准动作，支持模拟退款、退货、放款、失败恢复、幂等重放和结果核验 |
| 14. 申诉与二次调查 | [`src/dispute_agent/services/appeals.py`](src/dispute_agent/services/appeals.py)、[`docs/STEPS_13_14.md`](docs/STEPS_13_14.md) | 固定政策期限，校验申诉人和新证据，接受后创建新运行和新决定版本并保留历史 |
| 15. 评测 Harness | [`src/dispute_agent/services/evaluation.py`](src/dispute_agent/services/evaluation.py)、[`evaluation/labels.json`](evaluation/labels.json) | 用人工标签只读评估结论、证据、规则、补问、公平性、越权和执行成本 |
| 16. 案件工作台 | [`src/dispute_agent/services/workbench.py`](src/dispute_agent/services/workbench.py)、[`web/workbench.html`](web/workbench.html) | 聚合主张、时间线、证据关系图、规则、Agent 轨迹、审核、执行和申诉，提供可演示的案件视图 |
| 17. 争议分类体系 | [`src/dispute_agent/dispute_types.py`](src/dispute_agent/dispute_types.py)、[`alembic/versions/2e4c6a8b0d12_add_claim_routing_and_skill_binding.py`](alembic/versions/2e4c6a8b0d12_add_claim_routing_and_skill_binding.py) | 为每条 Claim 持久化争议类型、路由来源、理由、置信度、状态和版本化 Skill 绑定，并锁入 Case Run checkpoint |
| 18. Skill 框架 | [`src/dispute_agent/skills/`](src/dispute_agent/skills/)、[`docs/STEPS_17_18.md`](docs/STEPS_17_18.md) | 提供不可变 Skill 合同、注册表和描述不符、缺件、空包、运输损坏四套文本调查手册，并注入 Agent 上下文 |
| 19. 确定性 Router | [`src/dispute_agent/services/claim_router.py`](src/dispute_agent/services/claim_router.py)、[`config/dispute_router.json`](config/dispute_router.json)、[`docs/STEPS_19.md`](docs/STEPS_19.md) | 按用户声明、平台原因码和版本化文本规则路由 Claim，保存追加式审计历史，模型候选、歧义、假货和未知类型固定转人工 |
| 20. Skill 驱动完整编排 | [`src/dispute_agent/agents/runtime.py`](src/dispute_agent/agents/runtime.py)、[`src/dispute_agent/agents/heuristics.py`](src/dispute_agent/agents/heuristics.py)、[`src/dispute_agent/services/decision_guard.py`](src/dispute_agent/services/decision_guard.py)、[`docs/STEPS_20.md`](docs/STEPS_20.md) | 补齐缺件、空包、运输损坏政策；由绑定 Skill 裁剪上下文和工具；生成类型专属补问；Guard Profile 按争议类型执行独立边界 |
| 21. 通用模拟案件接入 | [`src/dispute_agent/services/intake.py`](src/dispute_agent/services/intake.py)、[`src/dispute_agent/intake_schemas.py`](src/dispute_agent/intake_schemas.py)、[`docs/INTAKE_API.md`](docs/INTAKE_API.md)、[`docs/STEPS_21.md`](docs/STEPS_21.md) | 通过幂等 API 创建交易、导入商品和聊天、提交争议/Claim、上传文字证据，并以版本检查和 Manifest Hash 冻结可重放案件基线 |
| 22. 异步任务和实时进度 | [`src/dispute_agent/services/workflow_jobs.py`](src/dispute_agent/services/workflow_jobs.py)、[`src/dispute_agent/workflow_queue.py`](src/dispute_agent/workflow_queue.py)、[`src/dispute_agent/worker.py`](src/dispute_agent/worker.py)、[`docs/STEPS_22.md`](docs/STEPS_22.md) | `POST /workflow` 立即返回持久化 Job；Redis/RQ Worker 执行并重试；SSE 展示 Agent 阶段；支持心跳、超时、去重、暂停、取消和重启恢复 |

第五至第七步的详细约定见 [`docs/STEPS_5_7.md`](docs/STEPS_5_7.md)，第八至第十步见 [`docs/STEPS_8_10.md`](docs/STEPS_8_10.md)，第十一至第十二步见 [`docs/STEPS_11_12.md`](docs/STEPS_11_12.md)，第十三至第十四步见 [`docs/STEPS_13_14.md`](docs/STEPS_13_14.md)，第十五至第十六步见 [`docs/STEPS_15_16.md`](docs/STEPS_15_16.md)，第十七至第十八步见 [`docs/STEPS_17_18.md`](docs/STEPS_17_18.md)，第十九步见 [`docs/STEPS_19.md`](docs/STEPS_19.md)，第二十步见 [`docs/STEPS_20.md`](docs/STEPS_20.md)，第二十一步见 [`docs/STEPS_21.md`](docs/STEPS_21.md)，第二十二步见 [`docs/STEPS_22.md`](docs/STEPS_22.md)。默认使用无需 API Key 的 `rule-based-baseline-v1`，自有模型接入与故障语义见 [`docs/MODEL_INTEGRATION.md`](docs/MODEL_INTEGRATION.md)。

## 本地运行

项目要求 Python 3.11 或更高版本。首次启动：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/xianyu-seed
```

启动 Redis（需要 Docker Desktop 已运行）：

```bash
docker compose up -d redis
```

分别启动 FastAPI 和 RQ Worker；二者必须读取同一份 `.env`：

```bash
.venv/bin/uvicorn dispute_agent.api:app --reload
.venv/bin/xianyu-worker
```

API 文档位于 `http://127.0.0.1:8000/docs`。

除种子案件外，现在也可以完全通过 API 创建新案件：创建交易、导入商品和聊天、提交 Claim、上传文字证据、冻结材料后启动 Workflow。完整请求示例和幂等/错误语义见 [`docs/INTAKE_API.md`](docs/INTAKE_API.md)。

## 接入自有大模型

是的，OpenAI-compatible 服务通常只需提供这三项：

```env
XIANYU_MODEL_BASE_URL=http://127.0.0.1:11434/v1
XIANYU_MODEL_API_KEY=
XIANYU_MODEL_NAME=qwen3
```

复制并编辑配置后启动：

```bash
cp .env.example .env
.venv/bin/uvicorn dispute_agent.api:app --reload
```

系统会自动选择自有模型 Backend，不需要再改 Python 代码。买卖双方 Agent 并行生成独立结构化分析，证据与规则 Agent 随后审查，裁决 Agent 综合材料给出建议；它们不进行简单投票。所有输出继续经过 Pydantic Schema、内容哈希、Decision Guard 和人工审核。

检查当前配置和连接状态：

```bash
curl http://127.0.0.1:8000/model
curl 'http://127.0.0.1:8000/model/health?probe=true'
```

模型传输失败或 Schema 修复耗尽时，HTTP 请求仍已用 `202` 接受；后台 Job 按配置重试，最终把 `MODEL_BACKEND_ERROR` 持久化到 Job，不会静默回退到规则基线。工作台会展示当前 Backend、实时阶段、重试状态，以及每个 Agent 的模型名、Token usage 和完整结构化输出。完整兼容格式、可选配置和非 OpenAI-compatible 适配示例见 [`docs/MODEL_INTEGRATION.md`](docs/MODEL_INTEGRATION.md)。

主演示案件 `case_clear_mismatch` 当前采用纯文本闭环：交易前商品描述、双方锁定聊天、物流签收事件和平台设备信息文本导出，不依赖图片或视频。工作台启动调查后通过 SSE 接收持久化进度，依次呈现双方 Agent、证据规则 Agent、裁决 Agent、Decision Guard、人工审核与模拟执行；刷新页面后会恢复当前 Job。点击页面底部“重置文本演示”可以清除该案件之前的运行记录并重复演示完整流程。

让调查自动推进到“等待补证”或 `HUMAN_REVIEW`：

```bash
curl -X POST http://127.0.0.1:8000/cases/case_clear_mismatch/workflow
# 从响应取得 job_id 后查询状态或订阅 SSE：
curl http://127.0.0.1:8000/workflow-jobs/<job_id>
curl -N http://127.0.0.1:8000/workflow-jobs/<job_id>/events
```

读取冻结审核包并批准固定决定版本：

```bash
curl http://127.0.0.1:8000/cases/case_clear_mismatch/review-package
curl -X POST http://127.0.0.1:8000/cases/case_clear_mismatch/reviews/approve \
  -H 'Content-Type: application/json' \
  -d '{"decision_id":"<decision_id>","reviewer_id":"user_reviewer_demo","reason":"已复核证据、规则和金额"}'
```

执行批准后的模拟处置并读取结果：

```bash
curl -X POST http://127.0.0.1:8000/cases/case_clear_mismatch/execution \
  -H 'Content-Type: application/json' \
  -d '{"decision_id":"<decision_id>"}'
curl http://127.0.0.1:8000/cases/case_clear_mismatch/execution
```

在申诉窗口内提交新证据并由审核员决定是否重开：

```bash
curl -X POST http://127.0.0.1:8000/cases/case_clear_mismatch/appeals \
  -H 'Content-Type: application/json' \
  -d '{"appellant_id":"user_buyer_demo","appellant_role":"BUYER","grounds":"NEW_EVIDENCE","statement":"提交第三方原始检测报告","new_evidence":[{"evidence_type":"DEVICE_REPORT","description":"第三方原始检测报告显示 8GB 和涉案序列号","source_record_id":"appeal-report-001","captured_at":"2026-07-23T10:00:00+08:00","related_claim_ids":["claim_clear_mismatch_buyer"],"extracted_facts":[{"field":"detected_memory_gb","value":8},{"field":"detected_serial","value":"SN-CLEAR-001"}]}]}'
curl http://127.0.0.1:8000/cases/case_clear_mismatch/appeals
```

运行评测并打开案件工作台：

```bash
.venv/bin/python scripts/evaluate_cases.py --output artifacts/evaluation.json
.venv/bin/python scripts/export_workbench.py case_clear_mismatch --output artifacts/case_clear_mismatch.workbench.json
open http://127.0.0.1:8000/workbench
```

启动 stdio MCP Server：

```bash
.venv/bin/xianyu-mcp
```

数据库默认写入 `data/dispute_agent.db`，可用 `XIANYU_DATABASE_URL` 覆盖。

## 快速校验

```bash
.venv/bin/python scripts/validate_foundation.py
.venv/bin/pytest
.venv/bin/alembic check
```

基础校验器会检查 JSON、状态引用、政策区间、规则引用和示例案件；开发依赖中的 `jsonschema` 会额外执行 Draft 2020-12 校验。测试覆盖数据库、工具权限、审计、双方独立分析、政策引用、补问去重、补证恢复、部分重评、Guard 拦截、审核包完整性、人工批准/拒绝/退回、模拟执行、幂等重试、结果回读、申诉期限、申诉身份校验、申诉重开、决定版本链、评测指标、越权处置检查、工作台投影、API 和 MCP 注册。

## 目录

```text
.
├── config/                  # 可被编排器直接读取的状态机
├── alembic/                 # 数据库迁移
├── docs/                    # 产品边界与流程说明
├── examples/                # 可验证的示例案件
├── evaluation/              # 人工标签与评测数据集
├── policies/                # 版本化平台规则
├── schemas/                 # JSON Schema Draft 2020-12
├── scripts/                 # 基础设计校验脚本
├── src/dispute_agent/       # API、数据模型、工具与编排器
│   └── skills/              # 版本化争议调查 Skill 与注册表
├── web/                     # 无外部依赖的案件工作台页面
└── tests/                   # 自动化测试
```
