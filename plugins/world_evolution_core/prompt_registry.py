"""Prompt Plaza registry entries for Evolution World."""
from __future__ import annotations

import json
from typing import Any

PLUGIN_OWNER = "plugin:world_evolution_core"

EVOLUTION_PROMPTS: list[dict[str, Any]] = [
    {
        "id": "plugin.world_evolution_core.agent-decision",
        "name": "Evolution Agent 决策",
        "description": "根据当前 hook payload 生成 Evolution 控制卡与策略。",
        "category": "planning",
        "source": "plugins/world_evolution_core/agent_runtime.py::run_decision",
        "owner": PLUGIN_OWNER,
        "runtime_status": "active",
        "authority_domain": "planning",
        "runtime_reader": "prompt_manager",
        "editable": True,
        "output_format": "json",
        "tags": ["Evolution", "Agent", "控制卡"],
        "variables": [{"name": "prompt_text", "type": "string", "required": True, "desc": "插件组装好的决策上下文"}],
        "system": "你是 Evolution Agent Orchestrator。只输出 JSON，不写正文，不泄露密钥。",
        "user_template": "{prompt_text}",
    },
    {
        "id": "plugin.world_evolution_core.agent-reflection",
        "name": "Evolution Agent 反思",
        "description": "把章节审查问题固化为后续可执行经验。",
        "category": "review",
        "source": "plugins/world_evolution_core/agent_runtime.py::build_agent_reflection_prompt",
        "owner": PLUGIN_OWNER,
        "runtime_status": "active",
        "authority_domain": "review",
        "runtime_reader": "prompt_manager",
        "editable": True,
        "output_format": "json",
        "tags": ["Evolution", "反思", "Capsule"],
        "variables": [
            {"name": "chapter_number", "type": "number", "required": True, "desc": "章节号"},
            {"name": "capsule_lines", "type": "string", "required": False, "desc": "固化 capsule 摘要"},
            {"name": "issue_lines", "type": "string", "required": False, "desc": "审查问题列表"},
        ],
        "system": "你是 Evolution 智能体的反思器，不写小说正文。你只总结本轮审查暴露出的可复用经验，帮助后续章节减少连续性和人物逻辑错误。必须输出 JSON 对象，不要输出 Markdown。",
        "user_template": """【章节】
第{chapter_number}章

【本轮固化 Capsule】
{capsule_lines}

【审查问题】
{issue_lines}

请输出 JSON：
{{
  "problem_pattern": "本轮问题模式，80字内",
  "root_cause": "根因，160字内",
  "next_chapter_constraints": ["后续可执行约束1", "后续可执行约束2"],
  "evidence_refs": [{{"summary": "引用证据摘要"}}],
  "suggest_gene_candidate": false
}}

要求：
1. 只写后续可执行的写作/审查策略。
2. 不复述完整剧情。
3. 不新增事实设定。
4. 优先处理章节承接、人物路线、认知边界、能力边界、性格调色盘。""",
    },
    {
        "id": "plugin.world_evolution_core.structured-extraction",
        "name": "Evolution 结构化事实抽取",
        "description": "从章节正文抽取 Evolution 权威角色、事件、路线和世界事实。",
        "category": "extraction",
        "source": "plugins/world_evolution_core/structured_extractor.py::_build_structured_extraction_prompt",
        "owner": PLUGIN_OWNER,
        "runtime_status": "active",
        "authority_domain": "chapter_facts",
        "runtime_reader": "prompt_manager",
        "editable": True,
        "output_format": "json",
        "tags": ["Evolution", "事实抽取", "权威"],
        "variables": [
            {"name": "chapter_number", "type": "number", "required": True, "desc": "章节号"},
            {"name": "schema", "type": "json", "required": True, "desc": "JSON schema"},
            {"name": "content", "type": "string", "required": True, "desc": "章节正文"},
        ],
        "system": "你是 PlotPilot Evolution 的结构化事实抽取器。你只输出 JSON 对象，不写解释、不写 Markdown。必须从正文中抽取明确事实，禁止把书名、地点、物件、形容词短语、章节名误当人物。",
        "user_template": """请阅读第 {chapter_number} 章正文，输出符合 schema 的 JSON。

硬规则：
1. characters 只包含正文中明确出场或被明确提及的人物。
2. 每个人物必须尽量补全 appearance、attributes、world_profile、personality_palette。
3. 性格调色盘不是标签列表，而是行为模型：base 是底色，main_tones 是主色调，accents 是点缀，derivatives 写具体衍生行为。
4. 如果正文证据不足，可以给出“暂未定型/待观察”的保守调色盘，但不能留空。
5. known_facts/unknowns/misbeliefs 必须符合角色视角，不允许让角色知道未在场信息。
6. world_events 只记录本章发生的事实，不要总结未来。

JSON schema:
{schema}

正文：
{content}""",
    },
    {
        "id": "plugin.world_evolution_core.connection-test",
        "name": "Evolution API 连接测试",
        "description": "测试 Evolution Agent API 是否可用。",
        "category": "extraction",
        "source": "plugins/world_evolution_core/agent_runtime.py::build_llm_connection_test_prompt",
        "owner": PLUGIN_OWNER,
        "runtime_status": "active",
        "authority_domain": "settings",
        "runtime_reader": "prompt_manager",
        "editable": True,
        "output_format": "text",
        "tags": ["Evolution", "连接测试"],
        "variables": [],
        "system": "你是 API 连接测试器。",
        "user_template": "请只回复 OK 两个字母，不要添加任何解释。",
    },
    {
        "id": "plugin.world_evolution_core.context-usage-protocol",
        "name": "Evolution 上下文使用方式",
        "description": "控制 Evolution 注入块的阅读方式，避免逐条复述和长资料重复注入。",
        "category": "generation",
        "source": "plugins/world_evolution_core/context_patch.py::_render_usage_protocol",
        "owner": PLUGIN_OWNER,
        "runtime_status": "active",
        "authority_domain": "continuity",
        "runtime_reader": "prompt_manager",
        "editable": True,
        "output_format": "text",
        "tags": ["Evolution", "上下文", "去重"],
        "variables": [],
        "system": "你是 Evolution 上下文压缩器。",
        "user_template": "以下内容是角色连续性参考，不是本章任务清单；不要逐条复述，也不要为使用这些信息强行安排情节。章节承接状态是硬约束：下一章开头必须承接上一章结尾；若跳时空，需要先交代过渡。硬边界用于避免逻辑越界；软倾向只影响选择风格；可变状态可在本章新证据刺激下自然更新。默认按用户目标控制篇幅，本轮压力测试以约2500字/章为目标；超过3000字应主动收束场景。避免复用高频模板句，如没有说话、没有回答、声音很轻、深吸一口气、沉默了几秒、像是等。",
    },
]


def seed_evolution_prompts() -> None:
    try:
        from infrastructure.ai.prompt_manager import get_prompt_manager

        get_prompt_manager().seed_prompt_entries(
            EVOLUTION_PROMPTS,
            template_name="Evolution World Assistant",
            template_description="Evolution 插件运行时提示词",
            template_category="plugin",
            template_metadata={"plugin": "world_evolution_core"},
        )
    except Exception:
        # Plugin loading must not fail if Prompt Plaza DB is unavailable.
        return


def schema_to_text(schema: Any) -> str:
    return json.dumps(schema, ensure_ascii=False)
