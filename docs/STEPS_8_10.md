# 第八至第十步实现说明

## 1. 第八步：双方分析与证据政策审查

`AgentRuntime` 运行三个调查角色：

- `BUYER_CASE_ANALYST` 与 `SELLER_CASE_ANALYST` 在独立线程中读取同一案件快照，分别输出逐项主张分析、支持/反对证据、证据缺口和建议补问。
- `EVIDENCE_POLICY_CLERK` 评估证据真实性、相关性、时间适配和证明力，构建时间线，识别材料冲突，并引用交易支付时已经固定的政策版本。
- 所有角色只能通过带审计的 `ToolService` 查询案件；不能直接修改案件状态、原始证据、订单或资金。

默认结构化生成后端为离线、可重复的 `rule-based-baseline-v1`。`CallableStructuredBackend` 是模型接入边界，外部模型的 JSON 必须通过对应 Pydantic Schema 后才能保存。

每次输出写入不可变的 `agent_outputs` 表，记录角色、Schema/Prompt/模型版本、输入指纹、结构化内容、用量和内容哈希。同一个 `case_run`、角色和阶段重复调用会复用已有输出。

## 2. 第九步：补问、去重、暂停恢复与部分重评

`EvidenceGapQuestionPlanner` 合并三个角色的补问建议。一次只向一方提出问题，避免案件同时停在买方和卖方两个等待状态。

补问的语义去重键由以下字段确定：

- 目标方；
- 关联主张集合；
- 可接受证据类型集合。

同一开放问题会幂等复用。已回答或已过期的问题，只有出现此前没有使用过的新基础证据时才能重新创建。问题同时保存生成原因、基础证据、回答证据、轮次和截止时间。

工作流发现阻塞缺口后，通过确定性编排器进入 `WAITING_FOR_BUYER` 或 `WAITING_FOR_SELLER`。补证接口会：

1. 校验回答方和全部开放问题；
2. 保存不可变证据元数据与内容哈希；
3. 将问题标记为 `ANSWERED`；
4. 完成旧 `case_run`，创建新 `case_run`；
5. 把问题关联主张写入 `dirty_claim_ids`；
6. 重新运行受影响主张及其直接对立主张。

接口：

- `POST /cases/{case_id}/evidence-and-resume`：保存证据、恢复案件，并可自动继续调查；
- `POST /cases/{case_id}/resume-evidence`：关联已经存在的证据并恢复；
- `GET /cases/{case_id}/agent-outputs`：查看所有可审计 Agent 输出。

## 3. 第十步：可审核裁决草稿

`ADJUDICATION_AGENT` 读取双方分析、证据政策报告、开放问题、当前证据和上一决定版本，输出：

- 每项主张的建议认定；
- 仅包含证据引用的 `established_facts`；
- 退款金额、运费承担和模拟动作；
- 政策引用、未解决问题、不确定性；
- 面向审核员和用户的两套说明。

部分重评时，未受新证据影响的上一版本主张认定会原样保留；新证据影响范围内的主张重新计算。序列号冲突或调包指控不会被模型推断成已经证明的买方行为，而是进入人工处理建议。

`resolution.create_draft` 会校验草稿内容与裁决 Agent 输出完全一致，并通过 `agent_output_id` 建立一对一关系。同一输出重复创建草稿会返回原决定和原动作，不会产生重复退款动作。

步骤 10 组件本身的终点是 `GUARD_CHECK`。仓库现已实现后续第 11、12 步，因此总工作流会继续运行确定性 Guard，并在通过后停在 `HUMAN_REVIEW`；仍不会自动批准或执行退款。

## 4. 运行方式

```bash
.venv/bin/alembic upgrade head
.venv/bin/xianyu-seed
.venv/bin/uvicorn dispute_agent.api:app --reload
```

推进明确描述不符案件：

```bash
curl -X POST http://127.0.0.1:8000/cases/case_clear_mismatch/workflow
```

该案件应生成 `RETURN_AND_FULL_REFUND` 草稿，通过 Guard 后停在 `HUMAN_REVIEW`。`case_buyer_missing_evidence` 应停在 `WAITING_FOR_BUYER`；`case_serial_conflict` 应先等待卖方补证，恢复后输出 `ESCALATE_TO_HUMAN`，不会认定买方调包。
