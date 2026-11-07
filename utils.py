"""兼容旧导入路径的 utils 门面模块。

修改原因：业务实现已经从 utils_pkg 归位到 core 对应模块，旧代码仍大量使用 from utils import xxx。
修改方式：本文件只从 core.utils、core.config 和其他 core 业务模块重新导出旧名称，不再从 utils_pkg 导入实现。
目的：保持旧调用方无需修改，同时让业务逻辑由 core 模块承载。
"""

# core.utils 的既有公开工具继续从这里再导出，保持历史行为。
from core.utils import (
    safe_get,
    get_model_dict,
    is_local_api_key,
    ThreadSafeCircularList,
    provider_api_circular_list,
    parse_rate_limit,
    circular_list_encoder,
    ApiKeyRateLimitRegistry,
    _save_all_auto_disabled,
    load_auto_disabled_snapshot,
    restore_auto_disabled,
    resolve_base_url,
    BaseAPI,
    is_tools_disabled,
    get_engine,
    get_proxy,
    truncate_for_logging,
    end_of_line,
    generate_sse_response,
    generate_no_stream_response,
    generate_chunked_image_md,
    get_image_format,
    encode_image,
    get_image_from_url,
    get_encode_image,
    get_base64_image,
    _convert_webp_base64_to_png,
    _prepare_image_for_upload,
    parse_json_safely,
)
from core.json_utils import json_dumps_text, json_loads
from core.config.codec import _YamlHelper, _quote_colon_strings, yaml
from core.config.file_store import API_YAML_PATH, _BASE_DIR, save_api_yaml
from core.config.loader import load_config, yaml_error_message
from core.config.service import (
    _PROVIDER_STRIP_FIELDS,
    _expand_sub_channels,
    _strip_provider_fields,
    update_config,
)
from core.config.serialization import (
    _rebuild_api_with_labels,
    _sanitize_config_for_persistence,
    dump_config_to_json_obj,
    dump_config_to_yaml_text,
)
from core.config.db_store import load_config_from_db, save_config_to_db
from core.http_headers import _set_header_case_insensitive, apply_custom_headers, has_header_case_insensitive
from core.key_stats import _query_channel_key_stats_d1, get_sorted_api_keys, query_channel_key_stats
from core.model_catalog import (
    _append_authorized_virtual_models,
    _append_model_info_if_missing,
    get_all_models,
    post_all_models,
)
from core.rate_limit import InMemoryRateLimiter
from core.stream_pipeline import (
    SSE_KEEPALIVE_COMMENT,
    ensure_string,
    error_handling_wrapper,
    identify_audio_format,
    iter_sse_with_keepalive,
    wait_for_timeout,
)

# 修改原因：旧 utils.py 没有 __all__，但验证命令会执行 from utils import *。
# 修改方式：显式导出旧公开名称，并把少量历史上被路由直接导入的下划线辅助函数保留在模块属性中。
# 目的：让星号导入不泄露门面内部同步变量，同时不影响 from utils import _sanitize_config_for_persistence 等旧用法。
__all__ = [
    "API_YAML_PATH",
    "ApiKeyRateLimitRegistry",
    "BaseAPI",
    "InMemoryRateLimiter",
    "SSE_KEEPALIVE_COMMENT",
    "ThreadSafeCircularList",
    "apply_custom_headers",
    "circular_list_encoder",
    "dump_config_to_json_obj",
    "dump_config_to_yaml_text",
    "encode_image",
    "end_of_line",
    "ensure_string",
    "error_handling_wrapper",
    "generate_chunked_image_md",
    "generate_no_stream_response",
    "generate_sse_response",
    "get_all_models",
    "get_base64_image",
    "get_encode_image",
    "get_engine",
    "get_image_format",
    "get_image_from_url",
    "get_model_dict",
    "get_proxy",
    "get_sorted_api_keys",
    "has_header_case_insensitive",
    "identify_audio_format",
    "is_local_api_key",
    "is_tools_disabled",
    "iter_sse_with_keepalive",
    "json_dumps_text",
    "json_loads",
    "load_auto_disabled_snapshot",
    "load_config",
    "load_config_from_db",
    "parse_json_safely",
    "parse_rate_limit",
    "post_all_models",
    "provider_api_circular_list",
    "query_channel_key_stats",
    "resolve_base_url",
    "restore_auto_disabled",
    "safe_get",
    "save_api_yaml",
    "save_config_to_db",
    "truncate_for_logging",
    "update_config",
    "wait_for_timeout",
    "yaml",
    "yaml_error_message",
]
