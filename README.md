# pp-Evolution-World-Assistant

PlotPilot 版 Evolution World Assistant 插件骨架。

本仓库用于把 `ST-Evolution-World-Assistant` 的运行时思想迁移到 PlotPilot 插件平台：章节事实提取、动态角色状态、世界演化状态、上下文注入、任务去重、重跑与回滚。

## 当前阶段

当前提交是 PlotPilot 插件最小骨架：

- `plugin.json` 声明 PlotPilot 插件能力、权限、hook、前端资源。
- `__init__.py` 注册后端 hook 与 API router。
- `service.py` 提供 `after_commit` 与 `before_context_build` 最小闭环。
- `routes.py` 提供状态、角色列表、章节重跑、小说重建接口占位。
- `static/inject.js` 接入 `window.PlotPilotPlugins` runtime 并显示前端面板入口。

## 安装

打包 `plugins/evolution_world_assistant/` 目录为 zip 后，通过 PlotPilot 插件管理页上传；或把该目录复制到 PlotPilot 宿主仓库的 `plugins/` 下。

```bash
cd plugins
zip -r evolution_world_assistant.zip evolution_world_assistant
```

## 迁移路线

1. 从 ST 版抽离 host-independent core：pipeline、settings、dedup、snapshot。
2. 用 PlotPilot adapter 替换 SillyTavern adapter。
3. 将 worldbook/floor 语义映射为 PlotPilot sidecar state/chapter snapshot。
4. 接入 PlotPilot `before_context_build` 与 `after_commit`。
5. 再逐步迁移 Vue 面板、FAB、debug panel。
