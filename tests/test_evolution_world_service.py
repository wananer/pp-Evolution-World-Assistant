import pytest

from plugins.evolution_world_assistant.service import EvolutionWorldAssistantService
from plugins.platform.job_registry import PluginJobRegistry
from plugins.platform.plugin_storage import PluginStorage


@pytest.mark.asyncio
async def test_after_commit_writes_facts_characters_and_context_block(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(storage=storage, jobs=PluginJobRegistry(storage))

    result = await service.after_commit(
        {
            "novel_id": "novel-1",
            "chapter_number": 1,
            "payload": {"content": "《林澈》抵达雾城，并见到了失踪多年的导师。导师发现城门外爆发袭击。"},
        }
    )

    assert result["ok"] is True
    facts = storage.read_json(
        "evolution_world_assistant",
        ["novels", "novel-1", "facts", "chapter_1.json"],
    )
    assert facts["chapter_number"] == 1
    assert "林澈" in facts["characters"]
    assert "雾城" in facts["locations"]

    characters = service.list_characters("novel-1")
    assert characters["items"][0]["name"] == "林澈"

    context = service.before_context_build({"novel_id": "novel-1", "chapter_number": 2})
    assert context["ok"] is True
    content = context["context_blocks"][0]["content"]
    assert "本章焦点角色" in content
    assert "林澈" in content
    assert "《林澈》" not in content
    assert "雾城" in content
    patch = context["context_patch"]
    assert patch["merge_strategy"] == "append_by_priority"
    assert patch["estimated_token_budget"] > 0
    assert [block["id"] for block in patch["blocks"]][:2] == ["focus_characters", "recent_facts"]
    assert patch["blocks"][0]["kind"] == "focus_character_state"


@pytest.mark.asyncio
async def test_manual_rebuild_replays_chapter_payloads(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(storage=storage, jobs=PluginJobRegistry(storage))

    result = await service.manual_rebuild(
        {
            "novel_id": "novel-2",
            "chapters": [
                {"number": 1, "content": "《沈月》进入黑塔，发现塔顶爆发异象。"},
                {"number": 2, "content": "沈月离开黑塔，来到星港。"},
            ],
        }
    )

    assert result["ok"] is True
    assert result["data"]["novel_id"] == "novel-2"
    assert result["data"]["rebuilt_chapters"] == [1, 2]
    assert result["data"]["characters_rebuilt"] == 1
    card = service.get_character("novel-2", "沈月")
    assert card is not None
    assert card["last_seen_chapter"] == 2
    timeline = service.list_character_timeline("novel-2", card["character_id"])
    assert len(timeline["items"]) == 2


@pytest.mark.asyncio
async def test_rollback_removes_snapshot_and_rebuilds_character_cards(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(storage=storage, jobs=PluginJobRegistry(storage))

    await service.manual_rebuild(
        {
            "novel_id": "novel-3",
            "chapters": [
                {"number": 1, "content": "《林澈》抵达雾城。顾衡交给林澈一枚钥匙。"},
                {"number": 2, "content": "林澈离开雾城，顾衡留在黑塔。"},
            ],
        }
    )

    before = service.list_snapshots("novel-3")
    assert [item["chapter_number"] for item in before["items"]] == [1, 2]

    result = await service.rollback({"novel_id": "novel-3", "chapter_number": 2})

    assert result["ok"] is True
    assert result["data"]["removed_snapshot"] is True
    after = service.list_snapshots("novel-3")
    assert [item["chapter_number"] for item in after["items"]] == [1]
    card = service.get_character("novel-3", "林澈")
    assert card is not None
    assert card["last_seen_chapter"] == 1
    runs = service.list_runs("novel-3")
    assert any(run["hook_name"] == "rollback" for run in runs["items"])


class FakeStructuredProvider:
    async def extract(self, request):
        assert request["schema"]["required"] == ["summary", "characters", "locations", "world_events"]
        return {
            "summary": "林澈在雾城获得钥匙。",
            "characters": [
                {"name": "林澈", "summary": "获得黑色钥匙", "locations": ["雾城"], "confidence": 0.92},
                {"name": "沈月", "summary": "追捕白鸦", "status": "active"},
            ],
            "locations": ["雾城", "黑塔"],
            "world_events": [
                {"summary": "林澈获得黑色钥匙", "event_type": "item", "characters": ["林澈"], "locations": ["黑塔"]}
            ],
        }


class FailingStructuredProvider:
    async def extract(self, request):
        raise RuntimeError("provider offline")


@pytest.mark.asyncio
async def test_structured_provider_overrides_deterministic_extraction(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(
        storage=storage,
        jobs=PluginJobRegistry(storage),
        extractor_provider=FakeStructuredProvider(),
    )

    result = await service.after_commit(
        {
            "novel_id": "novel-4",
            "chapter_number": 1,
            "payload": {"content": "这一章没有书名号，但结构化 provider 会返回人物。"},
        }
    )

    assert result["ok"] is True
    assert result["data"]["extraction"]["source"] == "structured"
    assert result["data"]["facts"]["characters"] == ["林澈", "沈月"]
    assert result["data"]["facts"]["locations"] == ["雾城", "黑塔"]
    runs = service.list_runs("novel-4")
    assert runs["items"][-1]["output"]["extraction_source"] == "structured"


@pytest.mark.asyncio
async def test_structured_provider_failure_falls_back_to_deterministic(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(
        storage=storage,
        jobs=PluginJobRegistry(storage),
        extractor_provider=FailingStructuredProvider(),
    )

    result = await service.after_commit(
        {
            "novel_id": "novel-5",
            "chapter_number": 1,
            "payload": {"content": "《顾衡》来到黑塔，发现雾城爆发异象。"},
        }
    )

    assert result["ok"] is True
    assert result["data"]["extraction"]["source"] == "deterministic"
    assert "顾衡" in result["data"]["facts"]["characters"]
    assert result["data"]["extraction"]["warnings"]


@pytest.mark.asyncio
async def test_context_patch_omits_future_chapter_facts(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(storage=storage, jobs=PluginJobRegistry(storage))

    await service.manual_rebuild(
        {
            "novel_id": "novel-6",
            "chapters": [
                {"number": 1, "content": "《林澈》抵达雾城。"},
                {"number": 2, "content": "林澈进入黑塔，发现星港信标。"},
            ],
        }
    )

    context = service.before_context_build({"novel_id": "novel-6", "chapter_number": 2})

    assert context["ok"] is True
    patch = context["context_patch"]
    recent_facts = next(block for block in patch["blocks"] if block["id"] == "recent_facts")
    assert [item["chapter_number"] for item in recent_facts["items"]] == [1]
    assert "第2章" not in recent_facts["content"]


def test_import_st_preset_converts_prompt_order_and_marks_unsupported(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(storage=storage, jobs=PluginJobRegistry(storage))

    result = service.import_st_preset(
        "novel-7",
        {
            "name": "ST Flow",
            "temperature": 0.8,
            "top_p": 0.9,
            "prompts": [
                {"identifier": "main", "name": "Main", "role": "system", "content": "提取角色与世界状态。"},
                {"identifier": "world", "name": "World", "role": "system", "content": "世界与地点：{{char}}"},
            ],
            "prompt_order": [{"order": [{"identifier": "world", "enabled": True}, {"identifier": "main", "enabled": False}]}],
            "controller_model": {"activate_entries": []},
            "extensions": {"SPreset": {"RegexBinding": {"regexes": [{"id": "r1", "scriptName": "clean", "findRegex": "foo", "replaceString": "bar"}]}}},
        },
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["source"] == "sillytavern_preset"
    assert data["flows"][0]["name"] == "ST Flow"
    assert data["flows"][0]["generation_options"]["temperature"] == 0.8
    assert [entry["identifier"] for entry in data["flows"][0]["prompt_order"]] == ["world", "main"]
    assert data["flows"][0]["prompt_order"][1]["enabled"] is False
    assert data["flows"][0]["regex_rules"][0]["find_regex"] == "foo"
    assert "controller_model_ejs_execution" in data["flows"][0]["unsupported"]
    saved = service.list_imported_flows("novel-7")
    assert saved["flows"][0]["name"] == "ST Flow"



@pytest.mark.asyncio
async def test_context_patch_filters_unmentioned_recent_characters_into_risks(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(storage=storage, jobs=PluginJobRegistry(storage))

    await service.manual_rebuild(
        {
            "novel_id": "novel-8",
            "chapters": [
                {"number": 1, "content": "《林澈》在雾城得到黑色钥匙。"},
                {"number": 2, "content": "《沈月》在星港追踪白鸦，发现银色罗盘。"},
                {"number": 3, "content": "《顾衡》留在城门，调查旧案卷宗。"},
            ],
        }
    )

    context = service.before_context_build(
        {
            "novel_id": "novel-8",
            "chapter_number": 4,
            "payload": {"outline": "林澈独自进入黑塔，用黑色钥匙打开密门。"},
        }
    )

    focus = next(block for block in context["context_patch"]["blocks"] if block["id"] == "focus_characters")
    assert [item["name"] for item in focus["items"]] == ["林澈"]
    risks = next(block for block in context["context_patch"]["blocks"] if block["id"] == "continuity_risks")
    assert "沈月" in risks["content"]
    assert "顾衡" in risks["content"]
    assert "不要强行安排出场" in risks["content"]



@pytest.mark.asyncio
async def test_context_patch_separates_background_constraints_from_focus(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(storage=storage, jobs=PluginJobRegistry(storage))

    await service.manual_rebuild(
        {
            "novel_id": "novel-9",
            "chapters": [
                {"number": 1, "content": "《林澈》在雾城得到黑色钥匙。"},
                {"number": 2, "content": "《沈月》在星港追踪白鸦，发现银色罗盘。"},
            ],
        }
    )

    context = service.before_context_build(
        {
            "novel_id": "novel-9",
            "chapter_number": 3,
            "payload": {"outline": "林澈进入星港，寻找白鸦留下的密门线索。"},
        }
    )

    focus = next(block for block in context["context_patch"]["blocks"] if block["id"] == "focus_characters")
    background = next(block for block in context["context_patch"]["blocks"] if block["id"] == "background_constraints")
    assert [item["name"] for item in focus["items"]] == ["林澈"]
    assert [item["name"] for item in background["items"]] == ["沈月"]
    assert "只作为连续性约束" in background["content"]
    assert "不要因此强制安排出场" in background["content"]
    assert "《沈月》" not in background["content"]
