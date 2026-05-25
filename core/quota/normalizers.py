from .types import QuotaSnapshot, QuotaGauge


# 修改原因：现有余额查询和 OAuth quota 返回格式不同，不能直接供统一额度 UI 使用。
# 修改方式：把旧 BalanceResult 和 OAuthManager account dict 分别转换为 QuotaSnapshot。
# 目的：让 P0 阶段只追加新结构，不改动既有查询逻辑和旧字段语义。
def from_balance_result(bal: dict) -> QuotaSnapshot:
    """把旧 balance.py 的 dict 结果转为 QuotaSnapshot。"""
    snap = QuotaSnapshot(
        supported=bal.get('supported', True),
        status='error' if bal.get('error') else 'ok',
        error=bal.get('error'),
        value_type=bal.get('value_type', 'amount'),
        total=bal.get('total'),
        used=bal.get('used'),
        available=bal.get('available'),
        percent=bal.get('percent'),
        currency=bal.get('currency'),
        expires_at=bal.get('expires_at'),
        raw=bal.get('raw'),
        tier=bal.get('tier'),
        quota_inner=bal.get('quota_inner'),
        quota_outer=bal.get('quota_outer'),
    )
    # 修改原因：旧 BalanceResult 没有 gauges，但前端新额度组件需要至少一个主进度条。
    # 修改方式：当 percent 或 available 存在时自动生成 balance gauge；percent 缺失时用 available/total 补算。
    # 目的：普通渠道无需改 balance.py，即可开始输出统一额度弧线数据。
    if snap.percent is not None or snap.available is not None:
        if snap.value_type == 'percent':
            gauge_percent = snap.percent
            display_mode = 'percent'
        elif snap.value_type == 'amount':
            gauge_percent = snap.percent
            if gauge_percent is None and snap.total and snap.total > 0 and snap.available is not None:
                gauge_percent = snap.available / snap.total * 100
            display_mode = 'amount'
        else:  # quota
            gauge_percent = min(snap.available, 100) if snap.available is not None else None
            display_mode = 'quota'
        snap.gauges.append(QuotaGauge(
            id='balance', label='余额', role='primary',
            percent=gauge_percent,
            total=snap.total, available=snap.available, used=snap.used,
            unit=snap.currency,
            display_mode=display_mode,
        ))
    # 修改原因：tier/plan 标签应由渠道或插件自己的 quota_display 插槽决定，normalizer 自动生成 badge 会绕过插件开关条件。
    # 修改方式：继续保留 snap.tier 兼容字段，但不再把 tier 转换为 snap.badges。
    # 目的：避免通用前端再次通过固定标签组件展示硬编码标签，并让 oai_tier 等插件独立控制展示。
    return snap


def from_oauth_account(account: dict) -> QuotaSnapshot:
    """把 OAuthManager 的 account dict 转为 QuotaSnapshot。"""
    snap = QuotaSnapshot(
        supported=True,
        status='ok',
        raw=account.get('quota_raw') or account.get('raw'),
        quota_inner=account.get('quota_inner'),
        quota_outer=account.get('quota_outer'),
    )
    # 修改原因：OAuthManager 使用 quota_inner 表示短窗口剩余额度，不能只折叠成单一 percent。
    # 修改方式：将 quota_inner 映射为 short_window gauge，并保留原兼容字段。
    # 目的：新 UI 可以显示 OAuth 短窗口额度，旧 UI 仍可读取 quota_inner。
    qi = account.get('quota_inner')
    if qi is not None:
        snap.gauges.append(QuotaGauge(
            id='inner', label='inner', role='short_window',
            percent=qi,
        ))
    # 修改原因：OAuthManager 使用 quota_outer 表示长窗口剩余额度，需要与短窗口分开展示。
    # 修改方式：将 quota_outer 映射为 long_window gauge，并保留原兼容字段。
    # 目的：新 UI 可以同时展示 OAuth 长窗口额度，旧 UI 仍可读取 quota_outer。
    qo = account.get('quota_outer')
    if qo is not None:
        snap.gauges.append(QuotaGauge(
            id='outer', label='outer', role='long_window',
            percent=qo,
        ))
    # 修改原因：旧余额展示只有一个 percent，需要从 OAuth 多窗口额度中选取保守值。
    # 修改方式：取所有 gauge percent 的最小值作为兼容 percent。
    # 目的：保持 OAuth 余额卡片用最低剩余额度表达整体可用性。
    pcts = [g.percent for g in snap.gauges if g.percent is not None]
    if pcts:
        snap.percent = min(pcts)
    return snap
