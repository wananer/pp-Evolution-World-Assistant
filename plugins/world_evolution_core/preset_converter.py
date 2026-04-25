"""Convert ST Evolution/SillyTavern preset-like JSON into PlotPilot Evolution flows.

This converter is intentionally declarative: it preserves prompts, generation
settings, regex rules, and selector hints, but never executes EJS/controller code.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha1
from typing import Any


@dataclass
class PlotPilotEvolutionFlow:
    id: str
    name: str
    enabled: bool = True
    priority: int = 100
    trigger: str = "after_commit"
    timeout_ms: int = 300000
    prompt_order: list[dict[str, Any]] = field(default_factory=list)
    generation_options: dict[str, Any] = field(default_factory=dict)
    behavior_options: dict[str, Any] = field(default_factory=dict)
    regex_rules: list[dict[str, Any]] = field(default_factory=list)
    selectors: list[dict[str, Any]] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    source: str = "st_preset"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def convert_st_preset(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("preset must be a JSON object")

    flows = _extract_flows(raw)
    converted = [_convert_flow(flow, index) for index, flow in enumerate(flows)]
    return {
        "schema_version": 1,
        "source": _detect_source(raw),
        "flows": [flow.to_dict() for flow in converted],
        "unsupported": _global_unsupported(raw),
    }


def _extract_flows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else raw
    flows = settings.get("flows") if isinstance(settings.get("flows"), list) else None
    if flows:
        return [flow for flow in flows if isinstance(flow, dict)]
    return [raw]


def _convert_flow(flow: dict[str, Any], index: int) -> PlotPilotEvolutionFlow:
    name = str(flow.get("name") or flow.get("preset_name") or flow.get("name1") or f"Imported Flow {index + 1}").strip()
    prompt_order = _convert_prompt_order(flow)
    generation_options = _convert_generation_options(flow)
    regex_rules = _convert_regex_rules(flow)
    selectors = _selector_hints(flow, prompt_order)
    unsupported = _flow_unsupported(flow)

    return PlotPilotEvolutionFlow(
        id=_flow_id(name, index),
        name=name or f"Imported Flow {index + 1}",
        enabled=flow.get("enabled") is not False,
        priority=_int(flow.get("priority"), 100),
        trigger=_map_trigger(flow.get("trigger") or flow.get("trigger_timing")),
        timeout_ms=_int(flow.get("timeout_ms"), 300000),
        prompt_order=prompt_order,
        generation_options=generation_options,
        behavior_options=_convert_behavior_options(flow),
        regex_rules=regex_rules,
        selectors=selectors,
        unsupported=unsupported,
    )


def _convert_prompt_order(flow: dict[str, Any]) -> list[dict[str, Any]]:
    prompts = flow.get("prompts") if isinstance(flow.get("prompts"), list) else []
    prompt_map = {item.get("identifier"): item for item in prompts if isinstance(item, dict) and item.get("identifier")}
    order_entries: list[dict[str, Any]] = []
    st_order = flow.get("prompt_order") if isinstance(flow.get("prompt_order"), list) else []
    if st_order and isinstance(st_order[0], dict) and isinstance(st_order[0].get("order"), list):
        order_entries = [item for item in st_order[0]["order"] if isinstance(item, dict)]

    identifiers = [entry.get("identifier") for entry in order_entries if entry.get("identifier")]
    if not identifiers:
        identifiers = [item.get("identifier") for item in prompts if isinstance(item, dict) and item.get("identifier")]

    result = []
    seen = set()
    for identifier in identifiers:
        if identifier in seen:
            continue
        seen.add(identifier)
        prompt = prompt_map.get(identifier, {})
        enabled_override = next((entry.get("enabled") for entry in order_entries if entry.get("identifier") == identifier), None)
        result.append(
            {
                "identifier": identifier,
                "name": str(prompt.get("name") or identifier),
                "enabled": bool(enabled_override if enabled_override is not None else prompt.get("enabled", True)),
                "type": "marker" if prompt.get("marker") is True else "prompt",
                "role": _role(prompt.get("role")),
                "content": str(prompt.get("content") or ""),
                "injection_position": _injection_position(prompt.get("injection_position")),
                "injection_depth": _int(prompt.get("injection_depth"), 0),
            }
        )
    if result:
        return result
    prompt = str(flow.get("prompt") or flow.get("request_template") or "").strip()
    return [{"identifier": "main", "name": "Main Prompt", "enabled": True, "type": "prompt", "role": "system", "content": prompt, "injection_position": "relative", "injection_depth": 0}]


def _convert_generation_options(flow: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "temperature": "temperature",
        "top_p": "top_p",
        "frequency_penalty": "frequency_penalty",
        "presence_penalty": "presence_penalty",
        "openai_max_context": "max_context_tokens",
        "max_context_tokens": "max_context_tokens",
        "openai_max_tokens": "max_reply_tokens",
        "max_reply_tokens": "max_reply_tokens",
        "stream_openai": "stream",
        "stream": "stream",
        "max_context_unlocked": "unlock_context_length",
        "unlock_context_length": "unlock_context_length",
    }
    result: dict[str, Any] = {}
    for source, target in mapping.items():
        if source in flow:
            result[target] = flow[source]
    return result


def _convert_behavior_options(flow: dict[str, Any]) -> dict[str, Any]:
    behavior = flow.get("behavior_options") if isinstance(flow.get("behavior_options"), dict) else {}
    result = dict(behavior)
    for key in ("structured_output", "reasoning_effort", "verbosity", "request_thinking"):
        if key in flow and key not in result:
            result[key] = flow[key]
    return result


def _convert_regex_rules(flow: dict[str, Any]) -> list[dict[str, Any]]:
    rules = []
    for item in flow.get("custom_regex_rules") or []:
        if not isinstance(item, dict):
            continue
        rules.append({"id": str(item.get("id") or _flow_id(str(item), 0)), "name": str(item.get("name") or ""), "enabled": item.get("enabled") is not False, "find_regex": str(item.get("find_regex") or ""), "replace_string": str(item.get("replace_string") or "")})

    extensions = flow.get("extensions") if isinstance(flow.get("extensions"), dict) else {}
    regexes = (((extensions.get("SPreset") or {}) if isinstance(extensions.get("SPreset"), dict) else {}).get("RegexBinding") or {})
    if isinstance(regexes, dict):
        for item in regexes.get("regexes") or []:
            if not isinstance(item, dict):
                continue
            rules.append({"id": str(item.get("id") or _flow_id(str(item), 0)), "name": str(item.get("scriptName") or ""), "enabled": item.get("disabled") is not True, "find_regex": str(item.get("findRegex") or ""), "replace_string": str(item.get("replaceString") or "")})
    return rules


def _selector_hints(flow: dict[str, Any], prompt_order: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selectors = []
    for entry in prompt_order:
        content = str(entry.get("content") or "")
        if "{{char}}" in content or "角色" in content:
            selectors.append({"type": "character_state", "source_prompt": entry.get("identifier"), "strategy": "mentioned_or_recent"})
        if "world" in content.lower() or "世界" in content or "地点" in content:
            selectors.append({"type": "world_event", "source_prompt": entry.get("identifier"), "strategy": "recent_facts"})
    if flow.get("controller_model"):
        selectors.append({"type": "controller_model", "strategy": "converted_to_declarative_selector", "source_prompt": "controller_model"})
    return selectors


def _flow_unsupported(flow: dict[str, Any]) -> list[str]:
    unsupported = []
    if flow.get("controller_model"):
        unsupported.append("controller_model_ejs_execution")
    if flow.get("worldbook") or flow.get("desired_entries") or flow.get("remove_entries"):
        unsupported.append("direct_worldbook_mutation")
    if any("<%" in str(entry.get("content") or "") for entry in flow.get("prompt_order") or [] if isinstance(entry, dict)):
        unsupported.append("inline_ejs_prompt_execution")
    return unsupported


def _global_unsupported(raw: dict[str, Any]) -> list[str]:
    unsupported = []
    text = str(raw)
    if "getwi" in text or "<%" in text:
        unsupported.append("ejs_runtime")
    if "worldbook" in text or "world_info" in text:
        unsupported.append("sillytavern_worldbook_runtime")
    return unsupported


def _detect_source(raw: dict[str, Any]) -> str:
    if "flows" in raw or "settings" in raw:
        return "st_evolution_settings"
    if "prompts" in raw or "prompt_order" in raw:
        return "sillytavern_preset"
    return "unknown_json"


def _flow_id(name: str, index: int) -> str:
    digest = sha1(f"{index}:{name}".encode("utf-8")).hexdigest()[:10]
    return f"imported_{digest}"


def _map_trigger(value: Any) -> str:
    raw = str(value or "after_reply").strip()
    if raw in {"before_reply", "before_context_build"}:
        return "before_context_build"
    if raw in {"manual", "manual_rebuild"}:
        return "manual_rebuild"
    return "after_commit"


def _role(value: Any) -> str:
    return str(value) if value in {"system", "user", "assistant"} else "system"


def _injection_position(value: Any) -> str:
    return str(value) if value in {"relative", "in_chat"} else "relative"


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
