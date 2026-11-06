import { useEffect, useState, useMemo } from 'react';
import {
  Activity, Cpu, Zap, BarChart3, AlertCircle, CheckCircle2,
  RefreshCw, Server, ChevronDown, ChevronUp, DollarSign, Search, X
} from 'lucide-react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
  PieChart, Pie, Legend, LineChart, Line, CartesianGrid
} from 'recharts';

interface StatData {
  time_range: string;
  channel_success_rates: { provider: string; success_rate: number; total_requests: number }[];
  model_request_counts: { model: string; count: number }[];
  endpoint_request_counts: { endpoint: string; count: number }[];
}

interface AnalysisEntry {
  provider: string;
  model: string;
  request_count: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  recorded_cost: number;
}

const TIME_RANGES = [
  { label: '1 小时', value: 1 },
  { label: '6 小时', value: 6 },
  { label: '24 小时', value: 24 },
  { label: '7 天', value: 168 },
  { label: '30 天', value: 720 }
];

const CHART_COLORS = [
  'hsl(var(--primary))',
  'hsl(var(--ring))',
  'hsl(160 84% 39%)',
  'hsl(38 92% 50%)',
  'hsl(var(--destructive))',
  'hsl(var(--secondary-foreground))'
];

const AXIS_COLOR = 'hsl(var(--muted-foreground))';
const SUCCESS_COLOR = 'hsl(160 84% 39%)';
const WARNING_COLOR = 'hsl(38 92% 50%)';
const ERROR_COLOR = 'hsl(var(--destructive))';

const LINE_COLORS = [
  '#3b82f6', '#ef4444', '#22c55e', '#f59e0b',
  '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16',
];

const formatCompact = (n: number) => {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
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

/** 多选下拉组件 */
function MultiSelect({
  label,
  options,
  selected,
  onChange,
  placeholder,
}: {
  label: string;
  options: string[];
  selected: string[];
  onChange: (val: string[]) => void;
  placeholder: string;
}) {
  const toggle = (val: string) => {
    if (selected.includes(val)) {
      onChange(selected.filter(v => v !== val));
    } else {
      onChange([...selected, val]);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <label className="text-xs font-medium text-muted-foreground">{label}</label>
        <div className="flex items-center gap-2">
          {options.length > 0 && (
            <button
              type="button"
              onClick={() => onChange(selected.length === options.length ? [] : [...options])}
              className="text-xs text-primary hover:underline"
            >
              {selected.length === options.length ? '清除' : '全选'}
            </button>
          )}
          {selected.length > 0 && selected.length < options.length && (
            <button
              type="button"
              onClick={() => onChange([])}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              清除
            </button>
          )}
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5 p-2 bg-background border border-border rounded-lg min-h-[36px] max-h-32 overflow-y-auto">
        {options.length === 0 ? (
          <span className="text-xs text-muted-foreground py-0.5">{placeholder}</span>
        ) : (
          options.map(opt => (
            <button
              key={opt}
              type="button"
              onClick={() => toggle(opt)}
              className={`inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border transition-colors ${
                selected.includes(opt)
                  ? 'bg-primary text-primary-foreground border-primary'
                  : 'bg-muted/50 text-muted-foreground border-border hover:bg-muted hover:text-foreground'
              }`}
            >
              <span className="truncate max-w-[150px]">{opt}</span>
              {selected.includes(opt) && <X className="w-3 h-3 shrink-0" />}
            </button>
          ))
        )}
      </div>
    </div>
  );
}

/** UTC 时间字符串转本地时间显示，根据粒度自适应 */
const formatTimeTick = (utcStr: string, gran?: string) => {
  try {
    const s = String(utcStr);
    // 天粒度：YYYY-MM-DD 格式，不带时区偏移
    if (gran === 'day' || (s.length <= 10 && /^\d{4}-\d{2}-\d{2}$/.test(s))) {
      return `${s.slice(5, 7)}/${s.slice(8, 10)}`;
    }
    const d = new Date(s.replace(' ', 'T') + 'Z');
    if (isNaN(d.getTime())) return s;
    return `${String(d.getMonth() + 1)}/${String(d.getDate())} ${String(d.getHours()).padStart(2,'0')}:00`;
  } catch {
    return String(utcStr);
  }
};

const formatTimeTooltip = (utcStr: string, gran?: string) => {
  try {
    const s = String(utcStr);
    if (gran === 'day' || (s.length <= 10 && /^\d{4}-\d{2}-\d{2}$/.test(s))) {
      return s;
    }
    const d = new Date(s.replace(' ', 'T') + 'Z');
    if (isNaN(d.getTime())) return s;
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:00`;
  } catch {
    return String(utcStr);
  }
};

export default function Dashboard() {
  const [stats, setStats] = useState<StatData | null>(null);
  const [totalTokens, setTotalTokens] = useState(0);
  const [totalCost, setTotalCost] = useState(0);
  const [loading, setLoading] = useState(true);
  const [timeRange, setTimeRange] = useState(24);
  const { token } = useAuthStore();
  const tooltipStyle = {
    backgroundColor: 'hsl(var(--popover))',
    borderColor: 'hsl(var(--border))',
    color: 'hsl(var(--popover-foreground))',
    borderRadius: '8px'
  };

  // 用量分析状态
  const [analysisOpen, setAnalysisOpen] = useState(true);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisData, setAnalysisData] = useState<AnalysisEntry[]>([]);
  const [analysisProviders, setAnalysisProviders] = useState<string[]>([]);
  const [analysisModels, setAnalysisModels] = useState<string[]>([]);
  const [analysisStart, setAnalysisStart] = useState('');
  const [analysisEnd, setAnalysisEnd] = useState('');
  const [analysisQueried, setAnalysisQueried] = useState(false);
  const [trendData, setTrendData] = useState<Record<string, string | number>[]>([]);
  const [trendModelsRaw, setTrendModelsRaw] = useState<string[]>([]);
  const [tokenBreakdown, setTokenBreakdown] = useState<any[]>([]);
  const [trendGranularity, setTrendGranularity] = useState<string>('hour');
  const [trendLoading, setTrendLoading] = useState(false);

  const fetchData = async () => {
    if (!token) return;
    setLoading(true);
    try {
      const end = new Date();
      const start = new Date(end.getTime() - timeRange * 60 * 60 * 1000);
      const tokenUrl = `/v1/token_usage?start_datetime=${encodeURIComponent(start.toISOString())}&end_datetime=${encodeURIComponent(end.toISOString())}`;

      const [statsRes, tokenRes] = await Promise.all([
        apiFetch(`/v1/stats?hours=${timeRange}`),
        apiFetch(tokenUrl),
      ]);

      if (statsRes.ok) {
        const data = await statsRes.json();
        setStats(data.stats || data);
        setTotalCost((data.stats || data).total_cost || 0);
      }

      if (tokenRes.ok) {
        const data = await tokenRes.json();
        const total = data.usage?.reduce((sum: number, item: { total_tokens?: number }) => sum + (item.total_tokens || 0), 0) || 0;
        setTotalTokens(total);
      }
    } catch (err) {
      console.error('Failed to fetch stats:', err);
    } finally {
      setLoading(false);
    }
  };

  const fetchAnalysis = async () => {
    if (!token) return;
    setAnalysisLoading(true);
    setAnalysisQueried(true);
    setAnalysisData([]);
    setTrendData([]);
    setTokenBreakdown([]);
    try {
      const params = new URLSearchParams();

      if (analysisStart) {
        params.set('start_datetime', new Date(analysisStart).toISOString());
      }
      if (analysisEnd) {
        params.set('end_datetime', new Date(analysisEnd).toISOString());
      }
      if (!analysisStart && !analysisEnd) {
        params.set('hours', String(timeRange));
      }
      if (analysisProviders.length > 0) {
        // 选中主渠道时自动包含其子渠道（子渠道名格式：主渠道名:引擎）
        const expandedProviders = new Set(analysisProviders);
        const allProviders = channelStats.map(c => c.provider);
        analysisProviders.forEach(sel => {
          allProviders.forEach(p => {
            if (p.startsWith(sel + ':')) expandedProviders.add(p);
          });
        });
        params.set('provider', Array.from(expandedProviders).join(','));
      }
      if (analysisModels.length > 0) {
        params.set('model', analysisModels.join(','));
      }

      const [res, trendRes] = await Promise.all([
        apiFetch(`/v1/stats/usage_analysis?${params}`),
        apiFetch(`/v1/stats/model_trend?${params}`),
      ]);

      if (res.ok) {
        const result = await res.json();
        setAnalysisData(result.data || []);
      }

      if (trendRes.ok) {
        const contentType = trendRes.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
          console.error('Trend API did not return JSON.');
          setTrendData([]);
          setTrendModelsRaw([]);
          setTokenBreakdown([]);
        } else {
          const trendResult = await trendRes.json();
          setTrendData(trendResult.data || []);
          setTrendModelsRaw(trendResult.models || []);
          setTokenBreakdown(trendResult.token_breakdown || []);
          setTrendGranularity(trendResult.granularity || 'hour');
        }
      } else {
        setTrendData([]);
        setTrendModelsRaw([]);
        setTokenBreakdown([]);
      }
    } catch (err) {
      console.error('Failed to fetch analysis:', err);
      setTrendModelsRaw([]);
      setTrendGranularity('hour');
    } finally {
      setAnalysisLoading(false);
      setTrendLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    fetchAnalysis();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, timeRange]);



  const channelStats = (stats?.channel_success_rates || []).slice().sort((a, b) => b.total_requests - a.total_requests);
  const modelStats = stats?.model_request_counts || [];
  const endpointStats = stats?.endpoint_request_counts || [];

  const totalRequests = channelStats.reduce((sum, item) => sum + item.total_requests, 0) || 0;
  const avgSuccessRate = totalRequests > 0
    ? channelStats.reduce((sum, item) => sum + item.success_rate * item.total_requests, 0) / totalRequests
    : 0;
  const activeChannels = channelStats.length || 0;

  const timeRangeLabel = TIME_RANGES.find(r => r.value === timeRange)?.label ?? `${timeRange} 小时`;

  const availableProviders = Array.from(new Set(channelStats.map(c => c.provider))).sort();
  const availableModels = Array.from(new Set(modelStats.map(m => m.model))).sort();

  // 用量分析汇总
  const analysisTotalRequests = analysisData.reduce((s, r) => s + r.request_count, 0);
  const analysisTotalPrompt = analysisData.reduce((s, r) => s + r.total_prompt_tokens, 0);
  const analysisTotalCompletion = analysisData.reduce((s, r) => s + r.total_completion_tokens, 0);
  const analysisTotalTokensAll = analysisData.reduce((s, r) => s + r.total_tokens, 0);
  const analysisTotalCost = analysisData.reduce((s, r) => s + r.recorded_cost, 0);

  const topCards = [
    { label: '总请求量', value: formatCompact(totalRequests), icon: Zap, color: 'text-amber-500', bg: 'bg-amber-500/10' },
    { label: `Token 消耗 (${timeRangeLabel})`, value: formatCompact(totalTokens), icon: BarChart3, color: 'text-blue-500', bg: 'bg-blue-500/10' },
    { label: '平均成功率', value: `${(avgSuccessRate * 100).toFixed(1)}%`, icon: CheckCircle2, color: 'text-emerald-500', bg: 'bg-emerald-500/10' },
    { label: '活跃渠道', value: activeChannels.toString(), icon: Cpu, color: 'text-purple-500', bg: 'bg-purple-500/10' },
    { label: `费用 (${timeRangeLabel})`, value: formatCost(totalCost), icon: DollarSign, color: 'text-amber-500', bg: 'bg-amber-500/10' },
  ];

  const formattedEndpointStats = endpointStats.slice(0, 5).map(item => ({
    name: item.endpoint.replace('POST ', '').replace('GET ', ''),
    value: item.count
  }));

  const formattedChannelStats = channelStats.filter(c => c.total_requests >= 5).slice(0, 8).map(item => ({
    name: item.provider,
    success_rate: Math.round(item.success_rate * 1000) / 10,
    requests: item.total_requests
  }));



  // 折线图只显示请求量 top 8 的模型
  const trendModels = useMemo(() => {
    if (trendModelsRaw.length <= 8) return trendModelsRaw;
    const totals = new Map<string, number>();
    trendData.forEach(row => {
      trendModelsRaw.forEach(m => {
        totals.set(m, (totals.get(m) || 0) + (Number(row[m]) || 0));
      });
    });
    return [...totals.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8)
      .map(([m]) => m);
  }, [trendModelsRaw, trendData]);

  // Token 用量堆叠柱状图数据
  const tokenBarData = useMemo(() => {
    return tokenBreakdown.map((row: any) => ({
      hour: row.hour,
      cached: row.cached_tokens || 0,
      uncached: Math.max(0, (row.prompt_tokens || 0) - (row.cached_tokens || 0)),
      output: row.completion_tokens || 0,
    }));
  }, [tokenBreakdown]);

  if (loading && !stats) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
        <RefreshCw className="w-8 h-8 animate-spin mb-3" />
        <p>加载数据中...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-500 font-sans pb-12">
      {/* Header */}
      <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-foreground">数据看板</h1>
          <p className="text-muted-foreground mt-1 text-sm sm:text-base">系统网关的实时监控与数据分析。</p>
        </div>

        <div className="flex items-center gap-2">
          <div className="flex items-center bg-card border border-border rounded-lg p-1 overflow-x-auto">
            {TIME_RANGES.map(range => (
              <button
                key={range.value}
                onClick={() => setTimeRange(range.value)}
                className={`px-2 sm:px-3 py-1.5 text-xs font-medium rounded-md transition-all whitespace-nowrap ${timeRange === range.value
                    ? 'bg-primary text-primary-foreground shadow-sm'
                    : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'
                  }`}
              >
                {range.label}
              </button>
            ))}
          </div>
          <button onClick={fetchData} className="p-2 text-muted-foreground hover:text-foreground bg-card border border-border rounded-lg transition-colors">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {/* Top Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-6">
        {topCards.map((stat, i) => (
          <div key={i} className="bg-card border border-border p-6 rounded-xl shadow-sm">
            <div className="flex justify-between items-start">
              <div>
                <p className="text-sm font-medium text-muted-foreground">{stat.label}</p>
                <h3 className="text-2xl sm:text-3xl font-bold text-foreground mt-2 truncate" title={stat.value}>{stat.value}</h3>
              </div>
              <div className={`p-2 rounded-lg ${stat.bg}`}>
                <stat.icon className={`w-5 h-5 ${stat.color}`} />
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Chart Section 1 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-card border border-border rounded-xl p-6 shadow-sm">
          <h3 className="text-base font-semibold text-foreground mb-6 flex items-center gap-2">
            <Cpu className="w-4 h-4 text-emerald-500" />
            渠道成功率 (%)
          </h3>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={formattedChannelStats} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
                <XAxis dataKey="name" stroke={AXIS_COLOR} fontSize={12} tickLine={false} axisLine={false} />
                <YAxis stroke={AXIS_COLOR} fontSize={12} tickLine={false} axisLine={false} domain={[0, 100]} />
                <Tooltip
                  cursor={{ fill: 'hsl(var(--muted) / 0.5)' }}
                  contentStyle={tooltipStyle}
                  itemStyle={{ color: tooltipStyle.color }}
                  formatter={(value: number) => [`${value.toFixed(1)}%`, '成功率']}
                />
                <Bar dataKey="success_rate" name="成功率" radius={[4, 4, 0, 0]}>
                  {formattedChannelStats.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={entry.success_rate >= 95 ? SUCCESS_COLOR : entry.success_rate >= 80 ? WARNING_COLOR : ERROR_COLOR} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="bg-card border border-border rounded-xl p-6 shadow-sm">
          <h3 className="text-base font-semibold text-foreground mb-6 flex items-center gap-2">
            <Activity className="w-4 h-4 text-blue-500" />
            模型请求量分布
          </h3>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={modelStats.slice(0, 5)}
                  cx="35%"
                  innerRadius={50}
                  outerRadius={80}
                  paddingAngle={3}
                  dataKey="count"
                  nameKey="model"
                >
                  {modelStats.slice(0, 5).map((_, index) => (
                    <Cell key={`cell-${index}`} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} itemStyle={{ color: tooltipStyle.color }} />
                <Legend
                  layout="vertical"
                  align="right"
                  verticalAlign="middle"
                  wrapperStyle={{ paddingLeft: '10px', fontSize: '12px', maxWidth: '45%' }}
                  formatter={(value: string) => <span className="text-foreground truncate block max-w-[120px]" title={value}>{value}</span>}
                />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      {/* Chart Section 2 & Table */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="bg-card border border-border rounded-xl p-6 shadow-sm flex flex-col">
          <h3 className="text-base font-semibold text-foreground mb-6 flex items-center gap-2">
            <Server className="w-4 h-4 text-purple-500" />
            接口访问分布 (Endpoint)
          </h3>
          <div className="flex-1 min-h-[250px]">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={formattedEndpointStats}
                  outerRadius={100}
                  dataKey="value"
                  nameKey="name"
                  label={({ name, percent }) => `${name} (${(percent * 100).toFixed(0)}%)`}
                  labelLine={false}
                >
                  {formattedEndpointStats.map((_, index) => (
                    <Cell key={`cell-${index}`} fill={CHART_COLORS[(index + 2) % CHART_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} itemStyle={{ color: tooltipStyle.color }} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="lg:col-span-2 bg-card border border-border rounded-xl shadow-sm overflow-hidden flex flex-col">
          <div className="p-6 border-b border-border bg-muted/30">
            <h3 className="text-base font-semibold text-foreground flex items-center gap-2">
              <Cpu className="w-4 h-4 text-primary" />
              渠道健康状况详细
            </h3>
          </div>
          <div className="overflow-auto flex-1" style={{ maxHeight: '400px' }}>
            <table className="w-full text-left text-sm">
              <thead className="bg-muted text-muted-foreground font-medium sticky top-0 z-10">
                <tr>
                  <th className="px-6 py-4 bg-muted">渠道名称</th>
                  <th className="px-6 py-4 bg-muted">健康状态</th>
                  <th className="px-6 py-4 bg-muted">请求数</th>
                  <th className="px-6 py-4 bg-muted">成功率</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {channelStats.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="px-6 py-8 text-center text-muted-foreground">暂无渠道数据</td>
                  </tr>
                ) : (
                  channelStats.map((channel, i) => {
                    const isHealthy = channel.success_rate >= 0.95;
                    const isWarning = channel.success_rate < 0.95 && channel.success_rate >= 0.8;
                    return (
                      <tr key={i} className="hover:bg-muted/50 transition-colors">
                        <td className="px-6 py-4 font-medium text-foreground">{channel.provider}</td>
                        <td className="px-6 py-4">
                          <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium border ${isHealthy ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-500 border-emerald-500/20' : isWarning ? 'bg-amber-500/10 text-amber-600 dark:text-amber-500 border-amber-500/20' : 'bg-red-500/10 text-red-600 dark:text-red-500 border-red-500/20'}`}>
                            {isHealthy ? <CheckCircle2 className="w-3.5 h-3.5" /> : <AlertCircle className="w-3.5 h-3.5" />}
                            {isHealthy ? '良好' : isWarning ? '警告' : '异常'}
                          </span>
                        </td>
                        <td className="px-6 py-4 text-muted-foreground">{channel.total_requests.toLocaleString()}</td>
                        <td className="px-6 py-4 font-mono font-bold">
                          <span className={isHealthy ? 'text-emerald-600 dark:text-emerald-500' : isWarning ? 'text-amber-600 dark:text-amber-500' : 'text-red-600 dark:text-red-500'}>
                            {(channel.success_rate * 100).toFixed(1)}%
                          </span>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* 用量分析 */}
      <div className="bg-card border border-border rounded-xl shadow-sm overflow-hidden">
        <button
          onClick={() => setAnalysisOpen(!analysisOpen)}
          className="w-full p-6 flex items-center justify-between hover:bg-muted/30 transition-colors"
        >
          <h3 className="text-base font-semibold text-foreground flex items-center gap-2">
            <BarChart3 className="w-4 h-4 text-blue-500" />
            用量分析
          </h3>
          {analysisOpen ? <ChevronUp className="w-5 h-5 text-muted-foreground" /> : <ChevronDown className="w-5 h-5 text-muted-foreground" />}
        </button>

        {analysisOpen && (
          <div className="px-6 pb-6 space-y-5 border-t border-border pt-5">
            {/* 筛选条件 */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {/* 开始时间 */}
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1.5">开始时间</label>
                <input
                  type="datetime-local"
                  value={analysisStart}
                  onChange={e => setAnalysisStart(e.target.value)}
                  className="w-full px-3 py-2 text-sm bg-background border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 text-foreground"
                />
              </div>
              {/* 结束时间 */}
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1.5">结束时间</label>
                <input
                  type="datetime-local"
                  value={analysisEnd}
                  onChange={e => setAnalysisEnd(e.target.value)}
                  className="w-full px-3 py-2 text-sm bg-background border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 text-foreground"
                />
              </div>
              {/* 时间范围快捷按钮 + 查询 */}
              <div className="flex items-end gap-2 pb-0.5">
                {[
                  { label: '1小时', hours: 1 },
                  { label: '24小时', hours: 24 },
                  { label: '7天', hours: 168 },
                  { label: '30天', hours: 720 },
                ].map(({ label, hours }) => (
                  <button
                    key={hours}
                    type="button"
                    onClick={() => {
                      const end = new Date();
                      const start = new Date(end.getTime() - hours * 60 * 60 * 1000);
                      const fmt = (d: Date) => {
                        const pad = (n: number) => String(n).padStart(2, '0');
                        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
                      };
                      setAnalysisStart(fmt(start));
                      setAnalysisEnd(fmt(end));
                    }}
                    className="px-2.5 py-1.5 text-xs font-medium bg-muted hover:bg-muted/80 text-foreground border border-border rounded-lg transition-colors"
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            {/* 渠道 & 模型多选 */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <MultiSelect
                label="渠道（多选）"
                options={availableProviders}
                selected={analysisProviders}
                onChange={setAnalysisProviders}
                placeholder="全部渠道"
              />
              <MultiSelect
                label="模型（多选）"
                options={availableModels}
                selected={analysisModels}
                onChange={setAnalysisModels}
                placeholder="全部模型"
              />
            </div>

            {/* 查询按钮 */}
            <div>
              <button
                onClick={fetchAnalysis}
                disabled={analysisLoading}
                className="px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors flex items-center gap-2 disabled:opacity-50"
              >
                {analysisLoading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                查询
              </button>
            </div>

            {/* 查询结果 */}
            {analysisQueried && (
              <div className="space-y-4">
                {/* 堆叠柱状图：请求次数 + Token 用量 */}
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                  {/* 请求次数折线图 */}
                  <div className="bg-muted/30 rounded-xl p-6 border border-border">
                    <h4 className="text-sm font-semibold text-foreground mb-4 flex items-center gap-2">
                      <Zap className="w-4 h-4 text-amber-500" />
                      请求次数趋势
                    </h4>
                    {trendLoading || analysisLoading ? (
                      <div className="h-64 flex items-center justify-center text-sm text-muted-foreground">
                        <RefreshCw className="w-4 h-4 animate-spin mr-2" />
                        加载中
                      </div>
                    ) : trendData.length > 0 && trendModels.length > 0 ? (
                      <div className="h-64">
                        <ResponsiveContainer width="100%" height="100%">
                          <LineChart data={trendData} margin={{ top: 5, right: 5, left: 5, bottom: 5 }}>
                            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--muted))" vertical={false} />
                            <XAxis dataKey="hour" stroke={AXIS_COLOR} fontSize={10} tickLine={false} axisLine={false} tickFormatter={(v: string) => formatTimeTick(v, trendGranularity)} />
                            <YAxis stroke={AXIS_COLOR} fontSize={10} tickLine={false} axisLine={false} tickFormatter={formatCompact} />
                            <Tooltip
                              contentStyle={tooltipStyle}
                              itemStyle={{ fontSize: '12px' }}
                              labelStyle={{ fontSize: '12px', fontWeight: 'bold', marginBottom: '4px' }}
                              labelFormatter={(v: string) => formatTimeTooltip(v, trendGranularity)}
                            />
                            <Legend iconType="circle" wrapperStyle={{ fontSize: '12px', paddingTop: '10px' }} />
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
                                activeDot={{ r: 4 }}
                              />
                            ))}
                          </LineChart>
                        </ResponsiveContainer>
                      </div>
                    ) : (
                      <div className="h-64 flex items-center justify-center text-sm text-muted-foreground">
                        暂无趋势数据
                      </div>
                    )}
                  </div>

                  {/* Token 用量堆叠柱状图 */}
                  <div className="bg-muted/30 rounded-xl p-6 border border-border">
                    <h4 className="text-sm font-semibold text-foreground mb-4 flex items-center gap-2">
                      <BarChart3 className="w-4 h-4 text-blue-500" />
                      Token 用量趋势
                    </h4>
                    {trendLoading || analysisLoading ? (
                      <div className="h-64 flex items-center justify-center text-sm text-muted-foreground">
                        <RefreshCw className="w-4 h-4 animate-spin mr-2" />
                        加载中
                      </div>
                    ) : tokenBarData.length > 0 ? (
                      <div className="h-64">
                        <ResponsiveContainer width="100%" height="100%">
                          <BarChart data={tokenBarData} margin={{ top: 5, right: 5, left: 5, bottom: 5 }}>
                            <XAxis dataKey="hour" stroke={AXIS_COLOR} fontSize={10} tickLine={false} axisLine={false} tickFormatter={(v: string) => formatTimeTick(v, trendGranularity)} />
                            <YAxis stroke={AXIS_COLOR} fontSize={10} tickLine={false} axisLine={false} tickFormatter={formatTokens} />
                            <Tooltip
                              contentStyle={tooltipStyle}
                              itemStyle={{ color: tooltipStyle.color }}
                              labelFormatter={(label: string) => {
                                const row = tokenBarData.find(r => r.hour === label);
                                const total = row ? (row.cached + row.uncached + row.output) : 0;
                                return `${formatTimeTooltip(label, trendGranularity)}    ${formatTokens(total)} tokens`;
                              }}
                              formatter={(value: number, name: string) => {
                                const labels: Record<string, string> = {
                                  cached: '输入（命中缓存）',
                                  uncached: '输入（未命中缓存）',
                                  output: '输出',
                                };
                                return [formatTokens(value), labels[name] || name];
                              }}
                            />
                            <Legend
                              formatter={(value: string) => {
                                const labels: Record<string, string> = {
                                  cached: '输入（命中缓存）',
                                  uncached: '输入（未命中缓存）',
                                  output: '输出',
                                };
                                return labels[value] || value;
                              }}
                              wrapperStyle={{ fontSize: '12px' }}
                            />
                            <Bar dataKey="output" stackId="tokens" name="output" fill="#1e3a5f" />
                            <Bar dataKey="uncached" stackId="tokens" name="uncached" fill="#1e40af" />
                            <Bar dataKey="cached" stackId="tokens" name="cached" fill="#93c5fd" radius={[4, 4, 0, 0]} />
                          </BarChart>
                        </ResponsiveContainer>
                      </div>
                    ) : (
                      <div className="h-64 flex items-center justify-center text-sm text-muted-foreground">
                        暂无 Token 数据
                      </div>
                    )}
                  </div>
                </div>

                {/* 汇总卡片 */}
                {analysisData.length > 0 && (
                  <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
                    <div className="bg-muted/50 rounded-lg p-3 text-center">
                      <p className="text-xs text-muted-foreground">总请求次数</p>
                      <p className="text-lg font-bold text-foreground mt-1">{formatCompact(analysisTotalRequests)}</p>
                    </div>
                    <div className="bg-muted/50 rounded-lg p-3 text-center">
                      <p className="text-xs text-muted-foreground">输入 Token</p>
                      <p className="text-lg font-bold text-foreground mt-1">{formatTokens(analysisTotalPrompt)}</p>
                    </div>
                    <div className="bg-muted/50 rounded-lg p-3 text-center">
                      <p className="text-xs text-muted-foreground">输出 Token</p>
                      <p className="text-lg font-bold text-foreground mt-1">{formatTokens(analysisTotalCompletion)}</p>
                    </div>
                    <div className="bg-muted/50 rounded-lg p-3 text-center">
                      <p className="text-xs text-muted-foreground">总 Token</p>
                      <p className="text-lg font-bold text-foreground mt-1">{formatTokens(analysisTotalTokensAll)}</p>
                    </div>
                    <div className="bg-amber-500/10 rounded-lg p-3 text-center" title="按请求发生时记录的价格统计">
                      <p className="text-xs text-muted-foreground">费用合计（USD）</p>
                      <p className="text-lg font-bold text-amber-600 dark:text-amber-400 mt-1">{formatCost(analysisTotalCost)}</p>
                    </div>
                  </div>
                )}

                {/* 结果表格 */}
                <div className="overflow-x-auto border border-border rounded-lg">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-muted text-muted-foreground font-medium">
                      <tr>
                        <th className="px-4 py-3">渠道</th>
                        <th className="px-4 py-3">模型</th>
                        <th className="px-4 py-3 text-right">请求次数</th>
                        <th className="px-4 py-3 text-right">输入 Token</th>
                        <th className="px-4 py-3 text-right">输出 Token</th>
                        <th className="px-4 py-3 text-right">总 Token</th>
                        <th className="px-4 py-3 text-right">费用（USD）</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {analysisData.length === 0 ? (
                        <tr>
                          <td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">
                            {analysisLoading ? '查询中...' : '暂无数据'}
                          </td>
                        </tr>
                      ) : (
                        analysisData.map((entry, i) => (
                          <tr key={i} className="hover:bg-muted/50 transition-colors">
                            <td className="px-4 py-3 font-medium text-foreground">{entry.provider}</td>
                            <td className="px-4 py-3 text-foreground font-mono text-xs">{entry.model}</td>
                            <td className="px-4 py-3 text-right text-muted-foreground">{formatCompact(entry.request_count)}</td>
                            <td className="px-4 py-3 text-right text-muted-foreground">{formatTokens(entry.total_prompt_tokens)}</td>
                            <td className="px-4 py-3 text-right text-muted-foreground">{formatTokens(entry.total_completion_tokens)}</td>
                            <td className="px-4 py-3 text-right font-mono font-bold text-foreground">{formatTokens(entry.total_tokens)}</td>
                            <td className="px-4 py-3 text-right font-mono text-amber-600 dark:text-amber-400">{formatCost(entry.recorded_cost)}</td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
