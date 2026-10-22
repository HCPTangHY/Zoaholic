"""
API 路由模块

"""

from fastapi import APIRouter

from core.env import env_bool

# 创建主路由器
api_router = APIRouter()

# 导入并注册子路由
from routes.models import router as models_router
from routes.images import router as images_router
from routes.audio import router as audio_router
from routes.embeddings import router as embeddings_router
from routes.moderations import router as moderations_router
from routes.channels import router as channels_router
from routes.admin import router as admin_router
from routes.stats import router as stats_router
from routes.plugins import router as plugins_router
from routes.setup import router as setup_router
from routes.auth import router as auth_router
from routes.health import router as health_router
from routes.workspace import router as workspace_router
from routes.system import router as system_router

# 导入方言路由（自动注册所有方言端点）
from core.dialects import dialect_router

# 注册所有子路由
api_router.include_router(models_router, tags=["Models"])
api_router.include_router(images_router, tags=["Images"])
api_router.include_router(audio_router, tags=["Audio"])
api_router.include_router(embeddings_router, tags=["Embeddings"])
api_router.include_router(moderations_router, tags=["Moderations"])
api_router.include_router(channels_router, tags=["Channels"])
api_router.include_router(admin_router, tags=["Admin"])
api_router.include_router(stats_router, tags=["Stats"])
api_router.include_router(plugins_router, tags=["Plugins"])
api_router.include_router(setup_router, tags=["Setup"])
api_router.include_router(auth_router, tags=["Auth"])

# 注册方言路由（OpenAI / Gemini / Claude 等格式端点）
api_router.include_router(dialect_router)

# 健康检查端点：/healthz、/readyz 匿名但仅返回最小状态；/v1/health/details 需管理员凭证
api_router.include_router(health_router, tags=["Health"])
api_router.include_router(workspace_router, tags=["Workspace"])
api_router.include_router(system_router, tags=["System"])
# 修改原因：debug 诊断端点属于临时排查工具，不应常驻生产环境。
# 修改方式：默认不注册；ENABLE_DEBUG_ENDPOINTS=true 时才挂载，且路由内部仍强制 admin 鉴权 + 限频。
# 目的：排查时显式开启，平时不暴露进程内存细节。
if env_bool("ENABLE_DEBUG_ENDPOINTS", False):
    from routes.debug import router as debug_router

    api_router.include_router(debug_router, tags=["Debug"])

__all__ = ["api_router"]