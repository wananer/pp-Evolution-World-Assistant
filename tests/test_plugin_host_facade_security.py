import sqlite3

import pytest

from plugins.platform.host_database import ReadOnlyHostDatabase
from plugins.platform.host_facade import PlotPilotPluginHost
from plugins.platform.plugin_storage import PluginStorage


def test_plugin_host_defaults_to_bounded_host_database_reads(tmp_path):
    db_path = tmp_path / "host.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE chapters (novel_id TEXT, chapter_number INTEGER, content TEXT)")
    conn.execute("INSERT INTO chapters VALUES ('novel-1', 1, '第一章')")
    conn.commit()
    conn.close()

    host = PlotPilotPluginHost(
        plugin_name="world_evolution_core",
        storage=PluginStorage(root=tmp_path / "plugin_platform"),
        host_database=ReadOnlyHostDatabase(db_path),
    )

    with pytest.raises(PermissionError):
        host.read_host_row("SELECT content FROM chapters WHERE novel_id = ?", ("novel-1",))

    assert host.read_host_table_row("chapters", columns=["content"], novel_id="novel-1") == {"content": "第一章"}
    assert host.read_host_table("chapters", columns=["content"], limit=1000) == [{"content": "第一章"}]

    with pytest.raises(ValueError):
        host.read_host_table("chapters; DROP TABLE chapters", columns=["content"])
    with pytest.raises(ValueError):
        host.read_host_table("chapters", columns=["content FROM chapters; DROP TABLE chapters"])


def test_plugin_host_scopes_storage_to_own_plugin_by_default(tmp_path):
    host = PlotPilotPluginHost(
        plugin_name="world_evolution_core",
        storage=PluginStorage(root=tmp_path / "plugin_platform"),
    )

    host.write_own_plugin_state(["novels", "novel-1", "state.json"], {"ok": True})

    assert host.read_own_plugin_state(["novels", "novel-1", "state.json"]) == {"ok": True}
    assert host.read_plugin_state("world_evolution_core", ["novels", "novel-1", "state.json"]) == {"ok": True}
    with pytest.raises(PermissionError):
        host.write_plugin_state("other_plugin", ["state.json"], {"ok": False})


def test_plugin_host_keeps_explicit_raw_sql_escape_hatch(tmp_path):
    db_path = tmp_path / "host.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE chapters (novel_id TEXT, content TEXT)")
    conn.execute("INSERT INTO chapters VALUES ('novel-1', '第一章')")
    conn.commit()
    conn.close()

    host = PlotPilotPluginHost(
        storage=PluginStorage(root=tmp_path / "plugin_platform"),
        host_database=ReadOnlyHostDatabase(db_path),
        allow_raw_host_sql=True,
    )

    assert host.read_host_row("SELECT content FROM chapters WHERE novel_id = ?", ("novel-1",)) == {"content": "第一章"}
    with pytest.raises(PermissionError):
        host.read_host_rows("DELETE FROM chapters")
