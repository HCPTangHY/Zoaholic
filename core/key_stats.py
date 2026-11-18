"""渠道 API key 统计和智能排序工具。"""


# 迁移说明：
# 修改原因：该模块承载业务逻辑，不应继续放在 utils_pkg 这种通用工具包中。
# 修改方式：按照 Scout 的归位方案迁移到 core 对应业务模块，并只调整必要的内部导入路径。
# 目的：让业务代码按领域归属维护，同时保留根 utils.py 和 utils_pkg shim 的旧导入兼容性。
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import case, func, select

from db import DB_TYPE, DISABLE_DATABASE, ChannelStat, RequestStat, async_session_scope, d1_client
from core.log_config import logger


async def _query_channel_key_stats_d1(
    provider_name: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> List[Dict]:
    if d1_client is None:
        return []

    if not start_dt:
        start_dt = datetime.now(timezone.utc) - timedelta(hours=24)

    sql = (
        "SELECT provider_api_key, COUNT(*) AS total_requests, "
        "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count "
        "FROM channel_stats "
        "WHERE provider = ? AND timestamp >= ? AND provider_api_key IS NOT NULL"
    )
    params = [provider_name, start_dt]
    if end_dt:
        sql += " AND timestamp < ?"
        params.append(end_dt)
    sql += " GROUP BY provider_api_key"

    rows = await d1_client.query_all(sql, params)

    key_stats: List[Dict] = []
    for row in rows:
        total_requests = int(row.get("total_requests") or 0)
        success_count = int(row.get("success_count") or 0)
        key_stats.append(
            {
                "api_key": row.get("provider_api_key"),
                "success_count": success_count,
                "total_requests": total_requests,
                "success_rate": (success_count / total_requests) if total_requests > 0 else 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
            }
        )

    # 查询每个 Key 的 Token 用量（通过 request_id 关联 request_stats）
    token_sql = (
        "SELECT cs.provider_api_key, "
        "COALESCE(SUM(rs.prompt_tokens), 0) AS total_prompt_tokens, "
        "COALESCE(SUM(rs.completion_tokens), 0) AS total_completion_tokens, "
        "COALESCE(SUM(rs.total_tokens), 0) AS total_tokens "
        "FROM channel_stats cs "
        "LEFT JOIN request_stats rs ON cs.request_id = rs.request_id "
        "WHERE cs.provider = ? AND cs.timestamp >= ? "
        "AND cs.provider_api_key IS NOT NULL AND cs.success = 1"
    )
    token_params = [provider_name, start_dt]
    if end_dt:
        token_sql += " AND cs.timestamp < ?"
        token_params.append(end_dt)
    token_sql += " GROUP BY cs.provider_api_key"

    token_rows = await d1_client.query_all(token_sql, token_params)
    token_map: Dict[str, Dict] = {}
    for row in token_rows:
        token_map[row.get("provider_api_key")] = {
            "total_prompt_tokens": int(row.get("total_prompt_tokens") or 0),
            "total_completion_tokens": int(row.get("total_completion_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
        }

    for stat in key_stats:
        t = token_map.get(stat["api_key"])
        if t:
            stat.update(t)

    return key_stats


async def query_channel_key_stats(
    provider_name: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> List[Dict]:
    """Queries the ChannelStat table for API key success rates.
    
    provider_name 支持逗号分隔的多个 provider（用于聚合主渠道+子渠道的统计）。
    """
    if DISABLE_DATABASE:
        return []

    # 解析逗号分隔的多 provider
    provider_names = [p.strip() for p in provider_name.split(',') if p.strip()]

    if (DB_TYPE or "sqlite").lower() == "d1":
        # D1: 逐个查再合并
        all_stats: Dict[str, Dict] = {}
        for pn in provider_names:
            for item in await _query_channel_key_stats_d1(pn, start_dt=start_dt, end_dt=end_dt):
                key = item["api_key"]
                if key in all_stats:
                    existing = all_stats[key]
                    existing["total_requests"] += item["total_requests"]
                    existing["success_count"] += item["success_count"]
                    existing["total_prompt_tokens"] += item.get("total_prompt_tokens", 0)
                    existing["total_completion_tokens"] += item.get("total_completion_tokens", 0)
                    existing["total_tokens"] += item.get("total_tokens", 0)
                else:
                    all_stats[key] = {**item}
        for v in all_stats.values():
            v["success_rate"] = v["success_count"] / v["total_requests"] if v["total_requests"] > 0 else 0
        sorted_stats = sorted(
            all_stats.values(),
            key=lambda item: (item["success_rate"], item["total_requests"]),
            reverse=True,
        )
        return sorted_stats

    async with async_session_scope() as session:
        if not start_dt:
            start_dt = datetime.now(timezone.utc) - timedelta(hours=24)
        
        # 支持多 provider IN 查询
        if len(provider_names) == 1:
            provider_filter = ChannelStat.provider == provider_names[0]
        else:
            provider_filter = ChannelStat.provider.in_(provider_names)
        
        query = (
            select(
                ChannelStat.provider_api_key,
                func.count().label("total_requests"),
                func.sum(case((ChannelStat.success, 1), else_=0)).label("success_count"),
            )
            .where(provider_filter)
            .where(ChannelStat.timestamp >= start_dt)
            .where(ChannelStat.provider_api_key.isnot(None))
        )
        if end_dt:
            query = query.where(ChannelStat.timestamp < end_dt)
        query = query.group_by(ChannelStat.provider_api_key)
        result = await session.execute(query)
        stats_from_db = result.mappings().all()

        # 查询每个 Key 的 Token 用量（通过 request_id 关联 request_stats）
        token_query = (
            select(
                ChannelStat.provider_api_key,
                func.coalesce(func.sum(RequestStat.prompt_tokens), 0).label("total_prompt_tokens"),
                func.coalesce(func.sum(RequestStat.completion_tokens), 0).label("total_completion_tokens"),
                func.coalesce(func.sum(RequestStat.total_tokens), 0).label("total_tokens"),
            )
            .join(RequestStat, ChannelStat.request_id == RequestStat.request_id)
            .where(provider_filter)
            .where(ChannelStat.timestamp >= start_dt)
            .where(ChannelStat.provider_api_key.isnot(None))
            .where(ChannelStat.success == True)
        )
        if end_dt:
            token_query = token_query.where(ChannelStat.timestamp < end_dt)
        token_query = token_query.group_by(ChannelStat.provider_api_key)
        token_result = await session.execute(token_query)
        token_map = {
            row.provider_api_key: {
                "total_prompt_tokens": int(row.total_prompt_tokens or 0),
                "total_completion_tokens": int(row.total_completion_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
            }
            for row in token_result.fetchall()
        }

    key_stats = []
    for row in stats_from_db:
        api_key = row.provider_api_key
        t = token_map.get(api_key, {})
        key_stats.append(
            {
                "api_key": api_key,
                "success_count": row.success_count,
                "total_requests": row.total_requests,
                "success_rate": row.success_count / row.total_requests
                if row.total_requests > 0
                else 0,
                "total_prompt_tokens": t.get("total_prompt_tokens", 0),
                "total_completion_tokens": t.get("total_completion_tokens", 0),
                "total_tokens": t.get("total_tokens", 0),
            }
        )
    sorted_stats = sorted(
        key_stats,
        key=lambda item: (item["success_rate"], item["total_requests"]),
        reverse=True,
    )
    return sorted_stats


async def get_sorted_api_keys(
    provider_name: str, all_keys_in_config: list, group_size: int = 100
):
    """
    获取根据成功率和特定分组算法排序的API密钥列表。

    1. 从数据库查询过去72小时内各API key的成功和失败次数。
    2. 计算成功率，并对所有key（包括未使用的key）进行排序。
    3. 应用“矩阵转置”分组算法，以平衡负载和探索。
    """
    if not all_keys_in_config:
        return []

    from core.stats import mask_provider_api_key

    key_stats = {}
    try:
        start_time = datetime.now(timezone.utc) - timedelta(hours=72)
        stats_list = await query_channel_key_stats(provider_name, start_dt=start_time)
        for stat in stats_list:
            key_stats[stat["api_key"]] = {
                "success_rate": stat["success_rate"],
                "total_requests": stat["total_requests"],
                "success_count": stat.get("success_count", 0),
            }
    except Exception as e:
        logger.error(
            f"Error querying key stats from DB for provider '{provider_name}': {e}"
        )
        # 在数据库查询失败时，返回原始顺序，确保系统可用性
        return all_keys_in_config

    def _score_key(raw_key: str) -> tuple[float, int]:
        masked = mask_provider_api_key(raw_key)
        s_masked = key_stats.get(masked)
        s_raw = key_stats.get(raw_key) if masked != raw_key else None
        if s_masked and s_raw:
            tot = s_masked["total_requests"] + s_raw["total_requests"]
            succ = s_masked["success_count"] + s_raw["success_count"]
            return ((succ / tot) if tot > 0 else 0.0, tot)
        entry = s_masked or s_raw
        if entry:
            return (entry["success_rate"], entry["total_requests"])
        return (-1.0, 0)

    # 对所有在配置文件中定义的key进行排序
    # 排序规则：1. 成功率降序 2. 总尝试次数降序（成功率相同时，尝试多的更可信）
    # 对于从未用过的key，它们会自然排在最后
    sorted_keys = sorted(
        all_keys_in_config,
        key=_score_key,
        reverse=True,
    )

    # 应用“矩阵转置”分组算法
    num_keys = len(sorted_keys)
    if num_keys == 0:
        return []

    num_groups = (num_keys + group_size - 1) // group_size
    groups = [[] for _ in range(num_groups)]

    for i, key in enumerate(sorted_keys):
        groups[i % num_groups].append(key)

    final_sorted_list = []
    for group in groups:
        final_sorted_list.extend(group)

    logger.info(
        f"Successfully sorted {len(final_sorted_list)} keys for provider '{provider_name}' using smart algorithm."
    )
    return final_sorted_list
