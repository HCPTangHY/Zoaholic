import { useEffect, useState } from 'react';
import {
  Activity, Cpu, Zap, BarChart3, AlertCircle, CheckCircle2,
  RefreshCw, Server, ChevronDown, ChevronUp, DollarSign, Search, X
} from 'lucide-react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
  PieChart, Pie, Legend
} from 'recharts';
import {
  LineChart, Line, CartesianGrid
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
}

interface RowPrice {
  prompt: number;
  completion: number;
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

// 折线图专用色板，颜色间差异大，避免混淆
const LINE_COLORS = [
  '#3b82f6', // 蓝
  '#ef4444', // 红
  '#22c55e', // 绿
  '#f59e0b', // 橙
  '#8b5cf6', // 紫
  '#ec4899', // 粉
  '#06b6d4', // 青
  '#84cc16', // 黄绿
];

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

/** 根据 model_price 配置，按最长前缀匹配返回 {prompt, completion}，未命中返回 null */
function matchModelPrice(modelPrice: Record<string, string>, modelName: string): { prompt: number; completion: number } | null {
  if (!modelName || !modelPrice || Object.keys(modelPrice).length === 0) return null;
  const matched = Object.keys(modelPrice)
    .filter(k => k !== 'default' && modelName.startsWith(k))
    .sort((a, b) => b.length - a.length);
  const key = matched.length > 0 ? matched[0] : (modelPrice['default'] !== undefined ? 'default' : null);
  if (!key) return null;
  const parts = String(modelPrice[key] || '').split(',').map(s => s.trim());
  const prompt = parseFloat(parts[0]) || 0;
  const completion = parseFloat(parts[1]) || 0;
  return { prompt, completion };
}

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

export default function Dashboard() {
  const [stats, setStats] = useState<StatData | null>(null);
  const [totalTokens, setTotalTokens] = useState(0);
  const [totalCost, setTotalCost] = useState(0);
  const [globalModelPrice, setGlobalModelPrice] = useState<Record<string, string>>({});
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
  const [analysisOpen, setAnalysisOpen] = useState(false);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisData, setAnalysisData] = useState<AnalysisEntry[]>([]);
  const [analysisProviders, setAnalysisProviders] = useState<string[]>([]);
  const [analysisModels, setAnalysisModels] = useState<string[]>([]);
  const [analysisStart, setAnalysisStart] = useState('');
  const [analysisEnd, setAnalysisEnd] = useState('');
  const [defaultPromptPrice, setDefaultPromptPrice] = useState(0.3);
  const [defaultCompletionPrice, setDefaultCompletionPrice] = useState(1.0);
  const [rowPrices, setRowPrices] = useState<Record<number, RowPrice>>({});
  const [analysisQueried, setAnalysisQueried] = useState(false);
  const [trendData, setTrendData] = useState<Record<string, string | number>[]>([]);
  const [trendModels, setTrendModels] = useState<string[]>([]);
  const [trendLoading, setTrendLoading] = useState(false);

  const fetchData = async () => {
    if (!token) return;
    setLoading(true);
    try {

      const statsRes = await apiFetch(`/v1/stats?hours=${timeRange}`);
      if (statsRes.ok) {
        const data = await statsRes.json();
        setStats(data.stats || data);
        setTotalCost((data.stats || data).total_cost || 0);
      }

      const end = new Date();
      const start = new Date(end.getTime() - timeRange * 60 * 60 * 1000);
      const tokenUrl = `/v1/token_usage?start_datetime=${encodeURIComponent(start.toISOString())}&end_datetime=${encodeURIComponent(end.toISOString())}`;

      const tokenRes = await apiFetch(tokenUrl);
      if (tokenRes.ok) {
        const data = await tokenRes.json();
        const total = data.usage?.reduce((sum: number, item: { total_tokens?: number }) => sum + (item.total_tokens || 0), 0) || 0;
        setTotalTokens(total);
      }

      // 获取全局 model_price 配置
      try {
        const configRes = await apiFetch('/v1/api_config');
        if (configRes.ok) {
          const cfgData = await configRes.json();
          const prefs = cfgData.preferences || cfgData.api_config?.preferences || {};
          setGlobalModelPrice(prefs.model_price || {});
        }
      } catch { /* ignore */ }
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
    setTrendData([]);
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
        params.set('provider', analysisProviders.join(','));
      }
      if (analysisModels.length > 0) {
        params.set('model', analysisModels.join(','));
      }

      const res = await apiFetch(`/v1/stats/usage_analysis?${params}`);
      const trendResPromise = apiFetch(`/v1/stats/model_trend?${params}`);

      if (res.ok) {
        const result = await res.json();
        const data: AnalysisEntry[] = result.data || [];
        
        setAnalysisData(prevData => {
          // 修复价格保留逻辑：根据 provider+model 匹配旧价格
          const newPrices: Record<number, RowPrice> = {};
          data.forEach((entry, i) => {
            const key = `${entry.provider}:${entry.model}`;
            const oldIdx = prevData.findIndex(p => `${p.provider}:${p.model}` === key);
            if (oldIdx !== -1 && rowPrices[oldIdx]) {
              newPrices[i] = rowPrices[oldIdx];
            } else {
              // 自动从全局 model_price 前缀匹配填充
              const auto = matchModelPrice(globalModelPrice, entry.model);
              newPrices[i] = auto || { prompt: defaultPromptPrice, completion: defaultCompletionPrice };
            }
          });
          setRowPrices(newPrices);
          return data;
        });
      }

      setTrendLoading(true);
      const trendRes = await trendResPromise;
      if (trendRes.ok) {
        const contentType = trendRes.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
          console.error('Trend API did not return JSON. The backend may not be restarted and the request likely fell back to index.html.');
          setTrendData([]);
          setTrendModels([]);
        } else {
          const trendResult = await trendRes.json();
          setTrendData(trendResult.data ||[]);
          setTrendModels(trendResult.models || []);
        }
      } else {
        const text = await trendRes.text().catch(() => '');
        console.error('Trend API request failed:', trendRes.status, text.slice(0, 200));
        setTrendData([]);
        setTrendModels([]);
      }
    } catch (err) {
      console.error('Failed to fetch analysis:', err);
      setTrendData([]);
      setTrendModels([]);
    } finally {
      setAnalysisLoading(false);
      setTrendLoading(false);
    }
  };

  const applyDefaultPricesToAll = () => {
    const prices: Record<number, RowPrice> = {};
    analysisData.forEach((_, i) => {
      const auto = matchModelPrice(globalModelPrice, analysisData[i].model);
      prices[i] = auto || { prompt: defaultPromptPrice, completion: defaultCompletionPrice };
    });
    setRowPrices(prices);
  };

  const updateRowPrice = (index: number, field: 'prompt' | 'completion', value: number) => {
    setRowPrices(prev => ({
      ...prev,
      [index]: { ...prev[index], [field]: value }
    }));
  };

  useEffect(() => {
    fetchData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, timeRange]);

  const channelStats = stats?.channel_success_rates || [];
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
  const analysisTotalCost = analysisData.reduce((s, entry, i) => {
    const p = rowPrices[i] || { prompt: defaultPromptPrice, completion: defaultCompletionPrice };
    return s + (entry.total_prompt_tokens * p.prompt + entry.total_completion_tokens * p.completion) / 1_000_000;
  }, 0);

  const topCards = [
    { label: '总请求量', value: totalRequests.toLocaleString(), icon: Zap, color: 'text-amber-500', bg: 'bg-amber-500/10' },
    { label: `Token 消耗 (${timeRangeLabel})`, value: totalTokens.toLocaleString(), icon: BarChart3, color: 'text-blue-500', bg: 'bg-blue-500/10' },
    { label: '平均成功率', value: `${(avgSuccessRate * 100).toFixed(1)}%`, icon: CheckCircle2, color: 'text-emerald-500', bg: 'bg-emerald-500/10' },
    { label: '活跃渠道', value: activeChannels.toString(), icon: Cpu, color: 'text-purple-500', bg: 'bg-purple-500/10' },
    { label: `费用 (${timeRangeLabel})`, value: formatCost(totalCost), icon: DollarSign, color: 'text-amber-500', bg: 'bg-amber-500/10' },
  ];

  const formattedEndpointStats = endpointStats.slice(0, 5).map(item => ({
    name: item.endpoint.replace('POST ', '').replace('GET ', ''),
    value: item.count
  }));

  const formattedChannelStats = channelStats.slice(0, 6).map(item => ({
    name: item.provider,
    success_rate: item.success_rate * 100,
    requests: item.total_requests
  }));

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
                <h3 className="text-3xl font-bold text-foreground mt-2">{stat.value}</h3>
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
          <div className="overflow-x-auto flex-1">
            <table className="w-full text-left text-sm">
              <thead className="bg-muted text-muted-foreground font-medium">
                <tr>
                  <th className="px-6 py-4">渠道名称</th>
                  <th className="px-6 py-4">健康状态</th>
                  <th className="px-6 py-4">请求数</th>
                  <th className="px-6 py-4">成功率</th>
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

      {/* 用量分析与费用模拟 */}
      <div className="bg-card border border-border rounded-xl shadow-sm overflow-hidden">
        <button
          onClick={() => setAnalysisOpen(!analysisOpen)}
          className="w-full p-6 flex items-center justify-between hover:bg-muted/30 transition-colors"
        >
          <h3 className="text-base font-semibold text-foreground flex items-center gap-2">
            <DollarSign className="w-4 h-4 text-amber-500" />
            用量分析与费用模拟
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
              {/* 时间范围快捷按钮 */}
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

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {/* 默认输入价格 */}
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1.5">默认输入价格 ($/M)</label>
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  value={defaultPromptPrice}
                  onChange={e => setDefaultPromptPrice(parseFloat(e.target.value) || 0)}
                  className="w-full px-3 py-2 text-sm bg-background border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 text-foreground"
                />
              </div>
              {/* 默认输出价格 */}
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1.5">默认输出价格 ($/M)</label>
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  value={defaultCompletionPrice}
                  onChange={e => setDefaultCompletionPrice(parseFloat(e.target.value) || 0)}
                  className="w-full px-3 py-2 text-sm bg-background border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 text-foreground"
                />
              </div>
              {/* 查询按钮 */}
              <div className="flex items-end gap-2">
                <button
                  onClick={fetchAnalysis}
                  disabled={analysisLoading}
                  className="flex-1 px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors flex items-center justify-center gap-2 disabled:opacity-50"
                >
                  {analysisLoading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                  查询
                </button>
              </div>
            </div>

            {/* 查询结果 */}
            {analysisQueried && (
              <div className="space-y-4">
                {/* 模型趋势折线图 */}
                <div className="bg-muted/30 rounded-xl p-6 border border-border">
                  <h4 className="text-sm font-semibold text-foreground mb-4 flex items-center gap-2">
                    <Activity className="w-4 h-4 text-primary" />
                    所选模型请求频率趋势（按小时）
                  </h4>
                  <p className="text-xs text-muted-foreground mb-4">
                    如果这里持续显示“暂无趋势数据”，且浏览器控制台提示返回了 HTML，请重启后端服务，使新增的统计接口生效。
                  </p>
                  {trendLoading ? (
                    <div className="h-64 flex items-center justify-center text-sm text-muted-foreground">
                      <RefreshCw className="w-4 h-4 animate-spin mr-2" />
                      正在加载趋势数据
                    </div>
                  ) : trendData.length > 0 && trendModels.length > 0 ? (
                    <div className="h-64">
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={trendData}>
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
                          <YAxis stroke={AXIS_COLOR} fontSize={10} />
                          <Tooltip
                            contentStyle={tooltipStyle}
                            itemStyle={{ fontSize: '12px' }}
                            labelStyle={{ fontSize: '12px', fontWeight: 'bold', marginBottom: '4px' }}
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
                      当前筛选条件下暂无趋势数据
                    </div>
                  )}
                </div>

                {/* 汇总卡片 */}
                {analysisData.length > 0 && (
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                    <div className="bg-muted/50 rounded-lg p-3 text-center">
                      <p className="text-xs text-muted-foreground">总请求次数</p>
                      <p className="text-lg font-bold text-foreground mt-1">{analysisTotalRequests.toLocaleString()}</p>
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
                    <div className="bg-amber-500/10 rounded-lg p-3 text-center border border-amber-500/20">
                      <p className="text-xs text-amber-600 dark:text-amber-400">模拟总费用</p>
                      <p className="text-lg font-bold text-amber-600 dark:text-amber-400 mt-1">{formatCost(analysisTotalCost)}</p>
                    </div>
                  </div>
                )}

                {/* 应用默认价格按钮 */}
                {analysisData.length > 0 && (
                  <div className="flex items-center gap-3">
                    <button
                      onClick={applyDefaultPricesToAll}
                      className="px-3 py-1.5 text-xs font-medium bg-muted hover:bg-muted/80 text-foreground border border-border rounded-lg transition-colors"
                    >
                      将默认价格应用到所有行
                    </button>
                    <span className="text-xs text-muted-foreground">可在表格中逐行调整每个模型的价格</span>
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
                        <th className="px-4 py-3 text-center">输入价格 ($/M)</th>
                        <th className="px-4 py-3 text-center">输出价格 ($/M)</th>
                        <th className="px-4 py-3 text-right">模拟费用</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {analysisData.length === 0 ? (
                        <tr>
                          <td colSpan={8} className="px-4 py-8 text-center text-muted-foreground">
                            {analysisLoading ? '查询中...' : '暂无数据'}
                          </td>
                        </tr>
                      ) : (
                        analysisData.map((entry, i) => {
                          const p = rowPrices[i] || { prompt: defaultPromptPrice, completion: defaultCompletionPrice };
                          const rowCost = (entry.total_prompt_tokens * p.prompt + entry.total_completion_tokens * p.completion) / 1_000_000;
                          return (
                            <tr key={i} className="hover:bg-muted/50 transition-colors">
                              <td className="px-4 py-3 font-medium text-foreground">{entry.provider}</td>
                              <td className="px-4 py-3 text-foreground font-mono text-xs">{entry.model}</td>
                              <td className="px-4 py-3 text-right text-muted-foreground">{entry.request_count.toLocaleString()}</td>
                              <td className="px-4 py-3 text-right text-muted-foreground">{entry.total_prompt_tokens.toLocaleString()}</td>
                              <td className="px-4 py-3 text-right text-muted-foreground">{entry.total_completion_tokens.toLocaleString()}</td>
                              <td className="px-2 py-1 text-center">
                                <input
                                  type="number"
                                  step="0.01"
                                  min="0"
                                  value={p.prompt}
                                  onChange={e => updateRowPrice(i, 'prompt', parseFloat(e.target.value) || 0)}
                                  className="w-20 px-2 py-1 text-xs text-center bg-background border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary/50 text-foreground"
                                />
                              </td>
                              <td className="px-2 py-1 text-center">
                                <input
                                  type="number"
                                  step="0.01"
                                  min="0"
                                  value={p.completion}
                                  onChange={e => updateRowPrice(i, 'completion', parseFloat(e.target.value) || 0)}
                                  className="w-20 px-2 py-1 text-xs text-center bg-background border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary/50 text-foreground"
                                />
                              </td>
                              <td className="px-4 py-3 text-right font-mono font-bold text-amber-600 dark:text-amber-400">{formatCost(rowCost)}</td>
                            </tr>
                          );
                        })
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
