"""Domain-like data shapes for the PlotPilot Evolution World plugin."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Union, Tuple


@dataclass
class ChapterFactSnapshot:
    novel_id: str
    chapter_number: int
    content_hash: str
    summary: str
    characters: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    world_events: list[str] = field(default_factory=list)
    at: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CharacterCard:
    character_id: str
    name: str
    first_seen_chapter: int
    last_seen_chapter: int
    aliases: list[str] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "active"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
