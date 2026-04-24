# ST-Evolution-World-Assistant → PlotPilot 迁移计划

## 目标

将 ST 插件中的 workflow、dedup、snapshot、UI 思路迁移为 PlotPilot 原生插件。

## 第一阶段

- 使用 PlotPilot 插件平台 manifest 能力声明。
- 注册 `after_commit` 与 `before_context_build` hook。
- 用 sidecar storage 保存章节事实快照。
- 生成前注入最近章节事实摘要。

## 第二阶段

- 从 ST 版抽离 host-independent core。
- 将 worldbook 写入改为 PlotPilot sidecar entity。
- 将 floor snapshot 改为 chapter/content_hash snapshot。
- 实现手动 rebuild 与 rollback。

## 第三阶段

- 迁移 Vue 面板、FAB、debug panel。
- 接入 LLM fact extractor。
- 增加 CharacterCard、WorldEvent、FactionState、RegionState。
