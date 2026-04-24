"""Shared platform API for plugin runtime status and hook introspection."""
from __future__ import annotations

from fastapi import APIRouter

from .hook_dispatcher import list_hooks
from .plugin_storage import PluginStorage

router = APIRouter(prefix="/api/v1/plugins/platform", tags=["plugins:platform"])


@router.get("/status")
async def get_platform_status():
    storage = PluginStorage()
    return {
        "ok": True,
        "runtime_api_version": "0.2",
        "features": {
            "manifest_capabilities": True,
            "frontend_lifecycle": True,
            "frontend_styles": True,
            "hook_dispatcher": True,
            "plugin_storage": True,
            "job_registry": True,
            "host_facade": True,
        },
        "storage_root": str(storage.root),
    }


@router.get("/hooks")
async def get_platform_hooks():
    return {"items": list_hooks()}
