# PR-024：审核员端身份与权限闭环

## 变更摘要

为第二十三步的生产部署基础增加审核员专用登录工作台。服务端通过 Redis Session 确定真实 reviewer，写操作使用 CSRF 保护，案件 API/SSE 默认需要登录；请求体中的身份字段仅做向后兼容，不能覆盖 Session 身份。

## 主要改动

- 新增 `src/dispute_agent/auth.py`：Redis/In-memory Session Store、Argon2 密码验证、opaque token、TTL、注销和 CSRF。
- `users` 增加 `username/password_hash/is_active/last_login_at`；案件材料增加 `recorded_by_id`，工具调用增加 `actor_id`。
- 新增 Alembic `6f1a2b3c4d5e`，兼容 SQLite batch migration 和 PostgreSQL。
- 新增 `xianyu-create-reviewer` CLI，支持交互密码、环境变量/标准输入和更新已有 REVIEWER。
- 增加 `/auth/login`、`/auth/logout`、`/auth/me` 和独立 `web/login.html`。
- FastAPI middleware 保护所有非公开 API、文档和 SSE；staging/production 强制认证和 Secure Cookie。
- 增加防 iframe、MIME sniffing、Referrer 泄漏和 HTTPS 降级的基础浏览器安全响应头。
- 工作台启动调用 `/auth/me`，自动附加 CSRF，显示真实审核员并支持退出；删除硬编码审核员身份。
- 审批、Workflow 控制、执行、路由、工具和证据/申诉代录都使用可信服务端身份。
- 更新 `CHANGELOG.md` 的 `Unreleased`；版本号与 tag 留到发布流程统一决定。

## 兼容性与迁移

旧领域单测在 fixture 中显式关闭认证；生产和 Compose 模板默认开启。已有种子用户没有硬编码密码，部署后执行 CLI 创建/更新账号。旧 `actor_id/reviewer_id` 请求仍能解析，但认证开启时会被忽略。

## 验证记录

```text
python -m compileall: PASS
pytest tests/test_reviewer_auth.py: PASS（7 passed）
pytest 全量: PASS（103 passed, 1 skipped）
alembic upgrade head: PASS（SQLite）
alembic downgrade e3b7a1c9d5f2 && upgrade head: PASS（SQLite）
alembic upgrade head/current: PASS（隔离 PostgreSQL 17，6f1a2b3c4d5e head）
compose config: PASS
Docker API/Worker build: PASS（非 root 检查沿用 PR-023）
Browser smoke test: PASS（登录页、真实 reviewer 工作台、无 console error）
```

## 发布步骤

1. 在 staging 设置真实 PostgreSQL/Redis、HTTPS、`XIANYU_AUTH_COOKIE_SECURE=true`。
2. `alembic upgrade head` 后执行 `xianyu-seed` 和 `xianyu-create-reviewer`。
3. 用真实账号登录，验证案件读取、SSE、暂停/恢复、审批、执行和注销。
4. 账号密码只通过 Secret Store、标准输入或运行时环境提供，不进入 Git。
5. 回滚优先回滚应用镜像；不自动 downgrade 已写入认证字段的数据库。
