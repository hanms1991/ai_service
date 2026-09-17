"""GET /agent/capabilities 能力查询端点。

设计文档 7.7 节：返回注册表表现 + 场景码清单，数据源即能力注册表 + scenes.yaml。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import require_api_key
from api.schemas import CapabilitiesResponse, CapabilityScene
from api.services.scene_resolver import get_scene_resolver
from agents.capability_registry import registry_as_jsonable

router = APIRouter(prefix="/agent", tags=["capabilities"])


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(
    _api_key: str = Depends(require_api_key),
) -> CapabilitiesResponse:
    """返回当前能力注册表（agents + skills）与场景码清单。"""
    registry = registry_as_jsonable()
    # 去掉 _orphan_skills 等内部字段
    agents = {k: v for k, v in registry.items() if k in ("agents",)}

    resolver = get_scene_resolver()
    scenes = [
        CapabilityScene(
            scene=b.scene,
            description=b.description,
            response_format=b.response_format,
            timeout_seconds=b.timeout_seconds,
        )
        for b in resolver.list_scenes()
    ]

    return CapabilitiesResponse(agents=agents.get("agents", {}), scenes=scenes)
