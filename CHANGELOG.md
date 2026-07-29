# Changelog

本项目采用 Keep a Changelog 风格记录面向使用者的变化。功能分支先写入 `Unreleased`；版本号和 Git tag 在发布时统一确定，避免每个 PR 各自争抢版本号。

## Unreleased

### Added

- 审核员专用登录页，以及 `/auth/login`、`/auth/me`、`/auth/logout`。
- Redis 服务端 Session、Argon2 密码、HttpOnly/SameSite/生产 Secure Cookie 与 CSRF 防护。
- `xianyu-create-reviewer` 账号创建/更新 CLI。
- 真实审核员身份在审批、Workflow、证据代录、申诉代录和工具调用中的审计记录。
- 审核员身份、Cookie、CSRF、SSE、Session TTL 和伪造身份回归测试。

### Changed

- 默认保护案件 API、OpenAPI、模型状态和 SSE；只有健康检查、登录接口与 HTML 外壳公开。
- 工作台从服务端读取审核员身份，不再在浏览器中硬编码 `reviewer_id/actor_id`。
- staging/production 配置强制开启认证、HTTPS 和 Secure Cookie。

### Security

- 客户端请求体中的身份字段不再作为授权或审计来源。
- 增加 CSRF、账号停用即时校验、Session 撤销以及基础浏览器安全响应头。

### Not Included

- 买家/卖家登录、注册、找回密码、OAuth/OIDC、MFA、多租户、案件团队 ACL 和登录限流。
