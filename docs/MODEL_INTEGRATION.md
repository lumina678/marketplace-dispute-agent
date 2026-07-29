# 自有大模型接入

系统已经将 OpenAI-compatible 结构化生成后端接入 FastAPI 和四个 Agent。正常情况下只需配置：

```env
XIANYU_MODEL_BASE_URL=http://127.0.0.1:11434/v1
XIANYU_MODEL_API_KEY=
XIANYU_MODEL_NAME=qwen3
```

- `XIANYU_MODEL_BASE_URL`：兼容 API 根路径，通常以 `/v1` 结尾；也可以直接填写以 `/chat/completions` 结尾的完整路径。
- `XIANYU_MODEL_API_KEY`：Bearer Token。本地无鉴权服务可以留空。
- `XIANYU_MODEL_NAME`：发送给模型服务的 `model` 字段。

`XIANYU_MODEL_BASE_URL` 与 `XIANYU_MODEL_NAME` 同时存在时，`model_backend=auto` 自动启用自有模型；两者都不配置时使用 `rule-based-baseline-v1`；只配置其中一项会在启动时明确报错，不会以不完整配置继续运行。通常不需要额外设置 `XIANYU_MODEL_BACKEND`。

## 兼容接口

生成请求：

```text
POST {XIANYU_MODEL_BASE_URL}/chat/completions
```

健康探测：

```text
GET {XIANYU_MODEL_BASE_URL}/models
```

生成接口需要返回 OpenAI-compatible 响应：

```json
{
  "choices": [
    {
      "message": {
        "content": "{\"...\":\"符合当前 Agent JSON Schema 的对象\"}"
      }
    }
  ],
  "usage": {
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "total_tokens": 150
  }
}
```

也支持 `message.parsed`、Markdown JSON fence、`<think>...</think>` 后的 JSON，以及 JSON 前后带少量说明文字的响应。

默认使用 `XIANYU_MODEL_RESPONSE_FORMAT=auto`：优先发送 `response_format.type=json_schema`；如果服务明确返回该格式不可用，会自动尝试 `json_object`，最后尝试仅通过 Prompt 约束 JSON。格式兼容降级不会切换到规则基线，最终输出仍须通过本地 Pydantic Schema 校验。

如果希望固定 provider 格式，可分别设置：

```env
XIANYU_MODEL_RESPONSE_FORMAT=json_object
```

或：

```env
XIANYU_MODEL_RESPONSE_FORMAT=none
```

即使关闭 provider 侧 `response_format`，本地仍会使用 Pydantic Schema 校验最终输出。

## 四个 Agent 的调用方式

同一个模型会用不同角色 Prompt 依次或并行承担：

1. `BUYER_CASE_ANALYST` 与 `SELLER_CASE_ANALYST` 并行分析，分别生成结构化主张评估，不进行多数投票。
2. `EVIDENCE_POLICY_CLERK` 检查证据、时间线和当前交易时点适用的规则版本。
3. `ADJUDICATION_AGENT` 综合双方分析和证据规则报告，生成结构化处置建议。
4. 建议持久化后仍需经过确定性的 Decision Guard 和人工审核，模型不能直接退款、退货或放款。

主演示默认使用纯文本材料，包括锁定商品描述、买卖双方聊天、物流事件和设备信息文字导出。图片、视频及其多模态解析不属于当前基本流程的前置条件；只有文本材料确实缺少会改变结论的关键事实时，Agent 才能发起补问。

模型接收角色 Prompt、已由本地工具读取并组装的案件上下文，以及本次输出 JSON Schema。当前 MCP Server 暴露案件数据和模拟处置工具；模型生成层不要求模型供应商原生支持 MCP。

## 失败语义

- 传输错误、超时、HTTP 408/409/425/429 和 5xx 会按指数退避重试。
- JSON 解析或 Schema 校验失败会发起独立的结构化修复请求，要求模型重新生成完整 JSON。
- `POST /cases/{id}/workflow` 只负责创建持久化 Job，立即返回 `202`；模型调用不会占用该 HTTP 请求。
- Worker 内的模型调用和 Job 投递分别重试；重试耗尽后 Job 进入 `FAILED`，`error.code=MODEL_BACKEND_ERROR`。
- 自有模型失败时绝不调用规则构建器作为静默 fallback。
- 案件保持在当前可恢复 phase；模型服务恢复后可创建新 Job，已经持久化且输入指纹相同的 Agent 输出会复用。
- API 响应、健康检查和工作台不会返回 API Key。

可选参数及默认值：

```env
XIANYU_MODEL_TIMEOUT_SECONDS=120
XIANYU_MODEL_HEALTH_TIMEOUT_SECONDS=5
XIANYU_MODEL_TEMPERATURE=0
XIANYU_MODEL_MAX_TOKENS=8000
XIANYU_MODEL_RESPONSE_FORMAT=auto
XIANYU_MODEL_MAX_RETRIES=2
XIANYU_MODEL_RETRY_BACKOFF_SECONDS=0.5
XIANYU_MODEL_SCHEMA_REPAIR_ATTEMPTS=1
```

## 启动与验证

```bash
cp .env.example .env
# 编辑 .env 中的三项模型配置
docker compose up -d redis
.venv/bin/uvicorn dispute_agent.api:app --reload
.venv/bin/xianyu-worker
```

第二十四步默认开启审核员认证。先按 `README.md` 登录并设置 `BASE`、`COOKIE`、`CSRF`，再确认实际加载的 Backend：

```bash
curl -b "$COOKIE" "$BASE/model"
curl -b "$COOKIE" "$BASE/model/health?probe=true"
```

再运行一个完整案件：

```bash
curl -b "$COOKIE" -X POST "$BASE/cases/case_clear_mismatch/workflow" \
  -H "X-CSRF-Token: $CSRF"
curl -b "$COOKIE" "$BASE/workflow-jobs/<job_id>"
curl -b "$COOKIE" -N "$BASE/workflow-jobs/<job_id>/events"
curl -b "$COOKIE" "$BASE/cases/case_clear_mismatch/agent-outputs"
```

打开 `http://127.0.0.1:8000/workbench`，可以查看当前 Backend，以及每个 Agent 的模型名、Token usage、内容哈希和完整结构化输出。

## 进程内自定义适配

如果服务不是 OpenAI-compatible，仍可使用已有的 `CallableStructuredBackend`：

```python
from dispute_agent.agents.backend import CallableStructuredBackend
from dispute_agent.agents.runtime import AgentRuntime

def generate_json(role, system_prompt, input_data, output_schema):
    return your_client.generate_json(
        role=role,
        system_prompt=system_prompt,
        input_data=input_data,
        output_schema=output_schema,
    )

runtime = AgentRuntime(
    backend=CallableStructuredBackend(
        name="my-private-model",
        generate_json=generate_json,
    )
)
```

FastAPI 测试或自定义部署也可以把 Backend 显式传给 `create_app(..., generation_backend=backend)`。
