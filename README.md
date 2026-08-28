# astrbot_plugin_memoir

模拟人脑记忆闭环（编码 -> 巩固 -> 遗忘 -> 召回 -> 洞察）的 AstrBot 长期记忆插件。

不使用向量数据库 / RAG，检索基于 SQLite FTS5 关键词匹配 + 结构化字段（重要性、时效、命中强度）。

私聊与群聊采用完全不同的记忆策略：

- 私聊：低编码门槛，语义记忆（人物画像）为主，衰减慢，持续巩固。
- 群聊：两级门控（规则预过滤 + 小模型判定）控制编码密度，情景记忆为主，衰减快，巩固按数量或静默时长周期触发。
- 可选：群聊中经用户授权的自我陈述，可桥接进其私聊记忆（默认关闭，带敏感度过滤）。

代码结构：

```
core/
  event_handler.py    钩子入口
  memory_recall.py    on_llm_request：检索 + 注入
  memory_writer.py    on_llm_response / 群消息：编码
  consolidation.py    周期巩固任务：升华语义记忆 + 洞察 + 遗忘
  scope.py            私聊/群聊 scope 归属解析
  storage.py          aiosqlite 封装
```

## 开发

```bash
uv sync
```

依赖变更后同步 AstrBot 加载所需的 requirements.txt：

```bash
uv add <package>
uv export --no-hashes --format requirements-txt > requirements.txt
```
