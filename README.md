# pp-Evolution-World-Assistant

PlotPilot 版 Evolution World Assistant 插件，正式插件目录名为 `world_evolution_core`。

本仓库用于把 `ST-Evolution-World-Assistant` 的运行时思想迁移到 PlotPilot 插件平台：章节事实提取、动态角色状态、世界演化状态、上下文注入、任务去重、重跑与回滚。

## 当前阶段

当前提交已经具备 PlotPilot 插件闭环：

- `plugin.json` 声明 PlotPilot 插件能力、权限、hook、前端资源。
- `__init__.py` 注册后端 hook 与 API router。
- `service.py` 提供 `after_commit`、`before_context_build`、章节重跑、回滚、审查与 ST 预设导入。
- `repositories.py` 持久化章节事实、人物卡、运行记录与导入流。
- `structured_extractor.py` 支持结构化抽取，并在失败时回落到确定性抽取。
- `static/inject.js` 接入 `window.PlotPilotPlugins` runtime 并显示人物卡册、时间线、快照与导入流面板。
- 人物卡包含外貌、属性、世界观自定义字段、认知/成长/能力边界，以及“性格调色盘”（底色、主色调、点缀与衍生行为）。

## 安装

先在目标 PlotPilot 宿主中安装/启用插件平台，再安装本插件。本仓库只发布 `plugins/world_evolution_core/` 业务插件本体，不携带也不修改 `plugins/loader.py` 或 `plugins/platform/**`。

打包 `plugins/world_evolution_core/` 目录为 zip 后，通过 PlotPilot 插件管理页上传；或把该目录复制到 PlotPilot 宿主仓库的 `plugins/` 下。

```bash
cd plugins
zip -r world_evolution_core.zip world_evolution_core
```

本仓库测试使用 `tests/host_shims/plugins/platform/` 作为最小宿主 shim；该目录只服务测试，不属于插件发布内容。

## 迁移路线

1. 从 ST 版抽离 host-independent core：pipeline、settings、dedup、snapshot。
2. 用 PlotPilot adapter 替换 SillyTavern adapter。
3. 将 worldbook/floor 语义映射为 PlotPilot sidecar state/chapter snapshot。
4. 接入 PlotPilot `before_context_build` 与 `after_commit`。
5. 再逐步迁移 Vue 面板、FAB、debug panel。
