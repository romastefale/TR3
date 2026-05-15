from __future__ import annotations

from app.moderation_tigrao.customize_router import router as customize_router
from app.moderation_tigrao.ddx_router import router as ddx_router
from app.moderation_tigrao.member_tag_router import router as member_tag_router
from app.moderation_tigrao.pinned_media_router import router as pinned_media_router
from app.moderation_tigrao.router import router

__all__ = ["router", "ddx_router", "customize_router", "member_tag_router", "pinned_media_router"]
