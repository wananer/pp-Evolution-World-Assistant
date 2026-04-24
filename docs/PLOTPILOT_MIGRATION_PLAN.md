# ST-Evolution-World-Assistant → PlotPilot 迁移计划

## 目标

将 ST 插件中的 workflow、dedup、snapshot、UI 思路迁移为 PlotPilot 原生插件。

## 当前能力：Phase 1 动态角色卡最小闭环

- `after_commit`：从已提交章节提取保守事实快照。
- `ChapterFactSnapshot`：保存章节摘要、显式角色、地点、世界事件。
- `CharacterCard`：维护角色首次/最近出现章节和近期事件。
- `before_context_build`：生成动态角色状态 + 近期章节事实上下文块。
- HTTP API：角色列表、角色详情、角色时间线、章节重跑、小说重建。
- 前端入口：FAB + 简单角色状态抽屉。

## 第二阶段

- 从 ST 版抽离 host-independent core。
- 将 worldbook 写入改为 PlotPilot sidecar entity。
- 将 floor snapshot 改为 chapter/content_hash snapshot。
- 实现真实 rollback 删除/归档语义。
- 接入 LLM fact extractor，替代当前保守正则提取器。

## 第三阶段

- 迁移 Vue 面板、FAB、debug panel。
- 增加 CharacterRelationshipEdge、WorldEvent、FactionState、RegionState。
- 增加任务队列、失败重跑、状态版本比较 UI。
