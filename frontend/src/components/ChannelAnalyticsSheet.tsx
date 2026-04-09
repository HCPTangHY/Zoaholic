import { useEffect, useState } from 'react';
import {
  X, RefreshCw, Activity, BarChart3, AlertCircle,
  ChevronDown, ChevronUp, KeyRound
} from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Legend
} from 'recharts';
import { apiFetch } from '../lib/api';

// ========== Types ==========

interface ChannelAnalyticsSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  providerName: string;
}

interface UsageEntry {
  provider: string;
  model: string;
  request_count: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  total_cost: number;
}

interface KeyRanking {
  api_key: string;
  success_count: number;
  total_requests: number;
  success_rate: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
}

interface ErrorLog {
  id: number;
  timestamp: string;
  model?: string;
  status_code?: number;
  api_key_prefix?: string;
}

// ========== Constants ==========

const TIME_RANGES = [
  { label: '1h', value: 1 },
  { label: '6h', value: 6 },
  { label: '24h', value: 24 },
  { label: '7天', value: 168 },
  { label: '30天', value: 720 },
];

const LINE_COLORS = [
  '#3b82f6', '#ef4444', '#22c55e', '#f59e0b',
  '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16',
];

const AXIS_COLOR = 'hsl(var(--muted-foreground))';

// ========== Helpers ==========

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

const maskKey = (key: string) => {
  if (!key || key.length < 16) return key || '—';
  return `${key.slice(0, 8)}...${key.slice(-4)}`;
};

// ========== Component ==========

export function ChannelAnalyticsSheet({ open, onOpenChange, providerName }: ChannelAnalyticsSheetProps) {
  const [timeRange, setTimeRange] = useState(24);
  const [loading, setLoading] = useState(false);

  // Data
  const [successRate, setSuccessRate] = useState<number | null>(null);
  const [usageData, setUsageData] = useState<UsageEntry[]>([]);
  const [trendData, setTrendData] = useState<any[]>([]);
  const [trendTokensData, setTrendTokensData] = useState<any[]>([]);
  const [trendModels, setTrendModels] = useState<string[]>([]);
  const [trendMetric, setTrendMetric] = useState<'count' | 'tokens'>('count');
  const [keyRankings, setKeyRankings] = useState<KeyRanking[]>([]);
  const [errorLogs, setErrorLogs] = useState<ErrorLog[]>([]);
  const [errorsExpanded, setErrorsExpanded] = useState(false);

  const fetchAll = async () => {
    if (!providerName) return;
    setLoading(true);
    setUsageData([]);
    setTrendData([]);
    setTrendTokensData([]);
    setTrendModels([]);
    setKeyRankings([]);
    setErrorLogs([]);
    setSuccessRate(null);

    const end = new Date();
    const start = new Date(end.getTime() - timeRange * 3600_000);
    const enc = encodeURIComponent(providerName);

    try {
      const [statsRes, usageRes, trendRes, keyRes, logsRes] = await Promise.all([
        apiFetch(`/v1/stats?hours=${timeRange}`),
        apiFetch(`/v1/stats/usage_analysis?provider=${enc}&hours=${timeRange}`),
        apiFetch(`/v1/stats/model_trend?provider=${enc}&hours=${timeRange}`),
        apiFetch(`/v1/channel_key_rankings?provider_name=${enc}&start_datetime=${start.toISOString()}&end_datetime=${end.toISOString()}`),
        apiFetch(`/v1/logs?provider=${enc}&success=false&page_size=10`),
      ]);

      if (statsRes.ok) {
        const raw = await statsRes.json();
        const stats = raw.stats || raw;
        const ch = stats.channel_success_rates?.find((c: any) => c.provider === providerName);
        setSuccessRate(ch?.success_rate ?? null);
      }
      if (usageRes.ok) {
        const result = await usageRes.json();
        setUsageData(result.data || []);
      }
      if (trendRes.ok) {
        const result = await trendRes.json();
        setTrendData(result.data || []);
        setTrendTokensData(result.tokens_data || []);
        setTrendModels(result.models || []);
      }
      if (keyRes.ok) {
        const result = await keyRes.json();
        setKeyRankings(result.rankings || []);
      }
      if (logsRes.ok) {
        const result = await logsRes.json();
        setErrorLogs(result.items || []);
      }
    } catch (err) {
      console.error('Failed to fetch channel analytics:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open && providerName) {
      fetchAll();
    }
  }, [open, providerName, timeRange]);

  // Computed
  const totalRequests = usageData.reduce((s, r) => s + r.request_count, 0);
  const totalTokens = usageData.reduce((s, r) => s + r.total_tokens, 0);
  const totalCost = usageData.reduce((s, r) => s + (r.total_cost || 0), 0);

  const activeTrendData = trendMetric === 'tokens' ? trendTokensData : trendData;

  const tooltipStyle = {
    backgroundColor: 'hsl(var(--popover))',
    borderColor: 'hsl(var(--border))',
    color: 'hsl(var(--popover-foreground))',
    borderRadius: '8px',
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40 animate-in fade-in duration-200" />
        <Dialog.Content className="fixed right-0 top-0 h-full w-full md:w-[720px] max-w-full bg-background border-l border-border shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300">
          {/* Header */}
          <div className="p-4 sm:p-5 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
            <Dialog.Title className="text-lg sm:text-xl font-bold text-foreground flex items-center gap-2">
              <BarChart3 className="w-5 h-5 text-primary" />
              渠道分析: {providerName}
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
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <div className="bg-card border border-border rounded-xl p-4">
                <p className="text-xs text-muted-foreground">总请求量</p>
                <p className="text-xl font-bold text-foreground mt-1">{totalRequests.toLocaleString()}</p>
              </div>
              <div className="bg-card border border-border rounded-xl p-4">
                <p className="text-xs text-muted-foreground">上游成功率</p>
                <p className={`text-xl font-bold mt-1 ${
                  successRate === null ? 'text-muted-foreground'
                    : successRate >= 0.95 ? 'text-emerald-600 dark:text-emerald-500'
                    : successRate >= 0.8 ? 'text-amber-600 dark:text-amber-500'
                    : 'text-red-600 dark:text-red-500'
                }`}>
                  {successRate !== null ? `${(successRate * 100).toFixed(1)}%` : '—'}
                </p>
              </div>
              <div className="bg-card border border-border rounded-xl p-4">
                <p className="text-xs text-muted-foreground">Token 消耗</p>
                <p className="text-xl font-bold text-foreground mt-1">{formatTokens(totalTokens)}</p>
              </div>
              <div className="bg-card border border-border rounded-xl p-4 border-amber-500/20">
                <p className="text-xs text-amber-600 dark:text-amber-400">费用</p>
                <p className="text-xl font-bold text-amber-600 dark:text-amber-400 mt-1">{formatCost(totalCost)}</p>
              </div>
            </div>

            {/* Trend Chart */}
            <div className="bg-card border border-border rounded-xl p-4">
              <div className="flex items-center justify-between mb-4">
                <h4 className="text-sm font-semibold text-foreground flex items-center gap-2">
                  <Activity className="w-4 h-4 text-primary" />
                  请求趋势（按小时）
                </h4>
                <div className="flex items-center bg-muted rounded-lg p-0.5">
                  <button
                    onClick={() => setTrendMetric('count')}
                    className={`px-2.5 py-1 text-xs rounded-md transition-colors ${
                      trendMetric === 'count' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'
                    }`}
                  >
                    请求次数
                  </button>
                  <button
                    onClick={() => setTrendMetric('tokens')}
                    className={`px-2.5 py-1 text-xs rounded-md transition-colors ${
                      trendMetric === 'tokens' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'
                    }`}
                  >
                    Token
                  </button>
                </div>
              </div>
              {loading ? (
                <div className="h-56 flex items-center justify-center text-sm text-muted-foreground">
                  <RefreshCw className="w-4 h-4 animate-spin mr-2" /> 加载中
                </div>
              ) : activeTrendData.length > 0 && trendModels.length > 0 ? (
                <div className="h-56">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={activeTrendData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--muted))" vertical={false} />
                      <XAxis
                        dataKey="hour"
                        stroke={AXIS_COLOR}
                        fontSize={10}
                        tickFormatter={(utcStr) => {
                          try {
                            const d = new Date(String(utcStr).replace(' ', 'T') + 'Z');
                            if (isNaN(d.getTime())) return String(utcStr);
                            return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
                          } catch {
                            return String(utcStr);
                          }
                        }}
                      />
                      <YAxis
                        stroke={AXIS_COLOR}
                        fontSize={10}
                        tickFormatter={trendMetric === 'tokens' ? formatTokens : undefined}
                      />
                      <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={{ fontSize: '12px' }}
                        labelStyle={{ fontSize: '12px', fontWeight: 'bold', marginBottom: '4px' }}
                        formatter={trendMetric === 'tokens' ? (value: number) => formatTokens(value) : undefined}
                      />
                      <Legend iconType="circle" wrapperStyle={{ fontSize: '11px', paddingTop: '8px' }} />
                      {trendModels.map((m, i) => (
                        <Line
                          key={m}
                          type="monotone"
                          dataKey={m}
                          name={m}
                          stroke={LINE_COLORS[i % LINE_COLORS.length]}
                          strokeWidth={2}
                          dot={false}
                          connectNulls
                          activeDot={{ r: 3 }}
                        />
                      ))}
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <div className="h-56 flex items-center justify-center text-sm text-muted-foreground">
                  暂无趋势数据
                </div>
              )}
            </div>

            {/* Model Usage Table */}
            {usageData.length > 0 && (
              <div className="bg-card border border-border rounded-xl overflow-hidden">
                <div className="px-4 py-3 border-b border-border bg-muted/30">
                  <h4 className="text-sm font-semibold text-foreground">模型用量明细</h4>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-muted text-muted-foreground text-xs">
                      <tr>
                        <th className="px-4 py-2.5">模型</th>
                        <th className="px-4 py-2.5 text-right">请求次数</th>
                        <th className="px-4 py-2.5 text-right">输入 Token</th>
                        <th className="px-4 py-2.5 text-right">输出 Token</th>
                        <th className="px-4 py-2.5 text-right">总 Token</th>
                        <th className="px-4 py-2.5 text-right">费用</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {usageData.map((entry, i) => (
                        <tr key={i} className="hover:bg-muted/50 transition-colors">
                          <td className="px-4 py-2.5 font-mono text-xs text-foreground">{entry.model}</td>
                          <td className="px-4 py-2.5 text-right text-muted-foreground">{entry.request_count.toLocaleString()}</td>
                          <td className="px-4 py-2.5 text-right text-muted-foreground">{entry.total_prompt_tokens.toLocaleString()}</td>
                          <td className="px-4 py-2.5 text-right text-muted-foreground">{entry.total_completion_tokens.toLocaleString()}</td>
                          <td className="px-4 py-2.5 text-right font-medium text-foreground">{entry.total_tokens.toLocaleString()}</td>
                          <td className="px-4 py-2.5 text-right font-mono text-amber-600 dark:text-amber-400">{entry.total_cost > 0 ? formatCost(entry.total_cost) : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Key Health Table */}
            {keyRankings.length > 0 && (
              <div className="bg-card border border-border rounded-xl overflow-hidden">
                <div className="px-4 py-3 border-b border-border bg-muted/30">
                  <h4 className="text-sm font-semibold text-foreground flex items-center gap-2">
                    <KeyRound className="w-3.5 h-3.5 text-emerald-500" />
                    上游 Key 用量与健康度
                  </h4>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-muted text-muted-foreground text-xs">
                      <tr>
                        <th className="px-4 py-2.5">API Key</th>
                        <th className="px-4 py-2.5 text-right">请求</th>
                        <th className="px-4 py-2.5 text-right">成功率</th>
                        <th className="px-4 py-2.5 text-right">输入 Token</th>
                        <th className="px-4 py-2.5 text-right">输出 Token</th>
                        <th className="px-4 py-2.5 text-right">总 Token</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {keyRankings.map((k, i) => {
                        const rate = k.success_rate;
                        const color = rate >= 0.95
                          ? 'text-emerald-600 dark:text-emerald-500'
                          : rate >= 0.8
                            ? 'text-amber-600 dark:text-amber-500'
                            : 'text-red-600 dark:text-red-500';
                        return (
                          <tr key={i} className="hover:bg-muted/50 transition-colors">
                            <td className="px-4 py-2.5 font-mono text-xs text-foreground">{maskKey(k.api_key)}</td>
                            <td className="px-4 py-2.5 text-right text-muted-foreground">{k.success_count}/{k.total_requests}</td>
                            <td className="px-4 py-2.5 text-right">
                              <span className={`font-mono font-bold ${color}`}>
                                {(rate * 100).toFixed(1)}%
                              </span>
                            </td>
                            <td className="px-4 py-2.5 text-right text-muted-foreground">{(k.total_prompt_tokens || 0).toLocaleString()}</td>
                            <td className="px-4 py-2.5 text-right text-muted-foreground">{(k.total_completion_tokens || 0).toLocaleString()}</td>
                            <td className="px-4 py-2.5 text-right font-medium text-foreground">{(k.total_tokens || 0).toLocaleString()}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Error Logs (collapsible) */}
            {errorLogs.length > 0 && (
              <div className="bg-card border border-border rounded-xl overflow-hidden">
                <button
                  onClick={() => setErrorsExpanded(!errorsExpanded)}
                  className="w-full px-4 py-3 flex items-center justify-between hover:bg-muted/30 transition-colors"
                >
                  <h4 className="text-sm font-semibold text-foreground flex items-center gap-2">
                    <AlertCircle className="w-3.5 h-3.5 text-red-500" />
                    最近失败请求
                    <span className="bg-red-500/10 text-red-600 dark:text-red-500 text-xs px-1.5 py-0.5 rounded-full">
                      {errorLogs.length}
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
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {errorLogs.map(log => (
                          <tr key={log.id} className="hover:bg-muted/50 transition-colors">
                            <td className="px-4 py-2.5 text-xs text-muted-foreground whitespace-nowrap">
                              {new Date(log.timestamp).toLocaleString()}
                            </td>
                            <td className="px-4 py-2.5 font-mono text-xs text-foreground">{log.model || '—'}</td>
                            <td className="px-4 py-2.5 text-center">
                              <span className="bg-red-500/10 text-red-600 dark:text-red-500 text-xs px-2 py-0.5 rounded font-mono">
                                {log.status_code || '—'}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            {/* Empty state */}
            {!loading && usageData.length === 0 && keyRankings.length === 0 && (
              <div className="text-center py-12 text-muted-foreground">
                <BarChart3 className="w-12 h-12 mx-auto mb-3 opacity-30" />
                <p className="text-sm">该渠道在所选时间范围内暂无数据</p>
              </div>
            )}

            <div className="h-6" />
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
