import pytest

from plugins.evolution_world_assistant.service import EvolutionWorldAssistantService
from plugins.platform.plugin_storage import PluginStorage
from plugins.platform.job_registry import PluginJobRegistry


@pytest.mark.asyncio
async def test_after_commit_writes_facts_and_context_block(tmp_path):
    storage = PluginStorage(root=tmp_path)
    service = EvolutionWorldAssistantService(storage=storage, jobs=PluginJobRegistry(storage))

    result = await service.after_commit(
        {
            "novel_id": "novel-1",
            "chapter_number": 1,
            "payload": {"content": "主角抵达雾城，并见到了失踪多年的导师。"},
        }
    )

    assert result["ok"] is True
    facts = storage.read_json(
        "evolution_world_assistant",
        ["novels", "novel-1", "facts", "chapter_1.json"],
    )
    assert facts["chapter_number"] == 1

    context = await service.before_context_build({"novel_id": "novel-1", "chapter_number": 2})
    assert context["ok"] is True
    assert "雾城" in context["context_blocks"][0]["content"]
