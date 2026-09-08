# 开发与发布

[返回 README](../README.md)

插件依赖使用 `uv` 管理：

```bash
uv sync
```

依赖变更后同步 AstrBot 加载所需的依赖文件：

```bash
uv add <package>
uv export --no-hashes --format requirements-txt > requirements.txt
```

Python 回归依赖 AstrBot 运行环境，从 **AstrBot 仓库根目录**执行：

```bash
uv run pytest data/plugins/astrbot_plugin_memoir/tests -q
```

从**插件根目录**运行静态检查和 WebUI 回归：

```bash
ruff format .
ruff check .
npm install --no-save --package-lock=false playwright@1.62.1
npx playwright install chromium
node --test tests/test_webui.cjs
```

浏览器测试使用模拟 bridge，不连接真实记忆服务。CI 包含 Python 回归、Ruff 和 Chromium WebUI 检查。

### 自动发布

发布前同步 `metadata.yaml` 与 `pyproject.toml` 的版本号，运行 `uv lock`，并把待发布内容从 `CHANGELOG.md` 的 `Unreleased` 移入 `## [版本号] — 日期`。在干净工作区使用短期 `release/*` 分支完成版本整理和验证，合入 `main` 后，从对应提交推送 `v版本号` 标签。

```bash
python scripts/release_notes.py --tag v0.4.0 --output dist/release-notes.md
```

将命令中的版本号替换为本次版本。脚本会检查标签、插件元数据、项目版本、锁文件和 CHANGELOG 是否一致；版本段落缺失、重复或为空都会阻止发布。

标签会触发 Release 流水线：先运行全部 CI，再打包插件 ZIP、生成 `SHA256SUMS`，上传附件，最后发布 Release。**Release 正文直接取自该版本的 CHANGELOG 段落**，不使用 GitHub 自动生成的提交列表。开发版、测试版和候选版标记为预发布。

发布任务中断时，可在 GitHub Actions 的 Release 工作流手动运行，填写已存在的版本标签。流水线会重新验证该标签对应的代码，补齐附件并更新相同 Release；不会创建重复版本。

<details>
<summary><strong>代码与资源结构</strong></summary>

```text
core/
  event_handler.py     生命周期与消息钩子
  memory_writer.py     私聊与群聊原文捕获
  media_processor.py   媒体与转发共用队列、失败恢复
  forward_parser.py    转发快照、适配器展开与共享预算
  llm_helper.py        模型调用与媒体描述
  consolidation.py     批量抽取、洞察、桥接与遗忘
  memory_recall.py     分层检索与提示词注入
  scope.py             会话归属与配置合并
  storage.py           SQLite 存储与事务
  web_api.py           管理页接口
pages/memoir/          WebUI 页面、样式与脚本
tests/                Python 与浏览器回归
docs/assets/          图标矢量源与演示截图
logo.png              AstrBot 自动发现的插件图标
CHANGELOG.md          发布历史与未发布变化
```

</details>
