# ISSUE-027：CI/CD 和版本发布

## 背景

仓库已有 Issue、分支、PR、ADR、Docker 和部署说明，但编译、测试、迁移、镜像构建和发布仍主要依赖手工执行，容易漏项，也无法给 PR 提供统一的合并门禁。

第二十五步用户端产品页和第二十六步完整可观测性本轮明确延期；第二十七步只依赖现有测试、异步 Workflow、Alembic 和第二十三步部署基础，因此可以独立提前实施。

## 范围

- PR/main 自动执行 Python、Workflow、PostgreSQL migration、前端、依赖和 Docker 检查；
- main CI 成功后发布 commit-SHA 镜像；
- 可配置的 staging 自动部署与 smoke test；
- production 手动 Workflow 和 Environment 审批；
- 语义版本 Tag、版本镜像、GitHub Release、Changelog 和发布检查清单；
- Dependabot 与 PR 模板。

## 验收标准

- [x] CI 覆盖需求列出的八类检查，并且任一失败会阻止镜像构建。
- [x] staging 只消费通过 CI 的不可变 SHA 镜像。
- [x] 未配置服务器时 staging 明确显示 skipped。
- [x] production 要求手动确认、完整 SHA 和 Environment。
- [x] Tag、项目版本和 Changelog 不一致时 Release 失败。
- [x] migration、smoke test、回滚和 GitHub 配置有可复现文档。
- [x] 第二十五、二十六步没有被隐式实现或宣称完成。

## 非目标

不实现买卖家 UI、管理员控制台、Prometheus、OpenTelemetry、Sentry、Kubernetes、多区域发布、蓝绿流量切换或自动数据库 downgrade。
