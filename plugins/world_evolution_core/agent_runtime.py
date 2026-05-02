"""Agent API runtime helpers for Evolution World.

This module owns the LLM-facing mechanics for the Agent API. The service layer
keeps hook orchestration and storage writes; runtime keeps model resolution,
connection tests, audited calls, and JSON parsing.
"""
from __future__ import annotations

import asyncio
import json
import threading
from time import perf_counter
from typing import Any, Callable, Optional
from urllib.parse import urlparse, urlunparse

from .agent_assets import build_reflection_record

try:
    from application.ai.llm_audit import audit_generate_call
except Exception:
    async def audit_generate_call(call: Callable[[], Any], **_metadata: Any) -> Any:
        result = call()
        if hasattr(result, "__await__"):
            return await result
        return result

try:
    from infrastructure.ai.prompt_resolver import resolve_prompt
except Exception:
    def resolve_prompt(_node_key: str, _variables: dict[str, Any], *, fallback_system: str = "", fallback_user: str = "") -> Any:
        class PromptResolutionFallback:
            system = fallback_system
            user = fallback_user

            def to_prompt(self) -> Any:
                return self

        return PromptResolutionFallback()


LLM_PROVIDER_MODES = {"same_as_main", "custom"}
LLM_MODEL_PROTOCOLS = {"openai", "anthropic", "gemini"}


class AgentRuntime:
    def __init__(
        self,
        *,
        settings_getter: Callable[[], dict[str, Any]],
        agent_llm_service: Optional[Any] = None,
        llm_provider_factory: Optional[Any] = None,
    ) -> None:
        self.settings_getter = settings_getter
        self.agent_llm_service = agent_llm_service
        self.llm_provider_factory = llm_provider_factory

    async def fetch_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = build_agent_models_request(payload, self.settings_getter())
        items = await fetch_model_list_items(request)
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "source": request["source"],
            "protocol": request["protocol"],
        }

    async def test_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = agent_runtime_settings_from_payload(payload, self.settings_getter())
        started = perf_counter()
        try:
            llm_service = self.resolve_llm_service(settings)
            result = await llm_service.generate(
                build_llm_connection_test_prompt(),
                make_generation_config(
                    model=str(settings.get("model") or ""),
                    max_tokens=32,
                    temperature=0.0,
                ),
            )
            return {
                "ok": True,
                "provider_mode": settings.get("provider_mode"),
                "protocol": (settings.get("custom_profile") or {}).get("protocol") if settings.get("provider_mode") == "custom" else None,
                "model": str(settings.get("model") or ""),
                "latency_ms": int((perf_counter() - started) * 1000),
                "preview": str(result.content or "").strip()[:120],
                "token_usage": token_usage_to_dict(getattr(result, "token_usage", None)),
            }
        except Exception as exc:
            return {
                "ok": False,
                "provider_mode": settings.get("provider_mode"),
                "protocol": (settings.get("custom_profile") or {}).get("protocol") if settings.get("provider_mode") == "custom" else None,
                "model": str(settings.get("model") or ""),
                "latency_ms": int((perf_counter() - started) * 1000),
                "error": str(exc),
            }

    def run_decision(self, phase: str, prompt_text: str, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings_getter().get("agent_api")
        if not isinstance(settings, dict) or not settings.get("enabled"):
            return {"ok": False, "error": "agent_api_disabled", "structured": {}, "phase": phase}
        novel_id = str(payload.get("novel_id") or "")
        chapter_number = _int_or_none(payload.get("chapter_number"))
        started_at = _now()
        try:
            llm_service = self.resolve_llm_service(settings)
            prompt = resolve_prompt(
                "plugin.world_evolution_core.agent-decision",
                {"prompt_text": prompt_text},
                fallback_system="你是 Evolution Agent Orchestrator。只输出 JSON，不写正文，不泄露密钥。",
                fallback_user=prompt_text,
            ).to_prompt()
            config = make_generation_config(
                model=str(settings.get("model") or ""),
                max_tokens=_clamp_int(settings.get("max_tokens"), 256, 4096, 1200),
                temperature=_clamp_float(settings.get("temperature"), 0.0, 2.0, 0.1),
            )
            result = run_async_blocking(
                audit_generate_call(
                    lambda: llm_service.generate(prompt, config),
                    prompt=prompt,
                    config=config,
                    metadata={
                        "novel_id": novel_id,
                        "chapter_number": chapter_number,
                        "phase": f"evolution_{phase}",
                        "source": "world_evolution_core.agent_orchestrator",
                    },
                )
            )
            content = str(result.content or "").strip()
            structured = parse_agent_json(content)
            if not structured:
                return {
                    "ok": False,
                    "phase": phase,
                    "started_at": started_at,
                    "at": _now(),
                    "model": str(settings.get("model") or ""),
                    "content": content[:1200],
                    "structured": {},
                    "token_usage": token_usage_to_dict(getattr(result, "token_usage", None)),
                    "error": "agent_decision_invalid_json",
                }
            return {
                "ok": True,
                "phase": phase,
                "started_at": started_at,
                "at": _now(),
                "model": str(settings.get("model") or ""),
                "content": content[:1200],
                "structured": structured,
                "token_usage": token_usage_to_dict(getattr(result, "token_usage", None)),
            }
        except Exception as exc:
            return {"ok": False, "phase": phase, "started_at": started_at, "at": _now(), "error": str(exc), "structured": {}}

    def build_reflection(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        capsules: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        started_at = _now()
        try:
            llm_service = self.resolve_llm_service(settings)
            prompt = build_agent_reflection_prompt(chapter_number=chapter_number, capsules=capsules, issues=issues)
            config = make_generation_config(
                model=str(settings.get("model") or ""),
                max_tokens=_clamp_int(settings.get("max_tokens"), 128, 2048, 800),
                temperature=_clamp_float(settings.get("temperature"), 0.0, 2.0, 0.1),
            )
            result = run_async_blocking(
                audit_generate_call(
                    lambda: llm_service.generate(prompt, config),
                    prompt=prompt,
                    config=config,
                    metadata={
                        "novel_id": novel_id,
                        "chapter_number": chapter_number,
                        "phase": "evolution_agent_reflection",
                        "source": "world_evolution_core.agent_runtime",
                    },
                )
            )
            content = str(result.content or "").strip()[:1200]
            finished_at = _now()
            structured = parse_agent_json(content)
            reflection = build_reflection_record(
                novel_id=novel_id,
                chapter_number=chapter_number,
                capsules=capsules,
                issues=issues,
                content=content,
                structured=structured,
                source="agent_api",
                model=str(settings.get("model") or ""),
                token_usage=token_usage_to_dict(getattr(result, "token_usage", None)),
                ok=True,
                at=finished_at,
            )
            return {
                "ok": True,
                "started_at": started_at,
                "at": finished_at,
                "chapter_number": chapter_number,
                "provider_mode": settings.get("provider_mode"),
                "model": str(settings.get("model") or ""),
                "capsule_ids": [str(item.get("id") or "") for item in capsules],
                "content": content,
                "structured": structured,
                "reflection": reflection,
                "token_usage": token_usage_to_dict(getattr(result, "token_usage", None)),
            }
        except Exception as exc:
            failed_at = _now()
            reflection = build_reflection_record(
                novel_id=novel_id,
                chapter_number=chapter_number,
                capsules=capsules,
                issues=issues,
                source="agent_api_fallback",
                ok=False,
                error=str(exc),
                at=failed_at,
            )
            return {"ok": False, "started_at": started_at, "at": failed_at, "error": str(exc), "reflection": reflection}

    def resolve_llm_service(self, settings: dict[str, Any]) -> Any:
        if self.agent_llm_service is not None:
            return self.agent_llm_service
        if self.llm_provider_factory is None:
            from infrastructure.ai.provider_factory import LLMProviderFactory

            self.llm_provider_factory = LLMProviderFactory()
        provider_mode = str(settings.get("provider_mode") or "same_as_main")
        if provider_mode == "custom":
            from application.ai.llm_control_service import LLMProfile

            profile_payload = settings.get("custom_profile") if isinstance(settings.get("custom_profile"), dict) else {}
            profile = LLMProfile(**custom_profile_for_llm(profile_payload))
            return self.llm_provider_factory.create_from_profile(profile)
        return self.llm_provider_factory.create_active_provider()


def build_agent_models_request(payload: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    return build_llm_models_request(payload, settings, key="agent_api")


def build_llm_models_request(payload: dict[str, Any], settings: dict[str, Any], *, key: str) -> dict[str, Any]:
    payload = payload or {}
    raw_settings = payload.get(key) if isinstance(payload.get(key), dict) else {}
    saved = settings.get(key) if isinstance(settings.get(key), dict) else {}
    merged = {**saved, **raw_settings}
    provider_mode = str(merged.get("provider_mode") or "same_as_main")
    if provider_mode not in LLM_PROVIDER_MODES:
        provider_mode = "same_as_main"
    custom = custom_profile_for_llm(merged.get("custom_profile") if isinstance(merged.get("custom_profile"), dict) else {})
    source = provider_mode
    protocol = custom.get("protocol") or "openai"
    if provider_mode != "custom":
        active_profile = (settings.get("active_profile") or {}) if isinstance(settings, dict) else {}
        protocol = str(active_profile.get("protocol") or "openai")
    return {
        "source": source,
        "provider_mode": provider_mode,
        "protocol": protocol if protocol in LLM_MODEL_PROTOCOLS else "openai",
        "base_url": custom.get("base_url") or "",
        "api_key": custom.get("api_key") or "",
        "extra_headers": custom.get("extra_headers") or {},
        "extra_query": custom.get("extra_query") or {},
        "timeout_ms": _clamp_int(payload.get("timeout_ms"), 1000, 180000, 60000),
    }


def agent_runtime_settings_from_payload(payload: dict[str, Any], saved_settings: dict[str, Any]) -> dict[str, Any]:
    payload_settings = payload.get("agent_api") if isinstance(payload.get("agent_api"), dict) else {}
    saved = saved_settings.get("agent_api") if isinstance(saved_settings.get("agent_api"), dict) else {}
    custom = custom_profile_for_llm({**(saved.get("custom_profile") or {}), **(payload_settings.get("custom_profile") or {})})
    settings = {**saved, **payload_settings, "custom_profile": custom}
    provider_mode = str(settings.get("provider_mode") or "same_as_main")
    settings["provider_mode"] = provider_mode if provider_mode in LLM_PROVIDER_MODES else "same_as_main"
    if settings["provider_mode"] == "custom":
        settings["model"] = custom.get("model") or ""
        settings["temperature"] = custom.get("temperature", settings.get("temperature"))
        settings["max_tokens"] = custom.get("max_tokens", settings.get("max_tokens"))
    return settings


async def fetch_model_list_items(request: dict[str, Any]) -> list[dict[str, str]]:
    import httpx

    protocol = str(request.get("protocol") or "openai")
    base_url = str(request.get("base_url") or "").strip()
    api_key = str(request.get("api_key") or "").strip()
    headers = dict(request.get("extra_headers") or {})
    params = dict(request.get("extra_query") or {})
    timeout = max(1.0, float(request.get("timeout_ms") or 60000) / 1000)

    if protocol == "gemini":
        url = f"{gemini_models_base(base_url)}/models"
        if api_key:
            params.setdefault("key", api_key)
    else:
        url = f"{openai_models_base(base_url)}/models"
        if api_key:
            headers = {"Authorization": f"Bearer {api_key}", **headers}

    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        body = (exc.response.text or "")[:400].replace("\n", " ")
        raise RuntimeError(f"上游模型列表 HTTP {exc.response.status_code}：{body or exc.response.reason_phrase}（请求 {url}）") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"连接上游失败：{exc}（请求 {url}）") from exc
    except ValueError as exc:
        raise RuntimeError(f"上游未返回 JSON（请求 {url}）") from exc

    return normalize_model_list_items(data, protocol)


def normalize_model_list_items(data: dict[str, Any], protocol: str) -> list[dict[str, str]]:
    if protocol == "gemini":
        raw_items = data.get("models", [])
    else:
        raw_items = data.get("data", [])
    if not isinstance(raw_items, list):
        return []

    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id") or entry.get("name") or entry.get("model")
        model_id = str(raw_id or "").strip()
        if protocol == "gemini" and model_id.startswith("models/"):
            model_id = model_id.removeprefix("models/")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        items.append(
            {
                "id": model_id,
                "name": str(entry.get("displayName") or entry.get("name") or model_id).removeprefix("models/"),
                "owned_by": str(entry.get("owned_by") or entry.get("ownedBy") or entry.get("publisher") or ""),
            }
        )
    return items


def openai_models_base(base_url: str) -> str:
    raw = (base_url or "").strip() or "https://api.openai.com/v1"
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/")
    if not path:
        path = "/v1"
    else:
        path = "/" + path.lstrip("/")
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", "")).rstrip("/")


def gemini_models_base(base_url: str) -> str:
    raw = (base_url or "").strip().rstrip("/") or "https://generativelanguage.googleapis.com/v1beta"
    for suffix in ("/models", "/v1beta/models", "/v1/models"):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)].rstrip("/")
            break
    return raw


def build_llm_connection_test_prompt() -> Any:
    return resolve_prompt(
        "plugin.world_evolution_core.connection-test",
        {},
        fallback_system="你是 API 连接测试器。",
        fallback_user="请只回复 OK 两个字母，不要添加任何解释。",
    ).to_prompt()


def build_agent_reflection_prompt(*, chapter_number: int, capsules: list[dict[str, Any]], issues: list[dict[str, Any]]) -> Any:
    system = (
        "你是 Evolution 智能体的反思器，不写小说正文。"
        "你只总结本轮审查暴露出的可复用经验，帮助后续章节减少连续性和人物逻辑错误。"
        "必须输出 JSON 对象，不要输出 Markdown。"
    )
    capsule_lines = "\n".join(
        f"- {item.get('title') or item.get('id')}：{item.get('guidance') or item.get('summary')}"
        for item in capsules[:6]
    )
    issue_lines = "\n".join(
        f"- [{item.get('severity')}] {item.get('issue_type')}：{item.get('description')}｜建议：{item.get('suggestion')}"
        for item in issues[:8]
    )
    user = f"""【章节】
第{chapter_number}章

【本轮固化 Capsule】
{capsule_lines or '无'}

【审查问题】
{issue_lines or '无'}

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
4. 优先处理章节承接、人物路线、认知边界、能力边界、性格调色盘。"""
    return resolve_prompt(
        "plugin.world_evolution_core.agent-reflection",
        {
            "chapter_number": chapter_number,
            "capsule_lines": capsule_lines or "无",
            "issue_lines": issue_lines or "无",
        },
        fallback_system=system,
        fallback_user=user,
    ).to_prompt()


def parse_agent_json(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def custom_profile_for_llm(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("id") or "evolution-agent-custom").strip() or "evolution-agent-custom",
        "name": str(raw.get("name") or "Evolution Agent API").strip() or "Evolution Agent API",
        "preset_key": str(raw.get("preset_key") or "custom-openai-compatible").strip() or "custom-openai-compatible",
        "protocol": str(raw.get("protocol") or "openai").strip() if str(raw.get("protocol") or "openai").strip() in LLM_MODEL_PROTOCOLS else "openai",
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key": str(raw.get("api_key") or "").strip(),
        "model": str(raw.get("model") or "").strip(),
        "temperature": _clamp_float(raw.get("temperature"), 0.0, 2.0, 0.1),
        "max_tokens": _clamp_int(raw.get("max_tokens"), 128, 4096, 1200),
        "timeout_seconds": _clamp_int(raw.get("timeout_seconds"), 10, 900, 180),
        "extra_headers": raw.get("extra_headers") if isinstance(raw.get("extra_headers"), dict) else {},
        "extra_query": raw.get("extra_query") if isinstance(raw.get("extra_query"), dict) else {},
        "extra_body": raw.get("extra_body") if isinstance(raw.get("extra_body"), dict) else {},
        "notes": str(raw.get("notes") or "Evolution Agent API settings."),
        "use_legacy_chat_completions": bool(raw.get("use_legacy_chat_completions")),
    }


def make_text_prompt(*, system: str, user: str) -> Any:
    try:
        from domain.ai.value_objects.prompt import Prompt

        return Prompt(system=system, user=user)
    except Exception:
        class PromptFallback:
            def __init__(self, system: str, user: str) -> None:
                self.system = system
                self.user = user

        return PromptFallback(system=system, user=user)


def make_generation_config(*, model: str, max_tokens: int, temperature: float) -> Any:
    try:
        from domain.ai.services.llm_service import GenerationConfig

        return GenerationConfig(model=model, max_tokens=max_tokens, temperature=temperature)
    except Exception:
        class GenerationConfigFallback:
            def __init__(self, model: str, max_tokens: int, temperature: float) -> None:
                self.model = model
                self.max_tokens = max_tokens
                self.temperature = temperature

        return GenerationConfigFallback(model=model, max_tokens=max_tokens, temperature=temperature)


def token_usage_to_dict(token_usage: Any) -> dict[str, int]:
    if token_usage is None:
        return {}
    if hasattr(token_usage, "to_dict"):
        data = token_usage.to_dict()
        return data if isinstance(data, dict) else {}
    return {
        "input_tokens": int(getattr(token_usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(token_usage, "output_tokens", 0) or 0),
        "total_tokens": int(getattr(token_usage, "total_tokens", 0) or 0),
    }


def run_async_blocking(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["result"] = asyncio.run(awaitable)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            box["error"] = exc

    thread = threading.Thread(target=runner, name="evolution-agent-runtime", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
