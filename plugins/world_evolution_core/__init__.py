"""PlotPilot adapter for Evolution World Assistant."""
from __future__ import annotations


def init_api(app) -> None:
    from plugins.platform.hook_dispatcher import register_hook

    from .routes import router
    from .service import EvolutionWorldAssistantService

    service = EvolutionWorldAssistantService()
    register_hook("world_evolution_core", "after_commit", service.after_commit)
    register_hook("world_evolution_core", "after_chapter_review", service.after_chapter_review)
    register_hook("world_evolution_core", "before_chapter_review", service.before_chapter_review)
    register_hook("world_evolution_core", "before_context_build", service.before_context_build)
    register_hook("world_evolution_core", "manual_rebuild", service.manual_rebuild)
    register_hook("world_evolution_core", "rollback", service.rollback)
    register_hook("world_evolution_core", "review_chapter", service.review_chapter)

    prefix = "/api/v1/plugins/evolution-world"
    if not any(getattr(route, "path", "").startswith(prefix) for route in app.routes):
        app.include_router(router)


def init_daemon() -> None:
    return None
