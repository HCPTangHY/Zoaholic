import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';
import {
  X, RefreshCw, Activity, BarChart3, AlertCircle,
  ChevronDown, ChevronUp, Globe, Box
} from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Legend
} from 'recharts';
import { apiFetch } from '../lib/api';

// 修改原因：Key Analytics 不再作为独立页面，而是从 Admin 页 Key 列表呼出侧滑面板。
// 修改方式：沿用 ChannelAnalyticsSheet 的 Dialog 结构、视觉样式、卡片、折线图和表格布局，只替换为 Key 维度的数据源。
// 目的：让 Key Analytics 与 Channel Analytics 保持一致的交互模式，并避免维护额外页面入口。

// ========== Types ==========

interface KeyAnalyticsSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  apiKeyValue: string;
  apiKeyName?: string;
}

interface SummaryData {
  key_hash: string;
  api_key_prefix: string;
  api_key_name?: string | null;
  api_key_group?: string | null;
  total_requests: number;
  success_count: number;
  success_rate: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_cost: number;
  unique_ips: number;
  unique_models: number;
  last_used?: string | null;
}

interface SummaryResponse {
  data?: SummaryData[];
}

interface IpQuotaSummary {
  remaining_ratio: number;
  exhausted: boolean;
  rule_count: number;
  bucket_count: number;
  label?: string;
  measure?: string;
  current?: number;
  limit?: number;
  remaining?: number;
}

interface QuotaLimitStatus {
  measure: string;
  aggregate: string;
  current: number;
  limit: number;
  remaining: number;
  remaining_ratio: number;
  period: number | string;
  window: string;
  label: string;
  reset_at?: number;
}

interface QuotaBucketStatus {
  dimensions: Record<string, string>;
  current: number;
  limit: number;
  remaining: number;
  remaining_ratio: number;
  limits: QuotaLimitStatus[];
}

interface QuotaRuleStatus {
  id: string;
  group_by: string[];
  where: Record<string, string>;
  measure: string;
  aggregate: string;
  label?: string;
  buckets: QuotaBucketStatus[];
}

interface IpEntry {
  ip?: string | null;
  request_count: number;
  last_used?: string | null;
  blocked?: boolean;
  quota_summary?: IpQuotaSummary | null;
}

interface ModelEntry {
  model?: string | null;
  request_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens?: number;
  cost: number;
}

interface IpTrendEntry {
  timestamp: string;
  request_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost: number;
}

interface IpDetail {
  ip: string;
  request_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost: number;
  last_used?: string | null;
  model_distribution: ModelEntry[];
  trend: IpTrendEntry[];
  quota_rules: QuotaRuleStatus[];
}

interface TrendEntry {
  timestamp?: string;
  time_bucket?: string;
  model?: string;
  request_count?: number;
  total_requests?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  tokens?: number;
}

interface TrendPoint {
  timestamp: string;
  requests: number;
  tokens: number;
}

interface ErrorEntry {
  timestamp?: string | null;
  model?: string | null;
  status_code?: number | null;
  provider?: string | null;
}

interface DetailResponse {
  ip_distribution?: IpEntry[];
  model_distribution?: ModelEntry[];
  model_trend?: TrendEntry[];
  recent_errors?: ErrorEntry[];
}

// ========== Constants ==========

const TIME_RANGES = [
  { label: '24h', value: 24 },
  { label: '7d', value: 168 },
  { label: '30d', value: 720 },
];

const LINE_COLORS = ['#3b82f6', '#22c55e'];
const AXIS_COLOR = 'hsl(var(--muted-foreground))';

// ========== Helpers ==========

const toNumber = (value: unknown) => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
};

const formatTokens = (n: number) => {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toString();
};

const formatCost = (n: number) => {
  if (n === 0) return '$0.00';
  if (n >= 1) return `$${n.toFixed(2)}`;
  if (n >= 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(6)}`;
};

const parseDate = (value: string) => {
  const normalized = value.includes('T') ? value : value.replace(' ', 'T');
  const hasZone = /([zZ]|[+-]\d{2}:?\d{2})$/.test(normalized);
  return new Date(hasZone ? normalized : `${normalized}Z`);
};

const formatDateTime = (value?: string | null) => {
  if (!value) return '—';
  const parsed = parseDate(String(value));
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
};

const formatTrendTick = (value: string, timeRange: number) => {
  const parsed = parseDate(String(value));
  if (Number.isNaN(parsed.getTime())) return String(value);
  if (timeRange > 48) return `${parsed.getMonth() + 1}/${parsed.getDate()}`;
  return `${String(parsed.getHours()).padStart(2, '0')}:${String(parsed.getMinutes()).padStart(2, '0')}`;
};

const formatSuccessRate = (value: number | null) => {
  if (value === null) return '—';
  const normalized = value <= 1 ? value * 100 : value;
  return `${normalized.toFixed(1)}%`;
};

const getSuccessRateColor = (value: number | null) => {
  if (value === null) return 'text-muted-foreground';
  const normalized = value <= 1 ? value * 100 : value;
  if (normalized >= 95) return 'text-emerald-600 dark:text-emerald-500';
  if (normalized >= 80) return 'text-amber-600 dark:text-amber-500';
  return 'text-red-600 dark:text-red-500';
};

const QUOTA_MEASURE_LABELS: Record<string, string> = {
  request: '请求次数',
  cost: '金额',
  token: '总 Token',
  token_in: '输入 Token',
  token_out: '输出 Token',
  ip: '不同 IP 数',
};

const formatQuotaValue = (measure: string, value: number) => (
  measure === 'cost' ? formatCost(value) : formatTokens(value)
);

const formatQuotaReset = (timestamp?: number) => {
  if (!timestamp) return '';
  const date = new Date(timestamp * 1000);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
};

const getModelTokens = (entry: ModelEntry) => {
  const explicitTotal = toNumber(entry.total_tokens);
  if (explicitTotal > 0) return explicitTotal;
  return toNumber(entry.prompt_tokens) + toNumber(entry.completion_tokens);
};

const normalizeIpTrendData = (rows: IpTrendEntry[]): TrendPoint[] => rows.map(row => ({
  timestamp: row.timestamp,
  requests: toNumber(row.request_count),
  tokens: toNumber(row.prompt_tokens) + toNumber(row.completion_tokens),
}));

const normalizeTrendData = (rows: TrendEntry[]) => {
  // 修改原因：详情接口当前按时间和模型返回趋势，后续也可能直接返回包含 Token 的时间桶。
  // 修改方式：前端按 timestamp 聚合请求量，并兼容 total_tokens、tokens、prompt_tokens 和 completion_tokens 字段。
  // 目的：让折线图稳定展示“请求量 + Token”双线，同时兼容不同后端版本的数据形状。
  const bucketMap = new Map<string, TrendPoint>();

  for (const row of rows) {
    const timestamp = row.timestamp || row.time_bucket;
    if (!timestamp) continue;

    const existing = bucketMap.get(timestamp) || { timestamp, requests: 0, tokens: 0 };
    const requests = toNumber(row.request_count ?? row.total_requests);
    const tokens = toNumber(row.total_tokens ?? row.tokens) || toNumber(row.prompt_tokens) + toNumber(row.completion_tokens);

    existing.requests += requests;
    existing.tokens += tokens;
    bucketMap.set(timestamp, existing);
  }

  return Array.from(bucketMap.values()).sort((a, b) => a.timestamp.localeCompare(b.timestamp));
};

async function keyHash(apiKey: string): Promise<string> {
  // 修改原因：后端详情接口只接收 key_hash，不能把完整 API Key 作为查询参数传给后端。
  // 修改方式：按需求使用浏览器 SubtleCrypto 计算 SHA-256，并截取前 16 位十六进制字符串。
  // 目的：让 Admin 页能够从本地 Key 值定位分析目标，同时减少 URL 中的敏感信息。
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(apiKey));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
}

// ========== Component ==========

export function KeyAnalyticsSheet({ open, onOpenChange, apiKeyValue, apiKeyName }: KeyAnalyticsSheetProps) {
  const [timeRange, setTimeRange] = useState(24);
  const [loading, setLoading] = useState(false);

  // Data
  const [summary, setSummary] = useState<SummaryData | null>(null);
  const [ipData, setIpData] = useState<IpEntry[]>([]);
  const [modelData, setModelData] = useState<ModelEntry[]>([]);
  const [trendData, setTrendData] = useState<TrendPoint[]>([]);
  const [errorData, setErrorData] = useState<ErrorEntry[]>([]);
  const [errorsExpanded, setErrorsExpanded] = useState(false);
  const [expandedIp, setExpandedIp] = useState<string | null>(null);
  const [ipDetails, setIpDetails] = useState<Record<string, IpDetail>>({});
  const [ipDetailLoading, setIpDetailLoading] = useState<string | null>(null);
  const [ipDetailErrors, setIpDetailErrors] = useState<Record<string, string>>({});

  const displayName = apiKeyName || (apiKeyValue ? `${apiKeyValue.slice(0, 8)}...${apiKeyValue.slice(-4)}` : '—');

  const fetchAll = useCallback(async () => {
    if (!apiKeyValue) return;

    // 修改原因：切换 Key 或时间范围时，旧数据会短暂残留在侧滑面板中。
    // 修改方式：每次请求前先清空组件状态，再并发拉取 summary 和 detail 两个接口。
    // 目的：避免用户看到上一个 Key 的分析结果，同时保留 ChannelAnalyticsSheet 的刷新体验。
    setLoading(true);
    setSummary(null);
    setIpData([]);
    setModelData([]);
    setTrendData([]);
    setErrorData([]);
    setErrorsExpanded(false);
    setExpandedIp(null);
    setIpDetails({});
    setIpDetailLoading(null);
    setIpDetailErrors({});

    try {
      const hash = await keyHash(apiKeyValue);
      const granularity = timeRange > 48 ? 'day' : 'hour';

      const [summaryRes, detailRes] = await Promise.all([
        apiFetch(`/v1/stats/key_analytics/summary?hours=${timeRange}&limit=500`),
        apiFetch(`/v1/stats/key_analytics/${hash}?hours=${timeRange}&granularity=${granularity}`),
      ]);

      if (summaryRes.ok) {
        const result = await summaryRes.json() as SummaryResponse;
        const matched = (result.data || []).find(item => item.key_hash === hash) || null;
        setSummary(matched);
      }

      if (detailRes.ok) {
        const detail = await detailRes.json() as DetailResponse;
        const ips = [...(detail.ip_distribution || [])].sort((a, b) => toNumber(b.request_count) - toNumber(a.request_count));
        const models = [...(detail.model_distribution || [])].sort((a, b) => toNumber(b.request_count) - toNumber(a.request_count));

        setIpData(ips);
        setModelData(models);
        setTrendData(normalizeTrendData(detail.model_trend || []));
        setErrorData(detail.recent_errors || []);
      }
    } catch (err) {
      console.error('Failed to fetch key analytics:', err);
    } finally {
      setLoading(false);
    }
  }, [apiKeyValue, timeRange]);

  const toggleIpDetail = useCallback(async (ip?: string | null) => {
    if (!ip) return;
    if (expandedIp === ip) {
      setExpandedIp(null);
      return;
    }

    setExpandedIp(ip);
    if (ipDetails[ip] || ipDetailLoading === ip) return;

    setIpDetailLoading(ip);
    setIpDetailErrors(prev => {
      const next = { ...prev };
      delete next[ip];
      return next;
    });
    try {
      const hash = await keyHash(apiKeyValue);
      const granularity = timeRange > 48 ? 'day' : 'hour';
      const params = new URLSearchParams({
        ip,
        hours: String(timeRange),
        granularity,
      });
      const response = await apiFetch(`/v1/stats/key_analytics/${hash}/ip?${params.toString()}`);
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.detail || `HTTP ${response.status}`);
      }
      const detail = await response.json() as IpDetail;
      setIpDetails(prev => ({ ...prev, [ip]: detail }));
    } catch (error) {
      setIpDetailErrors(prev => ({
        ...prev,
        [ip]: error instanceof Error ? error.message : '加载失败',
      }));
    } finally {
      setIpDetailLoading(current => current === ip ? null : current);
    }
  }, [apiKeyValue, expandedIp, ipDetailLoading, ipDetails, timeRange]);

  useEffect(() => {
    if (open && apiKeyValue) {
      fetchAll();
    }
  }, [open, apiKeyValue, timeRange, fetchAll]);

  const totalRequestsFromDetail = useMemo(
    () => modelData.reduce((sum, entry) => sum + toNumber(entry.request_count), 0),
    [modelData],
  );
  const totalTokensFromDetail = useMemo(
    () => modelData.reduce((sum, entry) => sum + getModelTokens(entry), 0),
    [modelData],
  );
  const totalCostFromDetail = useMemo(
    () => modelData.reduce((sum, entry) => sum + toNumber(entry.cost), 0),
    [modelData],
  );

  const totalRequests = summary?.total_requests ?? totalRequestsFromDetail;
  const successRate = summary?.success_rate ?? null;
  const totalTokens = summary
    ? toNumber(summary.total_prompt_tokens) + toNumber(summary.total_completion_tokens)
    : totalTokensFromDetail;
  const totalCost = summary?.total_cost ?? totalCostFromDetail;

  const tooltipStyle = {
    backgroundColor: 'hsl(var(--popover))',
    borderColor: 'hsl(var(--border))',
    color: 'hsl(var(--popover-foreground))',
    borderRadius: '8px',
  };

  const hasAnyData = totalRequests > 0 || ipData.length > 0 || modelData.length > 0 || trendData.length > 0 || errorData.length > 0;

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40 animate-in fade-in duration-200" />
        <Dialog.Content className="fixed right-0 top-0 h-full w-full md:w-[720px] max-w-full bg-background border-l border-border shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300">
          {/* Header */}
          <div className="p-4 sm:p-5 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
            <Dialog.Title className="text-lg sm:text-xl font-bold text-foreground flex items-center gap-2">
              <BarChart3 className="w-5 h-5 text-primary" />
              Key 分析: {displayName}
            </Dialog.Title>
            <Dialog.Close className="text-muted-foreground hover:text-foreground">
              <X className="w-5 h-5" />
            </Dialog.Close>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto p-4 sm:p-5 space-y-5">
            {/* Time Range */}
            <div className="flex items-center gap-2">
              <div className="flex items-center bg-card border border-border rounded-lg p-1 flex-1">
                {TIME_RANGES.map(r => (
                  <button
                    key={r.value}
                    onClick={() => setTimeRange(r.value)}
                    className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all flex-1 ${
                      timeRange === r.value
                        ? 'bg-primary text-primary-foreground shadow-sm'
                        : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'
                    }`}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
              <button
                onClick={fetchAll}
                className="p-2 text-muted-foreground hover:text-foreground bg-card border border-border rounded-lg transition-colors"
              >
                <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
              </button>
            </div>

            {/* Overview Cards */}
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-card border border-border rounded-xl p-4">
                <p className="text-xs text-muted-foreground">请求总量</p>
                <p className="text-xl font-bold text-foreground mt-1">{totalRequests.toLocaleString()}</p>
              </div>
              <div className="bg-card border border-border rounded-xl p-4">
                <p className="text-xs text-muted-foreground">成功率</p>
                <p className={`text-xl font-bold mt-1 ${getSuccessRateColor(successRate)}`}>
                  {formatSuccessRate(successRate)}
                </p>
              </div>
              <div className="bg-card border border-border rounded-xl p-4">
                <p className="text-xs text-muted-foreground">Token 总量</p>
                <p className="text-xl font-bold text-foreground mt-1">{formatTokens(totalTokens)}</p>
              </div>
              <div className="bg-card border border-border rounded-xl p-4 border-amber-500/20">
                <p className="text-xs text-amber-600 dark:text-amber-400">总费用</p>
                <p className="text-xl font-bold text-amber-600 dark:text-amber-400 mt-1">{formatCost(totalCost)}</p>
              </div>
            </div>

            {/* Trend Chart */}
            <div className="bg-card border border-border rounded-xl p-4">
              <div className="flex items-center justify-between mb-4">
                <h4 className="text-sm font-semibold text-foreground flex items-center gap-2">
                  <Activity className="w-4 h-4 text-primary" />
                  时间趋势
                </h4>
              </div>
              {loading ? (
                <div className="h-56 flex items-center justify-center text-sm text-muted-foreground">
                  <RefreshCw className="w-4 h-4 animate-spin mr-2" /> 加载中
                </div>
              ) : trendData.length > 0 ? (
                <div className="h-56">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={trendData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--muted))" vertical={false} />
                      <XAxis
                        dataKey="timestamp"
                        stroke={AXIS_COLOR}
                        fontSize={10}
                        tickFormatter={(value) => formatTrendTick(String(value), timeRange)}
                      />
                      <YAxis yAxisId="requests" stroke={AXIS_COLOR} fontSize={10} />
                      <YAxis yAxisId="tokens" orientation="right" stroke={AXIS_COLOR} fontSize={10} tickFormatter={formatTokens} />
                      <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={{ fontSize: '12px' }}
                        labelStyle={{ fontSize: '12px', fontWeight: 'bold', marginBottom: '4px' }}
                        labelFormatter={(value) => formatDateTime(String(value))}
                        formatter={(value: number | string, name: string) => {
                          const num = toNumber(value);
                          return [name === 'Token' ? formatTokens(num) : num.toLocaleString(), name];
                        }}
                      />
                      <Legend iconType="circle" wrapperStyle={{ fontSize: '11px', paddingTop: '8px' }} />
                      <Line
                        yAxisId="requests"
                        type="monotone"
                        dataKey="requests"
                        name="请求量"
                        stroke={LINE_COLORS[0]}
                        strokeWidth={2}
                        dot={false}
                        connectNulls
                        activeDot={{ r: 3 }}
                      />
                      <Line
                        yAxisId="tokens"
                        type="monotone"
                        dataKey="tokens"
                        name="Token"
                        stroke={LINE_COLORS[1]}
                        strokeWidth={2}
                        dot={false}
                        connectNulls
                        activeDot={{ r: 3 }}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <div className="h-56 flex items-center justify-center text-sm text-muted-foreground">
                  暂无趋势数据
                </div>
              )}
            </div>

            {/* IP Distribution Table */}
            {ipData.length > 0 && (
              <div className="bg-card border border-border rounded-xl overflow-hidden">
                <div className="px-4 py-3 border-b border-border bg-muted/30">
                  <h4 className="text-sm font-semibold text-foreground flex items-center gap-2">
                    <Globe className="w-3.5 h-3.5 text-emerald-500" />
                    IP 分布
                  </h4>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-muted text-muted-foreground text-xs">
                      <tr>
                        <th className="px-4 py-2.5">IP</th>
                        <th className="px-4 py-2.5 text-right">请求量</th>
                        <th className="px-4 py-2.5 text-right">实时配额</th>
                        <th className="px-4 py-2.5 text-right">最近使用</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {ipData.map((entry, i) => {
                        const ip = entry.ip || '';
                        const isExpanded = Boolean(ip) && expandedIp === ip;
                        const detail = ip ? ipDetails[ip] : undefined;
                        const detailTrend = detail ? normalizeIpTrendData(detail.trend || []) : [];
                        return (
                          <Fragment key={`${entry.ip || 'unknown'}-${i}`}>
                            <tr className={`hover:bg-muted/50 transition-colors ${entry.blocked ? 'bg-red-500/5' : ''}`}>
                              <td className="px-4 py-2.5">
                                <button
                                  type="button"
                                  onClick={() => void toggleIpDetail(entry.ip)}
                                  disabled={!ip}
                                  aria-expanded={isExpanded}
                                  className="flex items-center gap-2 font-mono text-xs text-foreground disabled:cursor-default"
                                >
                                  {ip && (isExpanded
                                    ? <ChevronUp className="w-3.5 h-3.5 text-muted-foreground" />
                                    : <ChevronDown className="w-3.5 h-3.5 text-muted-foreground" />)}
                                  {entry.ip || '—'}
                                </button>
                              </td>
                              <td className="px-4 py-2.5 text-right text-muted-foreground">{toNumber(entry.request_count).toLocaleString()}</td>
                              <td className="px-4 py-2.5 text-right">
                                {entry.quota_summary ? (
                                  <div className="ml-auto w-24" title={`${entry.quota_summary.current || 0}/${entry.quota_summary.limit || 0}`}>
                                    <div className="mb-1 flex items-center justify-end gap-1 text-[10px] text-muted-foreground">
                                      <span className={entry.quota_summary.exhausted ? 'text-red-500' : ''}>{Math.round(toNumber(entry.quota_summary.remaining_ratio) * 100)}%</span>
                                      <span>· {entry.quota_summary.rule_count} 规则</span>
                                    </div>
                                    <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                                      <div
                                        className={`h-full rounded-full ${entry.quota_summary.exhausted ? 'bg-red-500' : entry.quota_summary.remaining_ratio < 0.3 ? 'bg-amber-500' : 'bg-emerald-500'}`}
                                        style={{ width: `${Math.max(1, toNumber(entry.quota_summary.remaining_ratio) * 100)}%` }}
                                      />
                                    </div>
                                  </div>
                                ) : <span className="text-[10px] text-muted-foreground">未配置</span>}
                              </td>
                              <td className="px-4 py-2.5 text-right text-xs text-muted-foreground whitespace-nowrap">{formatDateTime(entry.last_used)}</td>
                            </tr>
                            {isExpanded && (
                              <tr className={entry.blocked ? 'bg-red-500/[0.03]' : 'bg-muted/20'}>
                                <td colSpan={4} className="p-0">
                                  <div className="border-t border-border px-4 py-4">
                                    {ipDetailLoading === ip ? (
                                      <div className="h-28 flex items-center justify-center text-xs text-muted-foreground">
                                        <RefreshCw className="w-3.5 h-3.5 animate-spin mr-2" /> 正在加载 IP 明细
                                      </div>
                                    ) : ipDetailErrors[ip] ? (
                                      <div className="h-20 flex items-center justify-center text-xs text-red-600 dark:text-red-500">
                                        {ipDetailErrors[ip]}
                                      </div>
                                    ) : detail ? (
                                      <div className="space-y-4">
                                        {(detail.quota_rules || []).length > 0 && (
                                          <div className="rounded-lg border border-primary/20 bg-primary/[0.03] p-3">
                                            <div className="mb-2 flex items-center justify-between gap-2">
                                              <div className="text-xs font-semibold text-foreground">当前窗口配额</div>
                                              <div className="text-[10px] text-muted-foreground">不随历史时间范围变化</div>
                                            </div>
                                            <div className="space-y-2">
                                              {detail.quota_rules.flatMap(rule => (rule.buckets || []).length > 0
                                                ? rule.buckets.map((bucket, bucketIndex) => (
                                                <div key={`${rule.id}-${bucketIndex}`} className="rounded-md border border-border bg-background p-2">
                                                  <div className="mb-1.5 flex flex-wrap items-center gap-1.5 text-[10px]">
                                                    <span className="font-medium text-foreground">{rule.label || QUOTA_MEASURE_LABELS[rule.measure] || rule.measure}</span>
                                                    {Object.entries(bucket.dimensions || {}).map(([dimension, value]) => (
                                                      <span key={dimension} className="rounded bg-muted px-1.5 py-0.5 font-mono text-muted-foreground">{dimension}={value}</span>
                                                    ))}
                                                    <span className="ml-auto text-muted-foreground">{rule.aggregate}</span>
                                                  </div>
                                                  <div className="space-y-1.5">
                                                    {(bucket.limits || []).map((limit, limitIndex) => {
                                                      const ratio = Math.max(0, Math.min(1, toNumber(limit.remaining_ratio)));
                                                      return (
                                                        <div key={limitIndex}>
                                                          <div className="mb-1 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
                                                            <span>{limit.label} · {limit.window === 'fixed' ? '固定窗口' : '滑动窗口'}{limit.reset_at ? ` · ${formatQuotaReset(limit.reset_at)} 刷新` : ''}</span>
                                                            <span className="whitespace-nowrap">{formatQuotaValue(rule.measure, toNumber(limit.current))} / {formatQuotaValue(rule.measure, toNumber(limit.limit))}</span>
                                                          </div>
                                                          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                                                            <div className={`h-full rounded-full ${ratio <= 0 ? 'bg-red-500' : ratio < 0.3 ? 'bg-amber-500' : 'bg-emerald-500'}`} style={{ width: `${Math.max(1, ratio * 100)}%` }} />
                                                          </div>
                                                        </div>
                                                      );
                                                    })}
                                                  </div>
                                                </div>
                                              ))
                                                : [(
                                                  <div key={`${rule.id}-empty`} className="rounded-md border border-border bg-background p-2 text-[10px] text-muted-foreground">
                                                    <span className="font-medium text-foreground">{rule.label || QUOTA_MEASURE_LABELS[rule.measure] || rule.measure}</span>
                                                    <span className="ml-2">当前窗口尚无分桶记录</span>
                                                  </div>
                                                )])}
                                            </div>
                                          </div>
                                        )}
                                        <div className="grid grid-cols-3 gap-2">
                                          <div className="rounded-lg border border-border bg-background px-3 py-2">
                                            <div className="text-[10px] text-muted-foreground">请求</div>
                                            <div className="mt-0.5 text-sm font-semibold text-foreground">{toNumber(detail.request_count).toLocaleString()}</div>
                                          </div>
                                          <div className="rounded-lg border border-border bg-background px-3 py-2">
                                            <div className="text-[10px] text-muted-foreground">Token</div>
                                            <div className="mt-0.5 text-sm font-semibold text-foreground">{formatTokens(toNumber(detail.prompt_tokens) + toNumber(detail.completion_tokens))}</div>
                                          </div>
                                          <div className="rounded-lg border border-amber-500/20 bg-background px-3 py-2">
                                            <div className="text-[10px] text-amber-600 dark:text-amber-400">费用</div>
                                            <div className="mt-0.5 text-sm font-semibold text-amber-600 dark:text-amber-400">{formatCost(toNumber(detail.cost))}</div>
                                          </div>
                                        </div>

                                        {detailTrend.length > 0 && (
                                          <div className="h-36 rounded-lg border border-border bg-background p-2">
                                            <ResponsiveContainer width="100%" height="100%">
                                              <LineChart data={detailTrend} margin={{ top: 8, right: 8, bottom: 0, left: -20 }}>
                                                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--muted))" vertical={false} />
                                                <XAxis dataKey="timestamp" stroke={AXIS_COLOR} fontSize={9} tickFormatter={(value) => formatTrendTick(String(value), timeRange)} />
                                                <YAxis yAxisId="requests" stroke={AXIS_COLOR} fontSize={9} />
                                                <YAxis yAxisId="tokens" orientation="right" stroke={AXIS_COLOR} fontSize={9} tickFormatter={formatTokens} />
                                                <Tooltip
                                                  contentStyle={tooltipStyle}
                                                  itemStyle={{ fontSize: '11px' }}
                                                  labelStyle={{ fontSize: '11px', fontWeight: 'bold' }}
                                                  labelFormatter={(value) => formatDateTime(String(value))}
                                                  formatter={(value: number | string, name: string) => {
                                                    const num = toNumber(value);
                                                    return [name === 'Token' ? formatTokens(num) : num.toLocaleString(), name];
                                                  }}
                                                />
                                                <Line yAxisId="requests" type="monotone" dataKey="requests" name="请求量" stroke={LINE_COLORS[0]} strokeWidth={1.75} dot={false} connectNulls />
                                                <Line yAxisId="tokens" type="monotone" dataKey="tokens" name="Token" stroke={LINE_COLORS[1]} strokeWidth={1.75} dot={false} connectNulls />
                                              </LineChart>
                                            </ResponsiveContainer>
                                          </div>
                                        )}

                                        <div className="rounded-lg border border-border bg-background overflow-hidden">
                                          <div className="grid grid-cols-[minmax(0,1fr)_64px_72px_78px] gap-2 bg-muted/60 px-3 py-2 text-[10px] text-muted-foreground">
                                            <span>模型</span><span className="text-right">请求</span><span className="text-right">Token</span><span className="text-right">费用</span>
                                          </div>
                                          {detail.model_distribution.length > 0 ? detail.model_distribution.map((model, modelIndex) => (
                                            <div key={`${model.model || 'unknown'}-${modelIndex}`} className="grid grid-cols-[minmax(0,1fr)_64px_72px_78px] gap-2 border-t border-border px-3 py-2 text-xs">
                                              <span className="truncate font-mono text-foreground" title={model.model || ''}>{model.model || '—'}</span>
                                              <span className="text-right text-muted-foreground">{toNumber(model.request_count).toLocaleString()}</span>
                                              <span className="text-right text-foreground">{formatTokens(getModelTokens(model))}</span>
                                              <span className="text-right font-mono text-amber-600 dark:text-amber-400">{formatCost(toNumber(model.cost))}</span>
                                            </div>
                                          )) : (
                                            <div className="border-t border-border px-3 py-5 text-center text-xs text-muted-foreground">暂无模型明细</div>
                                          )}
                                        </div>
                                      </div>
                                    ) : null}
                                  </div>
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Model Distribution Table */}
            {modelData.length > 0 && (
              <div className="bg-card border border-border rounded-xl overflow-hidden">
                <div className="px-4 py-3 border-b border-border bg-muted/30">
                  <h4 className="text-sm font-semibold text-foreground flex items-center gap-2">
                    <Box className="w-3.5 h-3.5 text-primary" />
                    模型分布
                  </h4>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-muted text-muted-foreground text-xs">
                      <tr>
                        <th className="px-4 py-2.5">模型</th>
                        <th className="px-4 py-2.5 text-right">请求量</th>
                        <th className="px-4 py-2.5 text-right">Token</th>
                        <th className="px-4 py-2.5 text-right">费用</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {modelData.map((entry, i) => (
                        <tr key={`${entry.model || 'unknown'}-${i}`} className="hover:bg-muted/50 transition-colors">
                          <td className="px-4 py-2.5 font-mono text-xs text-foreground">{entry.model || '—'}</td>
                          <td className="px-4 py-2.5 text-right text-muted-foreground">{toNumber(entry.request_count).toLocaleString()}</td>
                          <td className="px-4 py-2.5 text-right font-medium text-foreground">{getModelTokens(entry).toLocaleString()}</td>
                          <td className="px-4 py-2.5 text-right font-mono text-amber-600 dark:text-amber-400">{entry.cost > 0 ? formatCost(entry.cost) : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Error Logs (collapsible) */}
            {errorData.length > 0 && (
              <div className="bg-card border border-border rounded-xl overflow-hidden">
                <button
                  onClick={() => setErrorsExpanded(!errorsExpanded)}
                  className="w-full px-4 py-3 flex items-center justify-between hover:bg-muted/30 transition-colors"
                >
                  <h4 className="text-sm font-semibold text-foreground flex items-center gap-2">
                    <AlertCircle className="w-3.5 h-3.5 text-red-500" />
                    最近错误列表
                    <span className="bg-red-500/10 text-red-600 dark:text-red-500 text-xs px-1.5 py-0.5 rounded-full">
                      {errorData.length}
                    </span>
                  </h4>
                  {errorsExpanded
                    ? <ChevronUp className="w-4 h-4 text-muted-foreground" />
                    : <ChevronDown className="w-4 h-4 text-muted-foreground" />}
                </button>
                {errorsExpanded && (
                  <div className="border-t border-border overflow-x-auto">
                    <table className="w-full text-left text-sm">
                      <thead className="bg-muted text-muted-foreground text-xs">
                        <tr>
                          <th className="px-4 py-2.5">时间</th>
                          <th className="px-4 py-2.5">模型</th>
                          <th className="px-4 py-2.5 text-center">状态码</th>
                          <th className="px-4 py-2.5">Provider</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {errorData.map((err, i) => (
                          <tr key={`${err.timestamp || 'unknown'}-${i}`} className="hover:bg-muted/50 transition-colors">
                            <td className="px-4 py-2.5 text-xs text-muted-foreground whitespace-nowrap">{formatDateTime(err.timestamp)}</td>
                            <td className="px-4 py-2.5 font-mono text-xs text-foreground">{err.model || '—'}</td>
                            <td className="px-4 py-2.5 text-center">
                              <span className="bg-red-500/10 text-red-600 dark:text-red-500 text-xs px-2 py-0.5 rounded font-mono">
                                {err.status_code || '—'}
                              </span>
                            </td>
                            <td className="px-4 py-2.5 text-xs text-muted-foreground">{err.provider || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            {/* Empty state */}
            {!loading && !hasAnyData && (
              <div className="text-center py-12 text-muted-foreground">
                <BarChart3 className="w-12 h-12 mx-auto mb-3 opacity-30" />
                <p className="text-sm">该 Key 在所选时间范围内暂无数据</p>
              </div>
            )}

            <div className="h-6" />
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
