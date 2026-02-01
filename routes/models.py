"""
Models 路由
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from utils import post_all_models
from routes.deps import rate_limit_dependency, verify_api_key, get_app

router = APIRouter()


@router.get("/v1/models", dependencies=[Depends(rate_limit_dependency)])
async def list_models(api_index: int = Depends(verify_api_key)):
    """列出可用模型。

    返回当前 API Key 可访问的所有模型列表。

    兼容：
    - 管理控制台使用 admin JWT 访问时（Authorization: Bearer <jwt>），
      verify_api_key 会将其映射为配置中的 admin api_key index，从而也能正常拿到模型列表。
    """
    app = get_app()

    # ensure_config 中间件只在非 /v1 路径触发，这里兜底初始化 models_list
    if not hasattr(app.state, "models_list") or app.state.models_list is None:
        app.state.models_list = {}

    models = post_all_models(api_index, app.state.config, app.state.api_list, app.state.models_list)
    return JSONResponse(content={
        "object": "list",
        "data": models,
    })