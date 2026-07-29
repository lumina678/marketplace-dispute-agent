# ISSUE-024：审核员端身份、Session 与案件级操作边界

## 背景

第二十三步已经可以把案件工作台公开部署，但 API 请求体仍允许客户端提交 `actor_id`、`reviewer_id`，前端也把演示审核员写死在 JavaScript 中。这样的字段只能作为兼容旧客户端的“声明值”，不能作为权限和审计依据；SSE 也必须和普通案件 API 使用同一套访问控制。

## 目标

只为内部审核员建立一个可上线的登录闭环：服务端确认审核员身份，保护工作台、案件 API、异步 Workflow 控制、SSE、人工审核和模拟执行；买家/卖家继续作为交易和 Claim 的业务主体，不在本阶段提供登录入口。

## 验收标准

- [x] `REVIEWER` 用户可以使用账号密码登录，买家、卖家和管理员账号不能登录审核员端。
- [x] Session 使用随机 opaque token + Redis TTL；浏览器只持有 HttpOnly、SameSite=Strict Cookie。
- [x] 写操作需要 Session 中的 CSRF Token；缺失或错误时返回 403。
- [x] 未登录案件 API、OpenAPI、Agent 输出和 SSE 返回 401；健康检查仍可公开探活。
- [x] 审批、Workflow 控制、执行、路由覆盖和工具调用忽略客户端伪造身份，并记录 Session reviewer ID。
- [x] 买卖双方的 `submitted_by/appellant_id` 保留业务主体含义；`recorded_by_id` 记录审核员代录材料的真实操作者。
- [x] 提供 Argon2 账号 CLI、登录页、退出登录和账号停用后的 Session 失效。
- [x] SQLite/PostgreSQL migration、Secure Cookie 配置、认证单测和全量回归通过。
- [x] Session Store 故障明确返回 503，浏览器登录页和工作台完成真实烟雾测试。

## 非目标

不实现买家/卖家登录、注册、找回密码、OAuth、MFA、组织/租户、多级审核员组织架构、生产 Secret Manager、限流和完整 SIEM。它们属于后续公开平台化阶段。

## 风险与开放问题

- 当前开发 Compose 使用单 Redis；生产需要 Redis 持久化、备份和故障切换。
- 密码账号由部署 CLI 管理，后续可替换为企业 IdP，但 API 仍应保持同一 `ReviewerPrincipal` 边界。
- 案件级买家/卖家可见性暂未开放；当前所有登录审核员都能查看审核范围内的模拟案件，后续再增加 team/case ACL。
