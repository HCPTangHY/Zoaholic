"""
路由共享依赖项

提供认证、速率限制等共享功能
"""

from fastapi import Request

from core.auth import (
    rate_limit_dependency,
    verify_api_key,
    verify_admin_api_key,
)


def get_app():
    """获取 FastAPI 应用实例
    
    注意：当以 `python main.py` 方式运行时，main.py 作为 `__main__` 模块加载。
    直接 `from main import app` 会导致 main.py 被重新导入，创建新的未初始化 app。
    因此优先从 `__main__` 模块获取 app。
    """
    import sys
    # 优先从 __main__ 获取（python main.py 方式启动时）
    if '__main__' in sys.modules:
        main_module = sys.modules['__main__']
        if hasattr(main_module, 'app'):
            return main_module.app
    # 回退到直接导入（uvicorn main:app 方式启动时）
    from main import app
    return app


def get_model_handler():
    """获取模型请求处理器
    
    同 get_app()，优先从 __main__ 模块获取。
    """
    import sys
    if '__main__' in sys.modules:
        main_module = sys.modules['__main__']
        if hasattr(main_module, 'model_handler'):
            return main_module.model_handler
    from main import model_handler
    return model_handler




async def get_api_key(request: Request):
    """从请求中提取 API Key"""
    token = None
    if request.headers.get("x-api-key"):
        token = request.headers.get("x-api-key")
    elif request.headers.get("Authorization"):
        api_split_list = request.headers.get("Authorization").split(" ")
        if len(api_split_list) > 1:
            token = api_split_list[1]
    return token

