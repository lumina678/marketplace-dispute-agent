# 通用模拟案件接入 API

## 调用顺序

```text
创建交易
→ 导入商品基线
→ 批量导入聊天
→ 提交争议和 Claim
→ 上传初始文字证据
→ 冻结案件材料
→ 启动 Workflow
```

所有时间必须携带时区。示例使用种子用户：

- `user_buyer_demo`
- `user_seller_demo`
- `user_admin_demo`

第二十四步起，所有接入 API 都要求审核员 Session，写请求还要求 CSRF。先按 `README.md` 登录并设置 `BASE`、`COOKIE`、`CSRF`；下列请求中的买家/卖家 ID 是被代录的业务主体，真实操作者始终来自 Session。旧 `actor_id` 字段仅为兼容保留，认证开启时会被忽略。

### 1. 创建交易

```bash
curl -b "$COOKIE" -X POST "$BASE/transactions" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{
    "transaction_id":"txn_api_001",
    "buyer_id":"user_buyer_demo",
    "seller_id":"user_seller_demo",
    "listing_id":"listing_api_001",
    "paid_amount_minor":280000,
    "paid_at":"2026-07-10T09:30:00+08:00",
    "delivered_at":"2026-07-13T15:30:00+08:00",
    "order_status":"DISPUTED"
  }'
```

### 2. 导入商品快照

```bash
curl -b "$COOKIE" -X POST "$BASE/transactions/txn_api_001/listing-snapshots" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{
    "snapshot_id":"snapshot_api_001",
    "payload":{"title":"二手笔记本","memory_gb":16,"device_serial":"SN-API-001"},
    "captured_at":"2026-07-13T16:00:00+08:00"
  }'
```

### 3. 批量导入聊天

```bash
curl -b "$COOKIE" -X POST "$BASE/transactions/txn_api_001/messages:batch" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{
    "messages":[{
      "message_id":"message_api_001",
      "sender_role":"SELLER",
      "body":"确认是 16GB 内存，功能正常。",
      "sent_at":"2026-07-10T08:30:00+08:00"
    }]
  }'
```

### 4. 提交争议和 Claim

```bash
curl -b "$COOKIE" -X POST "$BASE/disputes" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{
    "case_id":"case_api_001",
    "transaction_id":"txn_api_001",
    "dispute_type":"DESCRIPTION_MISMATCH",
    "submitted_by_id":"user_buyer_demo",
    "claims":[{
      "claim_id":"claim_api_001",
      "party":"BUYER",
      "claim_type":"CONFIG_MISMATCH",
      "statement":"卖方承诺 16GB，买方检测为 8GB。",
      "asserted_at":"2026-07-13T18:00:00+08:00"
    }]
  }'
```

### 5. 上传文字证据

```bash
curl -b "$COOKIE" -X POST "$BASE/cases/case_api_001/evidence" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{
    "evidence_id":"ev_api_001",
    "submitted_by":"BUYER",
    "submitter_id":"user_buyer_demo",
    "evidence_type":"DEVICE_REPORT",
    "description":"设备信息文字导出",
    "text_content":"序列号 SN-API-001，系统检测内存 8GB。",
    "source_record_id":"buyer_report_api_001",
    "captured_at":"2026-07-13T17:00:00+08:00",
    "related_claim_ids":["claim_api_001"],
    "extracted_facts":[
      {"field":"detected_memory_gb","value":8},
      {"field":"detected_serial","value":"SN-API-001"}
    ]
  }'
```

`text_content` 是不可信数据，不会作为系统指令执行。`extracted_facts` 是当前文本 MVP 的结构化输入；未来多模态或抽取服务也应通过同一证据契约写入。

### 6. 冻结材料

先读取案件的 `state_version`，再进行乐观并发冻结：

```bash
curl -b "$COOKIE" -X POST "$BASE/cases/case_api_001/freeze" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"expected_state_version":1}'
```

冻结成功后状态为 `EVIDENCE_LOCKED`。再次发送相同请求会返回同一 Manifest Hash 和 `idempotent_replay=true`。

读取接入材料和完整性状态：

```bash
curl -b "$COOKIE" "$BASE/cases/case_api_001/intake"
```

启动调查：

```bash
curl -b "$COOKIE" -X POST "$BASE/cases/case_api_001/workflow" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' -d '{}'
```

该接口返回 `202 Accepted` 和 `job_id`，不会等待模型完成。使用
`GET /workflow-jobs/{job_id}` 查询持久化状态，或使用
`GET /workflow-jobs/{job_id}/events` 订阅 SSE 进度。

## 幂等与错误语义

- 相同业务 ID + 相同规范化内容：`200`，响应中 `created=false` 或 `reused_count>0`；
- 相同业务 ID + 不同内容：`409`；
- 未登录：`401`；缺失/错误 CSRF 或角色无权执行：`403`；
- 哈希、关联 ID、时区或冻结前置条件错误：`400`；
- `expected_state_version` 冲突：`409`；
- 冻结后修改基线：`409`，应改用补问或申诉接口。
