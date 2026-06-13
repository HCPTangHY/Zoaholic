import { useCallback, useEffect, useMemo, useState } from 'react';
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

interface IpEntry {
  ip?: string | null;
  request_count: number;
  last_used?: string | null;
  blocked?: boolean;
}

interface ModelEntry {
  model?: string | null;
  request_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens?: number;
  cost: number;
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

const getModelTokens = (entry: ModelEntry) => {
  const explicitTotal = toNumber(entry.total_tokens);
  if (explicitTotal > 0) return explicitTotal;
  return toNumber(entry.prompt_tokens) + toNumber(entry.completion_tokens);
};

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
                        <th className="px-4 py-2.5 text-right">最近使用</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {ipData.map((entry, i) => (
                        <tr key={`${entry.ip || 'unknown'}-${i}`} className={`hover:bg-muted/50 transition-colors ${entry.blocked ? 'bg-red-500/5' : ''}`}>
                          <td className="px-4 py-2.5 font-mono text-xs text-foreground">{entry.ip || '—'}</td>
                          <td className="px-4 py-2.5 text-right text-muted-foreground">{toNumber(entry.request_count).toLocaleString()}</td>
                          <td className="px-4 py-2.5 text-right text-xs text-muted-foreground whitespace-nowrap">{formatDateTime(entry.last_used)}</td>
                        </tr>
                      ))}
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
