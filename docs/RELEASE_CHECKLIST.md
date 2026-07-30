# 发布检查清单

这份清单用于 staging、production 和 Git Tag 发布。勾选项应写入发布 PR 或 GitHub Release，不要只保存在个人记忆中。

## 1. 发布前

- [ ] 发布提交已经合并到 `main`，CI 全部通过。
- [ ] `pyproject.toml` 的 `project.version` 是目标版本。
- [ ] `CHANGELOG.md` 已把相关内容从 `Unreleased` 移入带日期的版本段，例如 `## [0.2.0] - 2026-07-29`。
- [ ] 已检查新增 Alembic revision、升级耗时、锁表风险和旧应用兼容性。
- [ ] 已记录当前 production commit SHA、API/Worker 镜像和数据库备份位置。
- [ ] 没有把 `.env`、模型 Key、SSH 私钥、Cookie 或真实案件材料提交到 Git。
- [ ] staging/production 的 `DEPLOY_KNOWN_HOSTS` 指纹已通过服务器控制台核验。

## 2. staging

- [ ] main 的 SHA 镜像已发布到 GHCR。
- [ ] staging migration 成功，`/health`、`/ready`、`/worker/health` 均为 200。
- [ ] 审核员登录、案件读取、Workflow、SSE、人工审批和模拟执行 smoke test 通过。
- [ ] 模型失败会持久化为失败 Job，没有静默退回规则模型。
- [ ] 已查看 staging 日志，确认没有持续重启或 migration 重跑。

## 3. production 人工批准

- [ ] `Deploy production` 输入的是已通过 staging 的完整 40 位 commit SHA。
- [ ] GitHub `production` Environment 的 required reviewer 已批准。
- [ ] 已确认维护窗口、回滚负责人和上一个健康 SHA。
- [ ] production smoke test 通过。
- [ ] 发布后抽查一个只读案件；不要用真实资金动作验证个人项目。

## 4. Git Tag 与 GitHub Release

- [ ] Tag 使用语义版本，例如 `v0.2.0`，并且与 `project.version` 一致。
- [ ] Tag 指向 `main` 中已经通过 CI 的提交。
- [ ] `Version release` Workflow 成功创建版本镜像和 GitHub Release。
- [ ] Release notes 链接 Changelog、migration 和回滚说明。

## 5. 回滚

- [ ] 优先把 API/Worker 切回上一个健康 commit SHA 镜像。
- [ ] 确认旧应用能够读取当前 schema；不能仅因应用回滚就自动 downgrade 数据库。
- [ ] 只有在 migration 明确可逆、没有新版本数据写入且已有备份时才执行 Alembic downgrade。
- [ ] 回滚后重新执行三个健康探针和核心案件 smoke test。
- [ ] 在 Issue/PR 中记录原因、影响范围、时间线和后续修复。
