# ADR-0006：审核员端使用服务端 Redis Session 与 CSRF

- 状态：Accepted
- 日期：2026-07-29
- 关联步骤：第二十四步

## 背景

工作台只有审核员这一类内部使用者，但系统已经包含人工审批、暂停/恢复、模拟资金动作和 SSE。把 `reviewer_id` 放进请求体或 localStorage 会导致身份冒充、长连接绕过和不可追责的审计记录。

## 决策

1. 只允许 `User.role == REVIEWER` 且账号激活的用户登录审核员端。
2. 使用随机 opaque session token；Cookie 为 HttpOnly、SameSite=Strict，staging/production 强制 Secure。
3. Session 内容和 CSRF token 保存在 Redis，使用 SHA-256(token) 作为 Redis key，Cookie 原值不作为 Redis key；Session 具有绝对 TTL，注销立即删除。
4. 所有非公开 API 和 SSE 由 FastAPI middleware 验证 Session；健康检查、登录接口和 HTML 外壳是唯一公开路径。
5. 所有写请求要求 `X-CSRF-Token` 与 Session 中 token 常量时间比较；原生 EventSource 只读，不需要额外 Header。
6. API 兼容保留 `actor_id/reviewer_id` 字段，但认证开启时统一解析为 Session reviewer ID；它们不参与真实授权。
7. 业务主体和实际操作者分离：买卖双方仍写入 `submitted_by/appellant_id`，审核员代录时写入 `recorded_by_id`；工具调用用 `actor` 表示内部权限角色、`actor_id` 表示真实审核员。
8. 测试环境可以显式设置 `XIANYU_AUTH_ENABLED=false` 保留旧领域单测；公开部署配置不能关闭认证。
9. 浏览器响应统一增加 frame、MIME、referrer 和 HSTS（HTTPS 环境）基础安全头；登录页与工作台不把 Session 或 CSRF 写入 localStorage。

## 备选方案

- JWT + localStorage：浏览器脚本可读 Token，撤销和 SSE 身份变更更复杂，拒绝。
- JWT + HttpOnly Cookie：可行，但短期仍需要撤销表/黑名单；当前项目已有 Redis，服务端 Session 更直接。
- OAuth/OIDC：适合真实公司，但会引入 IdP、回调和环境配置，不符合目前个人 MVP 的边界。

## 后果

服务端需要 Redis 可用性和 Session TTL 监控，API 每次请求会确认账号仍为激活 REVIEWER；安全边界、注销、CSRF 和审计责任清晰。后续接企业 IdP 时可只替换登录凭证验证，不改变案件服务层的真实操作者接口。
