# 第二十七步：CI/CD 和版本发布

## 1. 能否跳过第二十五、二十六步

可以。本步的直接依赖是：

- 自动化测试和异步 Workflow（第二十二步）；
- PostgreSQL、Redis、API/Worker 镜像、Compose 和健康检查（第二十三步）；
- 当前审核员端认证（第二十四步，保证部署后工作台不是裸奔接口）。

第二十五步是产品入口，第二十六步是线上定位能力。它们很重要，但不是“代码能否构建、迁移、生成镜像和按门禁发布”的技术前置。本项目当前定位为个人作品和小规模演示，因此先做第二十七步是合理的。必须诚实保留两个边界：别人暂时只有审核员工作台；发生复杂线上故障时还没有完整 Trace/指标平台。

## 2. 本步目标与简化边界

本步把开发者手工清单变为可审查的流水线：

```text
Pull Request / main
  → compile + repository validation
  → full pytest
  → async workflow regression
  → PostgreSQL migration round trip
  → frontend JavaScript syntax
  → dependency vulnerability audit
  → API / Worker image build

main CI success
  → publish API / Worker images tagged by full commit SHA
  → deploy staging when configured
  → /health + /ready + /worker/health smoke test

reviewed staging SHA
  → manually run production workflow
  → GitHub production Environment approval
  → deploy the exact same SHA images

vX.Y.Z tag
  → validate pyproject + CHANGELOG
  → publish version images
  → create GitHub Release
```

没有引入 Kubernetes、Argo CD、Terraform、Sentry、OpenTelemetry 或 Prometheus。真实服务器没有配置前，流水线会构建并发布镜像，但 staging 部署 Job 显示 `skipped`，而不是假装上线成功。

## 3. 实际交付文件

| 文件 | 作用 |
| --- | --- |
| `.github/workflows/ci.yml` | PR/main 的统一质量门禁 |
| `.github/workflows/publish-staging.yml` | CI 成功后发布 SHA 镜像并按配置部署 staging |
| `.github/workflows/deploy-production.yml` | 手动 production 发布与 Environment 门禁 |
| `.github/workflows/release.yml` | Tag 校验、版本镜像和 GitHub Release |
| `.github/dependabot.yml` | 每周检查 Python、Action 和 Docker 更新 |
| `.github/PULL_REQUEST_TEMPLATE.md` | 强制 PR 说明验证、migration 和回滚 |
| `scripts/check_frontend.py` | 提取 HTML 内联脚本并使用 Node `--check` |
| `scripts/check_release.py` | 校验语义 Tag、`project.version` 和日期 Changelog |
| `scripts/smoke_deployment.py` | 等待三个健康端点就绪 |
| `deploy/compose.release.yaml` | 用 GHCR 镜像覆盖本地 build 服务 |
| `deploy/scripts/deploy_release.sh` | 远端 Compose 配置检查、pull、migration gate 和启动 |
| `docs/RELEASE_CHECKLIST.md` | 发布、migration、production 和回滚清单 |

## 4. PR CI 如何工作

`CI` Workflow 在 Pull Request、main push 和手动触发时运行：

1. `python-tests`：安装 `.[dev]`、compileall、基础契约校验和全量 pytest，并保存 JUnit 报告。
2. `async-workflow-tests`：单独运行队列、重试、暂停、取消、恢复和 Worker 健康回归，方便一眼定位异步层失败。
3. `migrations`：启动临时 PostgreSQL 17，执行完整升级、PostgreSQL 类型测试、降到 base、重新升级和 schema drift 检查。
4. `frontend`：Node 22 检查 `web/*.html` 中的内联 JavaScript；不需要为了两个静态页面引入 npm 工程。
5. `dependency-audit`：用 `pip-audit` 扫描实际安装的 Python 依赖。
6. `docker-build`：只有前五组检查全部成功才构建 API/Worker，并验证容器 UID 为 `10001`。

在 GitHub 仓库 `Settings → Branches → Branch protection rule` 中保护 `main`：要求 Pull Request，要求上述 CI checks，通过前禁止合并，并禁止 force push。个人项目可以不要求多人 review，但不要允许红灯合并。

## 5. GHCR 和 staging 配置

### 5.1 GitHub 仓库变量

第一次没有服务器时什么都不用配置，`STAGING_DEPLOY_ENABLED` 保持空值，发布流程只推 SHA 镜像。

有服务器后，在仓库级 Actions Variables 增加：

```text
STAGING_DEPLOY_ENABLED=true
```

创建名为 `staging` 的 GitHub Environment，并配置：

```text
Variables
DEPLOY_HOST=<服务器地址>
DEPLOY_PORT=22
DEPLOY_USER=<非 root 部署用户>
DEPLOY_PATH=/opt/xianyu
PUBLIC_BASE_URL=https://staging.example.com
GHCR_USERNAME=<GitHub 用户名>

Secrets
DEPLOY_SSH_KEY=<部署专用 SSH 私钥>
DEPLOY_KNOWN_HOSTS=<运维人员预先核验的服务器 SSH host key>
GHCR_PULL_TOKEN=<只有 read:packages 的 Token>
```

服务器上预先准备 `/opt/xianyu/deploy/env/staging.env`。流水线只复制 Compose、Caddy 和部署脚本，不覆盖这个 Secret 文件。部署用户需要访问 Docker；SSH Key、数据库密码和模型 Key 都不能进入仓库。`DEPLOY_KNOWN_HOSTS` 应由你在可信网络中通过服务器控制台核对指纹后生成，不能让 CI 在部署当下临时扫描并盲目信任。

### 5.2 发布语义

main CI 成功后，镜像形如：

```text
ghcr.io/<owner>/xianyu-api:<40位commit-sha>
ghcr.io/<owner>/xianyu-worker:<40位commit-sha>
```

staging 和 production 使用相同 SHA 镜像，避免“测试的是 A，生产重新构建成 B”。远端 `migrate` 服务成功后 API/Worker 才会启动，随后 Workflow 从 GitHub Runner 访问公网健康端点。

## 6. production 人工发布

创建 `production` Environment，配置与 staging 同名的变量/Secrets，并额外设置：

```text
PRODUCTION_DEPLOY_ENABLED=true
```

在 Environment Protection Rules 中增加 required reviewer。发布时：

1. 打开 Actions → `Deploy production` → Run workflow。
2. 输入 staging 已通过的完整 40 位 SHA。
3. 把 confirmation 选择为 `DEPLOY`。
4. GitHub 等待 production Environment 审批。
5. Workflow 确认 SHA 是 `origin/main` 的祖先，拒绝部署未合并功能分支。
6. 审批后拉取同一 SHA 镜像、执行 migration gate 并跑 smoke test。

个人项目如果没有 production 主机，不要开启 `PRODUCTION_DEPLOY_ENABLED`，也不要为了截图把 staging 冒充 production。

## 7. 发布 v0.2.0 示例

不要直接在功能分支创建版本 Tag。先开发布 PR：

```bash
git fetch origin
git switch main
git pull --ff-only
git switch -c release/v0.2.0
```

在发布 PR 中把 `pyproject.toml` 的版本改为 `0.2.0`，并把 Changelog 的相关 `Unreleased` 内容移动到：

```text
## [0.2.0] - 2026-07-29
```

写清 migration、旧版本兼容性和回滚 SHA。合并且 main CI 通过后：

```bash
git switch main
git pull --ff-only
.venv/bin/python scripts/check_release.py --tag v0.2.0
git tag -a v0.2.0 -m "Release v0.2.0"
git push origin v0.2.0
```

`Version release` 会再次检查 Tag 必须位于 main、版本一致且 Changelog 有日期，然后创建 `:v0.2.0` 镜像和 GitHub Release。当前功能分支不提前打 Tag，这是企业流程中的职责分离：功能完成不等于版本已经发布。

## 8. 数据库迁移与回滚

本步没有新增数据库 migration，当前 head 是 `6f1a2b3c4d5e`。CI 的临时库可以安全 `downgrade base`；production 不应该照抄这个破坏性操作。

应用回滚时重新运行 `Deploy production`，输入上一个健康 SHA。Compose 会拉取旧 API/Worker 镜像并重新执行当前 migration gate。只有满足以下全部条件才考虑数据库 downgrade：revision 明确支持、旧应用不兼容当前 schema、没有新版本数据写入、备份已经验证。

完整勾选项见 `docs/RELEASE_CHECKLIST.md`。

## 9. 本次实际执行记录

### 9.1 分支

计划从 `feat/reviewer-auth-workbench` 创建 `feat/ci-cd-release`。执行时 Git 提示分支已存在；检查发现该分支恰好指向审核员认证提交 `a62b3fc`，没有额外提交，因此直接安全复用，没有删除或强制覆盖分支。

### 9.2 审查和实现命令

```bash
git status --short --branch
git branch -vv
git log --oneline --graph --decorate
rg --files tests deploy scripts
.venv/bin/alembic heads
```

实现后执行：

```bash
.venv/bin/python -m compileall -q src scripts tests alembic
.venv/bin/python scripts/check_frontend.py
bash -n deploy/scripts/deploy_release.sh
ruby -e 'require "yaml"; Dir[".github/workflows/*.yml"].each { |f| YAML.load(File.read(f)) }'
XIANYU_API_IMAGE=ghcr.io/example/xianyu-api:test \
XIANYU_WORKER_IMAGE=ghcr.io/example/xianyu-worker:test \
docker compose --env-file deploy/env/staging.env.example \
  -f compose.yaml -f deploy/compose.staging.yaml -f deploy/compose.release.yaml config
.venv/bin/pytest
.venv/bin/alembic check
docker build -f deploy/docker/api.Dockerfile -t xianyu-api:step27 .
docker build -f deploy/docker/worker.Dockerfile -t xianyu-worker:step27 .
```

### 9.3 遇到的问题

1. 分支重名：没有用 `git branch -D`，而是确认 commit 和工作区后复用，避免丢失未知工作。
2. macOS 系统 Ruby 2.6 的 Psych 不支持新版 `YAML.load_file(..., aliases:)` 参数；改成兼容的 `YAML.load(File.read(...))`。这不影响 Workflow，只影响本地验证命令。
3. Docker Compose 原本只有 `build`，不能保证 staging 与 production 使用 CI 构建的同一镜像；增加 release override，通过强制 `XIANYU_API_IMAGE/XIANYU_WORKER_IMAGE` 解决。
4. 仓库当前没有真实服务器和 Secret；因此只实现可配置 CD，并让未配置部署明确 skipped，不写假账号或假部署结果。
5. 首次真实执行 `pip-audit` 发现本地 `pip 25.0.1` 和 `pytest 8.4.2` 已有公开漏洞；审计 Job 先升级到 `pip>=26.1.2`，项目测试依赖提升到 `pytest>=9.0.3`，并以全量测试确认兼容性，没有用 ignore 列表掩盖。

### 9.4 最终验证结果

```text
Actionlint / ShellCheck: PASS
CI/CD 专项测试: 5 passed
全量测试: 108 passed, 1 skipped
pip-audit: No known vulnerabilities found
PostgreSQL 17 migration 往返与集成测试: PASS
staging / production Compose release config: PASS
API / Worker Docker build: PASS
容器运行用户: 10001:10001
```

全量测试中的一个 skip 是需要显式 PostgreSQL URL 的集成用例；本次已在一次性 PostgreSQL 17 容器中单独运行并通过。GitHub Hosted Runner 的结果只有在分支推送后才能产生，不能用本地结果冒充远端 CI。

## 10. 当前仍未完成的能力

- 第二十五步的买卖家页面和管理员 Job 控制台；
- 第二十六步的 request/job/case 全链路 Trace、指标、Sentry 和告警；
- 云主机、域名、GitHub Environment Secret 的真实配置；
- 首次分支推送后的 GitHub Hosted Runner 结果。

这些不会阻止本项目作为审核员端作品运行，但在宣称“公开多用户生产系统”前仍需补齐。
