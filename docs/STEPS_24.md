# 第二十四步：审核员身份与权限——详细实施与操作手册

## 1. 这一步解决什么问题

上一版工作台把 `user_reviewer_demo`、`workbench-reviewer` 放在浏览器 JavaScript 或请求体里。任何能调用接口的人都可以伪造审核员 ID，SSE 也没有访问控制。第二十四步把系统定位为“内部审核员争议调查工作台”并建立最小生产安全闭环：

```text
登录账号密码
  → 服务端 Argon2 校验 REVIEWER
  → Redis 保存 opaque Session + CSRF
  → HttpOnly Cookie 自动访问工作台/API/SSE
  → 服务端忽略请求体伪造身份
  → 审批/执行/代录材料写入真实 reviewer
  → 注销、TTL 或停用账号立即阻断后续访问
```

本阶段不做买家/卖家登录。`BUYER/SELLER` 仍然是 Claim、聊天和证据中的业务主体；审核员代录时，`recorded_by_id` 才是实际操作人。

## 2. 交付物与代码边界

| 位置 | 做了什么 |
| --- | --- |
| `src/dispute_agent/auth.py` | Redis Session、测试用 In-memory Store、Argon2、TTL、注销、CSRF 和账号激活检查 |
| `src/dispute_agent/reviewer_cli.py` | `xianyu-create-reviewer` 创建/更新账号，不在迁移中写生产密码 |
| `src/dispute_agent/api.py` | middleware、登录接口、`/auth/me`、退出、可信身份解析、SSE/API 保护 |
| `src/dispute_agent/models.py` | 用户凭证、激活状态和 `recorded_by_id/actor_id` 审计字段 |
| `alembic/versions/6f1a2b3c4d5e_*.py` | SQLite/PostgreSQL migration |
| `web/login.html` | 审核员登录页 |
| `web/workbench.html` | `/auth/me` 启动检查、真实身份显示、CSRF、退出登录 |
| `tests/test_reviewer_auth.py` | 登录、角色拒绝、Cookie、CSRF、伪造身份、SSE、注销、TTL、CLI |

## 3. 模拟真实企业开发流程

本步骤不是直接在 `main` 上修改。推荐流程如下，当前仓库也按这个顺序交付：

```bash
git switch main
git pull --ff-only
git switch -c feat/reviewer-auth-workbench
```

1. 先写 `ISSUE-024`，固定“只做审核员端”、验收标准与非目标，避免开发中膨胀成完整账号中心。
2. 写 `ADR-0006`，比较 JWT、服务端 Session 和 OIDC，记录为什么复用现有 Redis。
3. 先改数据模型和 migration，再实现 Session Store、密码 CLI 和 API middleware。
4. 服务端可信身份打通后，最后修改登录页与工作台，避免前端先出现一个没有安全边界的“假登录”。
5. 先跑认证专项测试，再跑全量回归、SQLite/PostgreSQL migration 和容器构建。
6. 更新 README、操作手册与 `PR-024` 验证记录，形成一个可审查的独立提交。
7. 推送后创建 PR；CI 与 staging smoke test 通过后再 squash/merge 到 `main`。

关联的企业式工件：

- `docs/engineering/issues/ISSUE-024-reviewer-auth-workbench.md`
- `docs/adr/0006-reviewer-server-side-sessions.md`
- `docs/engineering/pull_requests/PR-024-reviewer-auth-workbench.md`
- `CHANGELOG.md` 的 `Unreleased`

## 4. 为什么选择服务端 Session

个人项目只做审核员端时，JWT 会多出刷新、撤销和 SSE 携带的问题；localStorage JWT 还会暴露给页面脚本。Redis 已经是第二十二步的必需依赖，因此采用短期随机 token：浏览器只知道 token，Redis 保存身份和 CSRF，注销可以立即删除。生产 Cookie 必须 Secure；开发 HTTP 才允许关闭 Secure。

## 5. 首次本地启动

```bash
cd /Users/ahs/Documents/xianyu
cp .env.example .env
# 本地 HTTP 保持 XIANYU_AUTH_COOKIE_SECURE=false
.venv/bin/pip install -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/xianyu-seed
.venv/bin/xianyu-create-reviewer \
  --user-id user_reviewer_demo \
  --username reviewer.demo
```

CLI 会交互询问密码，至少 12 个字符。非交互环境可使用 Secret Store 注入：

```bash
XIANYU_REVIEWER_PASSWORD='从 Secret Store 注入的临时密码' \
  .venv/bin/xianyu-create-reviewer \
  --user-id user_reviewer_demo --username reviewer.demo
```

不要把上面的真实密码写到 `.env`、README、Shell 历史或 Git。账号存在时 CLI 是幂等更新；如果目标用户不是 REVIEWER 会拒绝覆盖。

启动 API/Worker（`.env` 中的 Redis、数据库必须一致）：

```bash
.venv/bin/uvicorn dispute_agent.api:app --reload
.venv/bin/xianyu-worker
open http://127.0.0.1:8000/workbench
```

浏览器先看到登录页。登录成功后 `/auth/me` 返回 reviewer 显示信息和只存内存的 CSRF Token；浏览器不会把密码或 Session 放进 localStorage。

## 6. API 调试流程

登录接口只返回审核员摘要，不返回 Session 原值；Session 通过 `Set-Cookie` 写入 Cookie Jar。下面示例用 `curl` 保存 Cookie，并先读取 CSRF：

```bash
BASE=http://127.0.0.1:8000
COOKIE=/tmp/xianyu-reviewer.cookies

curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "$BASE/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"reviewer.demo","password":"<从 Secret Store 读取>"}'

curl -sS -b "$COOKIE" "$BASE/auth/me" > /tmp/xianyu-me.json
CSRF=$(python -c 'import json; print(json.load(open("/tmp/xianyu-me.json"))["csrf_token"])')

curl -sS -b "$COOKIE" "$BASE/cases?limit=50"
curl -sS -b "$COOKIE" -N "$BASE/workflow-jobs/<job_id>/events"
curl -sS -b "$COOKIE" -X POST "$BASE/cases/case_clear_mismatch/workflow" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' -d '{}'

curl -sS -b "$COOKIE" -X POST "$BASE/auth/logout" \
  -H "X-CSRF-Token: $CSRF"
```

健康检查是唯一不需要登录的业务外接口：`/health`、`/ready`、`/worker/health`、`/workflow/health`。`/docs`、案件 API、Agent 输出和 SSE 都需要 Session；所有 POST/PUT/PATCH/DELETE（包括 logout）还要 CSRF。

## 7. 服务端身份规则

| 场景 | 业务字段 | 可信审计字段 |
| --- | --- | --- |
| 买方 Claim | `submitted_by_id=user_buyer_demo` | `disputes.recorded_by_id=reviewer_auth`（审核员代录时） |
| 卖方聊天/陈述 | `sender_role=SELLER` | 导入请求的 Session reviewer |
| 文字证据 | `submitted_by=BUYER/SELLER`、`submitter_id` | `evidence.recorded_by_id` |
| 补问后的新证据 | `target=BUYER/SELLER` | `evidence.recorded_by_id` + CaseEvent 的 Session reviewer |
| 审批 | 客户端可带 `reviewer_id` 兼容字段 | `approvals.reviewer_id=Session reviewer` |
| Workflow 控制 | 客户端可带 `actor_id` 兼容字段 | `workflow_jobs.actor_id=Session reviewer` |
| 工具调用 | `actor=REVIEWER` 是权限角色 | `tool_calls.actor_id=Session reviewer` |
| 申诉代录 | `appellant_id/appellant_role` 仍表示买家/卖家 | `appeals.recorded_by_id=Session reviewer` |

认证关闭时（只用于旧单测）才保留请求体身份的兼容行为；staging/production 的配置校验会阻止关闭认证。

## 8. Docker/Compose 生产形态

开发 Compose 会把认证配置注入 API、Worker：

```env
XIANYU_AUTH_ENABLED=true
XIANYU_AUTH_SESSION_TTL_SECONDS=28800
XIANYU_AUTH_COOKIE_SECURE=false  # 只有本地 HTTP
```

staging/production 必须是：

```env
XIANYU_AUTH_ENABLED=true
XIANYU_AUTH_COOKIE_SECURE=true
XIANYU_PUBLIC_BASE_URL=https://staging.example.com
```

启动并创建账号：

```bash
docker compose --env-file deploy/env/staging.env \
  -f compose.yaml -f deploy/compose.staging.yaml up -d --build

docker compose --env-file deploy/env/staging.env \
  --profile tools run --rm \
  -e XIANYU_REVIEWER_USER_ID=user_reviewer_demo \
  -e XIANYU_REVIEWER_USERNAME=reviewer.demo \
  create-reviewer
```

若使用 Compose 的交互终端不可用，使用 `docker compose run -e XIANYU_REVIEWER_PASSWORD` 从运行时 Secret 注入。数据库 migration 必须先成功；账号 CLI 只更新用户表，不绕过 API 权限。

Compose 会读取项目根目录 `.env`。如果该文件仍配置 `sqlite:///...`，又没有传 `--env-file`，容器内 migration 可能错误地尝试访问 `/app/data`。部署命令必须显式指定目标环境文件，并用下面命令确认最终配置确实指向 PostgreSQL：

```bash
docker compose --env-file deploy/env/staging.env config | rg XIANYU_DATABASE_URL
```

不要把 `docker compose config` 的完整输出贴到公开日志，因为生产环境文件可能包含数据库或模型密钥。

## 9. 测试与验收

```bash
.venv/bin/python -m compileall -q src tests alembic
.venv/bin/pytest tests/test_reviewer_auth.py -q
.venv/bin/pytest

# migration 往返（临时 SQLite）
XIANYU_DATABASE_URL=sqlite:////tmp/xianyu-auth.db .venv/bin/alembic upgrade head
XIANYU_DATABASE_URL=sqlite:////tmp/xianyu-auth.db .venv/bin/alembic downgrade e3b7a1c9d5f2
XIANYU_DATABASE_URL=sqlite:////tmp/xianyu-auth.db .venv/bin/alembic upgrade head
```

验收重点：

1. 错误密码和非 REVIEWER 账号都只返回相同的通用 401，不泄露账号是否存在。
2. 未登录访问案件/SSE 返回 401；健康检查仍是 200。
3. 缺失/错误 CSRF 返回 403。
4. 请求体伪造 `reviewer_id=admin` 时，数据库审批人仍是登录审核员。
5. 过期或注销 Session 不能继续访问；停用账号后已有 Session 也会被 middleware 拒绝。
6. Secure Cookie 只在 HTTPS staging/production 打开。
7. Session Redis 不可用时登录/鉴权返回 503，不伪装成密码错误，也不静默绕过认证。
8. 登录页和工作台真实浏览器烟雾测试无脚本错误，工作台显示 Session 中的 reviewer。

## 10. 发布、回滚和后续迭代

发布前按企业流程创建 Issue-024、ADR-0006 和 PR-024；在 staging 先执行 migration、创建临时审核员、走一遍登录→Workflow→SSE→审批→执行→注销 smoke test，再合并/打 tag。回滚优先回滚 API/Worker 镜像；不要因为应用回滚就自动 downgrade 用户凭证字段。

后续如果要开放买家/卖家登录，应新增独立身份入口和案件级 ACL，不能把本步骤的 `REVIEWER` middleware 简单改成“所有 User 都能访问”。

完成本地提交后由开发者手动推送：

```bash
git status --short
git show --stat --oneline HEAD
git push -u origin feat/reviewer-auth-workbench
```

PR 合并前至少由自己以“Reviewer”视角再检查一次：请求体伪造身份是否被忽略、数据库审计人是否正确、错误响应是否泄露账号存在性、staging Cookie 是否带 `Secure`。个人项目没有第二位同事，也应该保留这份自审证据。
