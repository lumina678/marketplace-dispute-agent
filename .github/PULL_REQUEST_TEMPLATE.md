## 变更目的

<!-- 说明用户问题、Issue 链接，以及为什么现在需要修改。 -->

## 主要改动

<!-- 用列表概括实现内容。 -->

## 验证

- [ ] Python 编译与全量测试通过
- [ ] 异步 Workflow 回归通过
- [ ] Alembic migration 往返与 `alembic check` 通过
- [ ] 前端 JavaScript 检查通过
- [ ] 依赖漏洞扫描已查看
- [ ] API/Worker Docker 镜像构建通过

## 数据库与兼容性

- 是否包含 migration：否
- 升级命令：不适用
- 是否允许安全 downgrade：不适用

## 发布与回滚

- 发布风险：
- staging 验证方式：
- 回滚到哪个镜像或提交：

## 安全检查

- [ ] 没有提交密码、Token、Cookie、真实案件或其他 Secret
- [ ] 新增写接口具备身份、CSRF、权限和幂等检查
