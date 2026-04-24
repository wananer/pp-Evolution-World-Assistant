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

    context = await service.before_context_build({"novel_id": "novel-1", "chapter_number": 2})
    assert context["ok"] is True
    content = context["context_blocks"][0]["content"]
    assert "动态角色状态" in content
    assert "雾城" in content


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
