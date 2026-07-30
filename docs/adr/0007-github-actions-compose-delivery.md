# ADR-0007：GitHub Actions、GHCR 与 Compose 交付

- 状态：Accepted
- 日期：2026-07-29

## 背景

项目已经有 FastAPI、RQ Worker、PostgreSQL、Redis、Caddy、Alembic 和 staging/production Compose，但检查仍依赖开发者手工执行。个人项目需要能展示真实发布边界，又不应为了面试项目引入 Kubernetes、Argo CD 或云厂商专属平台。

## 决策

1. Pull Request 和 main 使用同一套 GitHub Actions CI：编译、全量测试、异步回归、PostgreSQL migration 往返、前端语法、依赖漏洞和双镜像构建。
2. main CI 成功后才发布 API/Worker 镜像；镜像使用完整 commit SHA，不使用可漂移的 `latest` 作为部署依据。
3. 配置好服务器时自动部署 staging 并执行健康 smoke test；没有配置时明确跳过部署，不伪造成功环境。
4. production 只能通过手动 Workflow 启动，要求输入已经过 staging 的 SHA，并绑定 GitHub `production` Environment 审批。
5. `vX.Y.Z` Tag 必须与 `pyproject.toml` 和带日期 Changelog 一致，之后才创建版本镜像与 GitHub Release。
6. 远端继续使用第二十三步的 Docker Compose；Secret 只保存在 GitHub Environment 和服务器 `.env`。
7. SSH host key 必须通过预先核验的 `DEPLOY_KNOWN_HOSTS` 提供；production SHA 必须属于 `origin/main`。

## 取舍

- 不采用 Kubernetes/GitOps：当前只有 API 和 Worker 两个应用进程，Compose 已满足个人项目演示和小规模部署。
- 不依赖第二十五步用户页面或第二十六步完整可观测性；缺少 Trace/Prometheus 不会阻止构建发布，但会限制线上定位能力，因此 production 仍应谨慎开放。
- SSH 部署适合单机 staging/production；规模扩大后可保持镜像和门禁契约，替换部署 Job 而无需改应用。
- GitHub Action 暂按官方主版本引用并由 Dependabot 更新；对更高供应链要求的环境应进一步固定到完整 Action commit SHA。
