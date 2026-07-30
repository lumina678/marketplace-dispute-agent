# PR-027：CI/CD 和版本发布

## 摘要

增加 GitHub Actions CI、commit-SHA 镜像发布、可选 staging 自动部署、production 人工门禁以及语义版本 Release。继续复用 Docker Compose，未增加云平台或 Kubernetes 依赖。

## 主要改动

- `.github/workflows/ci.yml`：编译、测试、异步回归、PostgreSQL migration、前端、漏洞扫描和镜像构建。
- `.github/workflows/publish-staging.yml`：main CI 成功后发布 GHCR SHA 镜像；配置服务器后自动部署 staging。
- `.github/workflows/deploy-production.yml`：手动输入已验证 SHA，经 production Environment 批准后部署。
- `.github/workflows/release.yml`：验证 Tag/版本/Changelog，发布版本镜像并创建 GitHub Release。
- `deploy/compose.release.yaml` 和 `deploy/scripts/deploy_release.sh`：让远端 Compose 消费不可变镜像。
- `scripts/check_frontend.py`、`scripts/check_release.py`、`scripts/smoke_deployment.py`：可在本地和 CI 复用的确定性检查。
- 修复首次漏洞扫描发现的旧 pip/pytest 风险：审计环境使用 `pip>=26.1.2`，开发约束升级为 `pytest>=9.0.3`。
- 发布检查清单、ADR、Dependabot、PR 模板和复盘文档。

## 数据库

本 PR 没有新增 migration。CI 会在临时 PostgreSQL 17 上执行 `upgrade head → downgrade base → upgrade head → alembic check`。生产回滚默认只回滚应用镜像，不自动 downgrade schema。

## 验证记录

```text
python compileall: PASS
validate_foundation.py: PASS
actionlint + ShellCheck: PASS
GitHub Actions YAML parse: PASS（4 个 Workflow）
CI/CD 专项测试: PASS（5 passed）
异步 Workflow + 部署专项: PASS
pytest 全量: PASS（108 passed, 1 skipped）
前端 JavaScript: PASS（2 个内联脚本块）
pip-audit: PASS（No known vulnerabilities found）
PostgreSQL 17: PASS（upgrade head → integration test → downgrade base → upgrade head → alembic check）
staging/production Compose release config: PASS
API/Worker Docker build: PASS
API/Worker non-root UID: PASS（10001:10001）
```

`tests/test_postgresql_integration.py` 在普通 SQLite 全量测试中按设计 skipped；它已经在独立 PostgreSQL 17 容器验证中通过。GitHub Hosted Runner 的首次真实结果需要分支推送并创建 PR 后确认。

## 发布与回滚

本分支基于 PR-024。PR-024 未合并前，PR-027 base 使用 `feat/reviewer-auth-workbench`；合并后 rebase 到 `origin/main` 并把 base 改为 `main`。详细服务器变量、Secret、Tag 和回滚操作见 `docs/STEPS_27.md` 与 `docs/RELEASE_CHECKLIST.md`。
