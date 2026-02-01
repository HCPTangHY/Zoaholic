import { useState, useEffect } from 'react';
import { useAuthStore } from '../store/authStore';
import {
  RefreshCw, Filter, ChevronDown, ChevronRight, FileText,
  Clock, ArrowDownToLine, CheckCircle2, XCircle,
  Globe, Key, Server, RotateCcw, Eye, EyeOff,
  Flag, Users, Zap, AlertTriangle
} from 'lucide-react';

// 匹配后端 LogEntry 模型
interface LogEntry {
  id: number;
  timestamp: string;
  endpoint?: string;
  client_ip?: string;
  provider?: string;
  model?: string;
  api_key_prefix?: string;
  process_time?: number;
  first_response_time?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  success: boolean;
  status_code?: number;
  is_flagged: boolean;
  // 扩展字段
  provider_id?: string;
  provider_key_index?: number;
  api_key_name?: string;
  api_key_group?: string;
  retry_count?: number;
  retry_path?: string;
  request_headers?: string;
  request_body?: string;
  upstream_request_body?: string;
  upstream_response_body?: string;
  response_body?: string;
  raw_data_expires_at?: string;
}

export default function Logs() {
  const { token } = useAuthStore();
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [totalCount, setTotalCount] = useState(0);

  // Pagination
  const [page, setPage] = useState(1);
  const [pageSize] = useState(50);

  // Search & Filter States
  const [filterModel, setFilterModel] = useState('');
  const [filterProvider, setFilterProvider] = useState('');
  const [filterSuccess, setFilterSuccess] = useState<string>('ALL');
  const [showFilters, setShowFilters] = useState(false);

  // Accordion State - 展开的日志ID集合
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());

  const fetchLogs = async (resetPage = false) => {
    if (!token) return;
    setLoading(true);

    const currentPage = resetPage ? 1 : page;
    if (resetPage) setPage(1);

    try {
      const queryParams = new URLSearchParams({
        page: currentPage.toString(),
        page_size: pageSize.toString(),
      });

      if (filterModel) queryParams.append('model', filterModel);
      if (filterProvider) queryParams.append('provider', filterProvider);
      if (filterSuccess === 'SUCCESS') queryParams.append('success', 'true');
      if (filterSuccess === 'FAILED') queryParams.append('success', 'false');

      const res = await fetch(`/v1/logs?${queryParams.toString()}`, {
        headers: { Authorization: `Bearer ${token}` }
      });

      if (res.ok) {
        const data = await res.json();
        const fetchedLogs = data.items || [];
        setLogs(fetchedLogs);
        setTotalCount(data.total || 0);
        setHasMore(currentPage * pageSize < (data.total || 0));
      }
    } catch (err) {
      console.error('Failed to fetch logs:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLogs(true);
  }, [filterModel, filterProvider, filterSuccess]);

  const loadMore = () => {
    setPage(prev => prev + 1);
  };

  useEffect(() => {
    if (page > 1) {
      fetchLogs();
    }
  }, [page]);

  // Toggle accordion
  const toggleExpand = (id: number) => {
    setExpandedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  // ========== Helpers ==========
  const getStatusColor = (success: boolean, code?: number) => {
    if (success) return 'text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/20';
    if (code && code >= 400 && code < 500) return 'text-yellow-600 dark:text-yellow-500 bg-yellow-500/10 border-yellow-500/20';
    return 'text-red-600 dark:text-red-500 bg-red-500/10 border-red-500/20';
  };

  const calculateSpeed = (log: LogEntry) => {
    if (!log.completion_tokens || !log.process_time) return null;

    const startTime = log.first_response_time || 0;
    const genTime = log.process_time - startTime;
    if (genTime <= 0) return null;

    const speed = log.completion_tokens / genTime;
    let color = 'text-muted-foreground';
    if (speed >= 80) color = 'text-purple-600 dark:text-purple-400';
    else if (speed >= 40) color = 'text-emerald-600 dark:text-emerald-400';
    else if (speed < 15) color = 'text-yellow-600 dark:text-yellow-500';

    return { speed: speed.toFixed(1), color };
  };

  const formatTimestamp = (ts: string) => {
    try {
      const date = new Date(ts);
      return date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      });
    } catch {
      return ts;
    }
  };

  const formatFullTimestamp = (ts: string) => {
    try {
      const date = new Date(ts);
      return date.toLocaleString('zh-CN');
    } catch {
      return ts;
    }
  };

  const formatJsonBestEffort = (raw: string) => {
    const input = String(raw ?? '').trim();
    if (!input) return { formatted: '', isJson: false };

    try {
      let parsed: any = JSON.parse(input);

      // 处理“双重序列化”的情况："{...}" / "[...]"
      if (typeof parsed === 'string') {
        const inner = parsed.trim();
        try {
          parsed = JSON.parse(inner);
        } catch {
          return { formatted: parsed, isJson: false };
        }
      }

      if (parsed === null) return { formatted: 'null', isJson: true };
      if (typeof parsed === 'object') return { formatted: JSON.stringify(parsed, null, 2), isJson: true };
      return { formatted: String(parsed), isJson: false };
    } catch {
      return { formatted: raw, isJson: false };
    }
  };

  const getHttpCodeColor = (code?: number | null) => {
    if (code == null) return 'text-muted-foreground bg-muted/30 border-border';
    if (code >= 200 && code < 300) return 'text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/20';
    if (code >= 400 && code < 500) return 'text-yellow-600 dark:text-yellow-500 bg-yellow-500/10 border-yellow-500/20';
    return 'text-red-600 dark:text-red-500 bg-red-500/10 border-red-500/20';
  };

  type RetryHop = { provider?: string; status_code?: number | null; error?: string };

  const RetryPathView = ({ retryPathJson }: { retryPathJson: string }) => {
    const [openIndex, setOpenIndex] = useState<number | null>(null);

    let items: RetryHop[] | null = null;
    try {
      const parsed = JSON.parse(retryPathJson);
      if (Array.isArray(parsed)) items = parsed;
    } catch {
      items = null;
    }

    if (!items) {
      return (
        <pre className="bg-background border border-border p-3 rounded-lg text-xs font-mono text-foreground overflow-x-auto whitespace-pre-wrap">
          {retryPathJson}
        </pre>
      );
    }

    if (items.length === 0) {
      return <div className="text-sm text-muted-foreground">无重试记录</div>;
    }

    return (
      <div className="space-y-2">
        {items.map((hop, idx) => {
          const provider = hop.provider || '-';
          const code = hop.status_code;
          const error = hop.error || '';
          const isOpen = openIndex === idx;
          const preview = error.length > 120 ? `${error.slice(0, 120)}...` : error;

          return (
            <div key={`${provider}-${idx}`} className="border border-border rounded-lg overflow-hidden bg-background">
              <button
                type="button"
                onClick={() => setOpenIndex(prev => (prev === idx ? null : idx))}
                className="w-full text-left px-3 py-2 flex items-start gap-2 hover:bg-muted/50 transition-colors"
              >
                <div className="flex-shrink-0 text-xs font-mono text-muted-foreground mt-0.5">
                  #{idx + 1}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-foreground truncate" title={provider}>{provider}</span>
                    <span className={`px-2 py-0.5 rounded text-xs font-mono border ${getHttpCodeColor(code)}`}>
                      {code ?? '-'}
                    </span>
                    {error && (
                      <span className="text-xs text-muted-foreground flex items-center gap-1">
                        <AlertTriangle className="w-3.5 h-3.5" />
                        <span className="truncate max-w-[520px]">{isOpen ? '展开查看错误详情' : preview}</span>
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex-shrink-0 text-muted-foreground mt-0.5">
                  {isOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                </div>
              </button>
              {isOpen && error && (
                <pre className="border-t border-border p-3 text-xs font-mono text-foreground whitespace-pre-wrap max-h-72 overflow-y-auto">
                  {error}
                </pre>
              )}
            </div>
          );
        })}
      </div>
    );
  };

  // 单条日志的手风琴组件
  const LogAccordionItem = ({ log }: { log: LogEntry }) => {
    const isExpanded = expandedIds.has(log.id);
    const speedInfo = calculateSpeed(log);

    return (
      <div className="bg-card border border-border rounded-xl overflow-hidden">
        {/* Header - 折叠状态显示关键信息 */}
        <div
          className="cursor-pointer hover:bg-muted/50 transition-colors"
          onClick={() => toggleExpand(log.id)}
        >
          {/* 第一行：核心信息 */}
          <div className="flex items-center gap-2 sm:gap-3 p-3 sm:p-4">
            {/* 展开图标 */}
            <div className="flex-shrink-0 text-muted-foreground">
              {isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            </div>

            {/* 状态图标 */}
            <div className="flex-shrink-0">
              {log.success ? (
                <CheckCircle2 className="w-5 h-5 text-emerald-500" />
              ) : (
                <XCircle className="w-5 h-5 text-red-500" />
              )}
            </div>

            {/* 时间 */}
            <div className="flex-shrink-0 text-xs sm:text-sm font-mono text-muted-foreground w-[85px] sm:w-[100px]">
              {formatTimestamp(log.timestamp)}
            </div>

            {/* 模型 & 渠道 */}
            <div className="flex-1 min-w-0">
              <div className="font-medium text-foreground text-sm truncate" title={log.model || '-'}>
                {log.model || '-'}
              </div>
              <div className="text-xs text-muted-foreground truncate">
                {log.provider || '未知'}
                {log.provider_key_index !== undefined && <span className="opacity-60"> [{log.provider_key_index}]</span>}
              </div>
            </div>

            {/* 标记 & 重试 */}
            <div className="hidden sm:flex items-center gap-1.5 flex-shrink-0">
              {log.is_flagged && (
                <span className="text-yellow-500" title="已标记">
                  <Flag className="w-4 h-4" />
                </span>
              )}
              {(log.retry_count ?? 0) > 0 && (
                <span className="text-orange-500 flex items-center gap-0.5 text-xs" title={`重试 ${log.retry_count} 次`}>
                  <RotateCcw className="w-3.5 h-3.5" />
                  {log.retry_count}
                </span>
              )}
            </div>

            {/* 状态码 */}
            <div className="flex-shrink-0">
              <span className={`px-1.5 sm:px-2 py-0.5 sm:py-1 rounded text-xs font-mono font-medium border ${getStatusColor(log.success, log.status_code)}`}>
                {log.status_code || '-'}
              </span>
            </div>
          </div>

          {/* 第二行：详细指标 */}
          <div className="flex items-center gap-2 sm:gap-4 px-3 sm:px-4 pb-3 sm:pb-4 pt-0 text-xs flex-wrap">
            {/* API Key */}
            <div className="flex items-center gap-1 text-muted-foreground" title={`API Key: ${log.api_key_name || log.api_key_prefix || '-'}`}>
              <Key className="w-3.5 h-3.5" />
              <span className="max-w-[80px] sm:max-w-[120px] truncate">{log.api_key_name || log.api_key_prefix || '-'}</span>
            </div>

            {/* 分组 */}
            {log.api_key_group && (
              <div className="hidden sm:flex items-center gap-1 text-muted-foreground">
                <Users className="w-3.5 h-3.5" />
                <span>{log.api_key_group}</span>
              </div>
            )}

            {/* IP */}
            <div className="hidden lg:flex items-center gap-1 text-muted-foreground font-mono">
              <Globe className="w-3.5 h-3.5" />
              <span>{log.client_ip || '-'}</span>
            </div>

            <div className="flex-1" />

            {/* Tokens */}
            <div className="flex items-center gap-1 font-mono">
              <span className="text-muted-foreground">{log.prompt_tokens || 0}</span>
              <span className="text-muted-foreground/50">+</span>
              <span className="text-blue-600 dark:text-blue-400">{log.completion_tokens || 0}</span>
              <span className="text-muted-foreground/50">=</span>
              <span className="text-foreground">{log.total_tokens || 0}</span>
            </div>

            {/* 耗时 */}
            <div className="flex items-center gap-1 text-muted-foreground" title={`总耗时: ${log.process_time?.toFixed(2)}s, 首响: ${log.first_response_time?.toFixed(2) || '-'}s`}>
              <Clock className="w-3.5 h-3.5" />
              <span className="font-mono">{log.process_time?.toFixed(2) || '-'}s</span>
              {log.first_response_time !== undefined && (
                <span className="text-muted-foreground/60 hidden sm:inline">(首响 {log.first_response_time.toFixed(2)}s)</span>
              )}
            </div>

            {/* 速度 */}
            {speedInfo && (
              <div className={`flex items-center gap-1 font-mono ${speedInfo.color}`} title="生成速度">
                <Zap className="w-3.5 h-3.5" />
                <span>{speedInfo.speed} t/s</span>
              </div>
            )}
          </div>
        </div>

        {/* Expanded Content - 展开后显示原始数据 */}
        {isExpanded && (
          <div className="border-t border-border bg-muted/30 p-4 space-y-4">
            {/* 补充信息 */}
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 text-sm">
              <InfoItem label="日志 ID" value={String(log.id)} mono />
              <InfoItem label="完整时间" value={formatFullTimestamp(log.timestamp)} />
              <InfoItem label="Endpoint" value={log.endpoint || '-'} mono />
              <InfoItem label="客户端 IP" value={log.client_ip || '-'} mono />
              <InfoItem label="Provider ID" value={log.provider_id || '-'} />
              {log.raw_data_expires_at && (
                <InfoItem label="数据过期" value={formatFullTimestamp(log.raw_data_expires_at)} />
              )}
            </div>

            {/* 重试路径 */}
            {log.retry_path && (
              <div className="space-y-1">
                <div className="text-xs font-medium text-muted-foreground flex items-center gap-1">
                  <RotateCcw className="w-3.5 h-3.5" /> 重试路径
                </div>
                <RetryPathView retryPathJson={log.retry_path} />
              </div>
            )}

            {/* 请求/响应数据 - 手风琴形式并列 */}
            <div className="space-y-2">
              {/* 1. 请求头 */}
              <JsonAccordion
                title="请求头"
                data={log.request_headers}
                icon={<FileText className="w-4 h-4" />}
              />

              {/* 2. 用户请求体 */}
              <JsonAccordion
                title="用户请求体"
                data={log.request_body}
                icon={<Eye className="w-4 h-4" />}
              />

              {/* 3. 上游请求体 */}
              <JsonAccordion
                title="上游请求体"
                data={log.upstream_request_body}
                icon={<Server className="w-4 h-4" />}
              />

              {/* 4. 上游响应体 */}
              <JsonAccordion
                title="上游响应体"
                data={log.upstream_response_body}
                icon={<Server className="w-4 h-4" />}
              />

              {/* 5. 用户响应体 */}
              <JsonAccordion
                title="用户响应体"
                data={log.response_body}
                icon={<EyeOff className="w-4 h-4" />}
              />
            </div>
          </div>
        )}
      </div>
    );
  };

  // 信息项组件
  const InfoItem = ({ label, value, mono }: { label: string; value: string; mono?: boolean }) => (
    <div className="space-y-0.5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-sm text-foreground truncate ${mono ? 'font-mono' : ''}`} title={value}>
        {value}
      </div>
    </div>
  );

  // JSON 手风琴展示块组件
  const JsonAccordion = ({ title, data, icon, defaultOpen = false }: { title: string; data?: string; icon?: import('react').ReactNode; defaultOpen?: boolean }) => {
    const [isOpen, setIsOpen] = useState(defaultOpen);

    if (!data) return null;

    const { formatted } = formatJsonBestEffort(data);

    // 计算预览文本
    const previewText = formatted.length > 80 ? formatted.substring(0, 80) + '...' : formatted;

    return (
      <div className="border border-border rounded-lg overflow-hidden">
        <div
          className="flex items-center gap-2 px-3 py-2 bg-muted/50 cursor-pointer hover:bg-muted transition-colors"
          onClick={() => setIsOpen(!isOpen)}
        >
          <div className="flex-shrink-0 text-muted-foreground">
            {isOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          </div>
          <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
            {icon}
            {title}
          </div>
          {!isOpen && (
            <div className="flex-1 text-xs font-mono text-muted-foreground/60 truncate ml-2">
              {previewText.replace(/\n/g, ' ')}
            </div>
          )}
        </div>
        {isOpen && (
          <pre className="bg-background p-3 text-xs font-mono text-foreground overflow-x-auto whitespace-pre-wrap max-h-80 overflow-y-auto border-t border-border">
            {formatted}
          </pre>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-4 sm:space-y-6 animate-in fade-in duration-500 font-sans pb-12 h-full flex flex-col">
      {/* Header */}
      <div className="flex justify-between items-center flex-shrink-0">
        <div>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-foreground">系统日志</h1>
          <p className="text-muted-foreground mt-1 text-sm sm:text-base">监控 API 请求详情与性能</p>
        </div>
        <button
          onClick={() => fetchLogs(true)}
          className="p-2 text-muted-foreground hover:text-foreground bg-card border border-border rounded-lg transition-colors"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* Toolbar */}
      <div className="bg-card border border-border p-3 sm:p-4 rounded-xl shadow-sm space-y-3 flex-shrink-0">
        {/* Mobile Filter Toggle */}
        <button
          onClick={() => setShowFilters(!showFilters)}
          className="flex items-center gap-2 text-sm text-muted-foreground md:hidden w-full justify-center py-1"
        >
          <Filter className="w-4 h-4" />
          {showFilters ? '收起筛选' : '展开筛选'}
          <ChevronDown className={`w-4 h-4 transition-transform ${showFilters ? 'rotate-180' : ''}`} />
        </button>

        {/* Filters - Always show on desktop, toggle on mobile */}
        <div className={`flex flex-col sm:flex-row gap-3 ${showFilters ? 'block' : 'hidden md:flex'}`}>
          <select
            value={filterSuccess}
            onChange={e => setFilterSuccess(e.target.value)}
            className="bg-background border border-border text-sm px-3 py-2 rounded-lg text-foreground flex-1 sm:flex-none"
          >
            <option value="ALL">所有状态</option>
            <option value="SUCCESS">成功</option>
            <option value="FAILED">失败</option>
          </select>

          <input
            type="text"
            placeholder="模型过滤"
            value={filterModel}
            onChange={e => setFilterModel(e.target.value)}
            className="bg-background border border-border text-sm px-3 py-2 rounded-lg text-foreground flex-1 sm:w-32 sm:flex-none"
          />

          <input
            type="text"
            placeholder="渠道过滤"
            value={filterProvider}
            onChange={e => setFilterProvider(e.target.value)}
            className="bg-background border border-border text-sm px-3 py-2 rounded-lg text-foreground flex-1 sm:w-32 sm:flex-none"
          />

          <div className="text-xs text-muted-foreground self-center">
            共 {totalCount} 条记录
          </div>
        </div>
      </div>

      {/* Logs List - Accordion */}
      <div className="flex-1 overflow-auto space-y-2">
        {logs.length === 0 && !loading ? (
          <div className="flex flex-col items-center justify-center p-16 text-muted-foreground bg-card border border-border rounded-xl">
            <FileText className="w-12 h-12 mb-4 opacity-50" />
            <p>未找到匹配的日志</p>
          </div>
        ) : (
          logs.map((log) => <LogAccordionItem key={log.id} log={log} />)
        )}

        {/* Load More */}
        {hasMore && logs.length > 0 && (
          <button
            onClick={loadMore}
            disabled={loading}
            className="w-full text-sm text-muted-foreground hover:text-foreground font-medium flex items-center justify-center gap-1.5 py-4 bg-card border border-border rounded-xl disabled:opacity-50 transition-colors"
          >
            <ArrowDownToLine className="w-4 h-4" />
            {loading ? '加载中...' : `加载更多 (${logs.length}/${totalCount})`}
          </button>
        )}
      </div>
    </div>
  );
}
