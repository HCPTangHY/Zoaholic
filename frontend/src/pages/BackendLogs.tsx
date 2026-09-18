import { useEffect, useMemo, useRef, useState } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import {
  RefreshCw,
  Search,
  Server,
  Play,
  Pause,
  Terminal,
  AlertTriangle,
  ArrowDown,
  Save,
  Settings2,
  ChevronDown,
  ChevronRight,
  Bug,
  Info,
  CircleAlert,
  AlertOctagon,
} from 'lucide-react';

type BackendLogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL';
type BackendLogSource = 'stream' | 'logger';

interface BackendLogEntry {
  id: number;
  captured_at: string;
  stream: 'stdout' | 'stderr';
  level?: BackendLogLevel | null;
  logger_name?: string | null;
  source: BackendLogSource;
  message: string;
}

interface BackendLogsResponse {
  items: BackendLogEntry[];
  total: number;
  limit: number;
  max_id: number;
  buffer_size: number;
}

const POLL_INTERVAL_MS = 3000;
const DEFAULT_PAGE_SIZE = 200;
const DEFAULT_BUFFER_SIZE = 200;
const PAGE_SIZE_OPTIONS = [50, 100, 200, 400, 800];
const BUFFER_SIZE_OPTIONS = [200, 400, 800, 1200, 2000, 5000];
const LOGGER_CHIP_COLLAPSED_LIMIT = 12;
const LEVEL_OPTIONS: BackendLogLevel[] = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

function getLevelMeta(level?: BackendLogLevel | null) {
  switch (level) {
    case 'DEBUG':
      return {
        label: 'DEBUG',
        icon: Bug,
        badgeClasses: 'text-slate-700 dark:text-slate-200 bg-slate-500/15 border-slate-500/30',
        rowClasses: 'border-l-slate-500/50 bg-slate-500/[0.07]',
      };
    case 'INFO':
      return {
        label: 'INFO',
        icon: Info,
        badgeClasses: 'text-sky-700 dark:text-sky-300 bg-sky-500/15 border-sky-500/30',
        rowClasses: 'border-l-sky-500/50 bg-sky-500/[0.07]',
      };
    case 'WARNING':
      return {
        label: 'WARNING',
        icon: AlertTriangle,
        badgeClasses: 'text-amber-800 dark:text-amber-300 bg-amber-500/15 border-amber-500/30',
        rowClasses: 'border-l-amber-500/60 bg-amber-500/[0.08]',
      };
    case 'ERROR':
      return {
        label: 'ERROR',
        icon: CircleAlert,
        badgeClasses: 'text-rose-700 dark:text-rose-300 bg-rose-500/15 border-rose-500/30',
        rowClasses: 'border-l-rose-500/70 bg-rose-500/[0.08]',
      };
    case 'CRITICAL':
      return {
        label: 'CRITICAL',
        icon: AlertOctagon,
        badgeClasses: 'text-red-700 dark:text-red-300 bg-red-500/15 border-red-500/35 shadow-[inset_0_0_0_1px_rgba(239,68,68,0.15)]',
        rowClasses: 'border-l-red-600/80 bg-red-500/[0.10]',
      };
    default:
      return {
        label: 'STREAM',
        icon: Terminal,
        badgeClasses: 'text-muted-foreground bg-muted border-border',
        rowClasses: 'border-l-border',
      };
  }
}

function getStreamBadgeClasses(stream: BackendLogEntry['stream']) {
  return stream === 'stderr'
    ? 'text-red-700 dark:text-red-300 bg-red-500/12 border-red-500/25'
    : 'text-emerald-700 dark:text-emerald-300 bg-emerald-500/12 border-emerald-500/25';
}

function getSourceLabel(item: BackendLogEntry) {
  if (item.source === 'logger' && item.logger_name) {
    return item.logger_name;
  }
  if (item.source === 'logger') {
    return 'logger';
  }
  return 'raw stream';
}

function formatTime(ts: string) {
  try {
    return new Date(ts).toLocaleString('zh-CN', {
      hour12: false,
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return ts;
  }
}

function normalizePositiveInt(value: unknown, fallback: number) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export default function BackendLogs() {
  const { token } = useAuthStore();
  const [items, setItems] = useState<BackendLogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);
  const [error, setError] = useState('');
  const [errorOnlyMode, setErrorOnlyMode] = useState(false);
  const [loggerFilter, setLoggerFilter] = useState('ALL');
  const [loggerSearchText, setLoggerSearchText] = useState('');
  const [showAllLoggerOptions, setShowAllLoggerOptions] = useState(false);
  const [configMessage, setConfigMessage] = useState('');
  const [search, setSearch] = useState('');
  const [levelFilter, setLevelFilter] = useState<'ALL' | BackendLogLevel>('ALL');
  const [streamFilter, setStreamFilter] = useState<'ALL' | 'stdout' | 'stderr'>('ALL');
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [bufferSize, setBufferSize] = useState(DEFAULT_BUFFER_SIZE);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [showConfig, setShowConfig] = useState(false);
  const [total, setTotal] = useState(0);
  const [maxId, setMaxId] = useState(0);
  const [effectiveBufferSize, setEffectiveBufferSize] = useState(DEFAULT_BUFFER_SIZE);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const pendingScrollToBottomRef = useRef(false);
  const isNearBottomRef = useRef(true);

  const loggerOptions = useMemo(() => {
    const names = new Set<string>();
    items.forEach(item => {
      const name = item.logger_name?.trim();
      if (name) names.add(name);
    });
    if (loggerFilter !== 'ALL') names.add(loggerFilter);
    return Array.from(names).sort((a, b) => a.localeCompare(b, 'zh-CN'));
  }, [items, loggerFilter]);

  const hasLoggerFilter = loggerFilter !== 'ALL';

  const filteredLoggerOptions = useMemo(() => {
    const keyword = loggerSearchText.trim().toLowerCase();
    if (!keyword) return loggerOptions;
    return loggerOptions.filter(name => name.toLowerCase().includes(keyword));
  }, [loggerOptions, loggerSearchText]);

  const visibleLoggerOptions = showAllLoggerOptions
    ? filteredLoggerOptions
    : filteredLoggerOptions.slice(0, LOGGER_CHIP_COLLAPSED_LIMIT);

  const hasHiddenLoggerOptions = filteredLoggerOptions.length > LOGGER_CHIP_COLLAPSED_LIMIT;

  const scrollToBottom = () => {
    if (!scrollerRef.current) return;
    scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
  };

  const checkNearBottom = () => {
    const el = scrollerRef.current;
    if (!el) return;
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const fetchBackendLogSettings = async () => {
    if (!token) return;

    try {
      const res = await apiFetch('/v1/api_config');
      if (!res.ok) return;

      const data = await res.json();
      const preferences = data.api_config?.preferences || data.preferences || {};
      const serverPageSize = normalizePositiveInt(preferences.backend_logs_page_size, DEFAULT_PAGE_SIZE);
      const serverBufferSize = normalizePositiveInt(preferences.backend_log_buffer_size, DEFAULT_BUFFER_SIZE);

      setPageSize(serverPageSize);
      setBufferSize(serverBufferSize);
    } catch (err) {
      console.error('Failed to load backend log settings:', err);
    }
  };

  const fetchBackendLogs = async ({ scrollAfterLoad = false }: { scrollAfterLoad?: boolean } = {}) => {
    if (!token) return;
    setLoading(true);
    setError('');
    pendingScrollToBottomRef.current = scrollAfterLoad;

    try {
      const queryParams = new URLSearchParams({
        limit: String(pageSize),
      });

      if (search.trim()) queryParams.set('search', search.trim());
      if (streamFilter !== 'ALL') queryParams.set('stream', streamFilter);
      if (errorOnlyMode) queryParams.set('level_group', 'errors');
      if (levelFilter !== 'ALL') queryParams.set('level', levelFilter);
      if (loggerFilter !== 'ALL') queryParams.set('logger_name', loggerFilter);

      const res = await apiFetch(`/v1/backend_logs?${queryParams.toString()}`);
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `请求失败 (${res.status})`);
      }

      const data: BackendLogsResponse = await res.json();
      setItems(data.items || []);
      setTotal(data.total || 0);
      setMaxId(data.max_id || 0);
      setEffectiveBufferSize(data.buffer_size || bufferSize);
    } catch (err) {
      pendingScrollToBottomRef.current = false;
      setError(err instanceof Error ? err.message : '获取后台日志失败');
    } finally {
      setLoading(false);
    }
  };

  const toggleErrorOnlyMode = () => {
    setErrorOnlyMode(prev => {
      const next = !prev;
      if (next && !['ALL', 'ERROR', 'CRITICAL'].includes(levelFilter)) {
        setLevelFilter('ALL');
      }
      return next;
    });
  };

  const handlePageSizeChange = (value: number) => {
    setPageSize(value);
    setConfigMessage('');
  };

  const handleBufferSizeChange = (value: number) => {
    setBufferSize(value);
    setConfigMessage('');
  };

  const saveBackendLogSettings = async () => {
    if (!token) return;

    const normalizedPageSize = pageSize;
    const normalizedBufferSize = bufferSize;

    setSavingConfig(true);
    setConfigMessage('');
    setError('');

    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          preferences: {
            backend_logs_page_size: normalizedPageSize,
            backend_log_buffer_size: normalizedBufferSize,
          },
        }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `保存失败 (${res.status})`);
      }

      setConfigMessage('显示配置已保存到服务端');
      await fetchBackendLogs();
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存显示配置失败');
    } finally {
      setSavingConfig(false);
    }
  };

  useEffect(() => {
    fetchBackendLogSettings();
  }, [token]);

  useEffect(() => {
    fetchBackendLogs({ scrollAfterLoad: true });
  }, [token, search, errorOnlyMode, levelFilter, streamFilter, loggerFilter, pageSize]);

  useEffect(() => {
    if (!autoRefresh || !token) return;
    const timer = window.setInterval(() => {
      fetchBackendLogs({ scrollAfterLoad: isNearBottomRef.current });
    }, POLL_INTERVAL_MS);

    return () => window.clearInterval(timer);
  }, [autoRefresh, token, search, errorOnlyMode, levelFilter, streamFilter, loggerFilter, pageSize]);

  useEffect(() => {
    if (!pendingScrollToBottomRef.current) return;
    scrollToBottom();
    pendingScrollToBottomRef.current = false;
  }, [items]);

  useEffect(() => {
    setShowAllLoggerOptions(false);
  }, [loggerSearchText]);

  const isBufferSmallerThanPage = bufferSize < pageSize;

  return (
    <div className="h-full min-h-0 min-w-0 flex flex-col gap-4 sm:gap-6 animate-in fade-in duration-500">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between flex-shrink-0">
        <div className="min-w-0">
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-foreground">后台日志</h1>
          <p className="text-muted-foreground mt-1 text-sm sm:text-base">
            查看当前实例的 stdout / stderr 输出，并按日志级别与 logger 来源区分，适合 Render、Railway 等平台快速排查部署日志。
          </p>
        </div>

        <div className="flex items-center gap-2 self-start sm:self-auto">
          <button
            type="button"
            onClick={() => setAutoRefresh(prev => !prev)}
            className={`inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm border transition-colors ${
              autoRefresh
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-card text-foreground border-border hover:bg-muted'
            }`}
          >
            {autoRefresh ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
            {autoRefresh ? '自动刷新中' : '自动刷新已暂停'}
          </button>

          <button
            type="button"
            onClick={() => fetchBackendLogs({ scrollAfterLoad: true })}
            title="立即刷新"
            className="inline-flex items-center justify-center rounded-lg p-2 bg-card border border-border text-muted-foreground hover:text-foreground transition-colors"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {showConfig && (
        <div className="rounded-lg border border-border bg-muted/30 px-3 py-3 space-y-3 flex-shrink-0">
          <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
            <div>
              <div className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
                <Settings2 className="w-3.5 h-3.5" /> 显示配置
              </div>
              <div className="text-[11px] text-muted-foreground/80 mt-1">
                配置保存在服务端，对所有管理员生效，不再跟随当前浏览器。
              </div>
            </div>

            <button
              type="button"
              onClick={saveBackendLogSettings}
              disabled={savingConfig}
              className="inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm border bg-primary text-primary-foreground border-primary hover:bg-primary/90 disabled:opacity-60"
            >
              {savingConfig ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              保存配置
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <label className="space-y-1">
              <span className="text-xs font-medium text-muted-foreground">一页显示多少行</span>
              <select
                value={pageSize}
                onChange={e => handlePageSizeChange(parseInt(e.target.value, 10))}
                className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground"
              >
                {PAGE_SIZE_OPTIONS.map(option => (
                  <option key={option} value={option}>{`${option} 行 / 页`}</option>
                ))}
              </select>
            </label>

            <label className="space-y-1">
              <span className="text-xs font-medium text-muted-foreground">后端保留多少行</span>
              <select
                value={bufferSize}
                onChange={e => handleBufferSizeChange(parseInt(e.target.value, 10))}
                className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground"
              >
                {BUFFER_SIZE_OPTIONS.map(option => (
                  <option key={option} value={option}>{`保留最近 ${option} 行`}</option>
                ))}
              </select>
            </label>
          </div>

          {isBufferSmallerThanPage && (
            <div className="rounded-lg border border-yellow-500/20 bg-yellow-500/10 px-3 py-2 text-sm text-yellow-700 dark:text-yellow-400">
              当前“后端保留多少行”小于“一页显示多少行”。这意味着即使单页设置为 {pageSize} 行，
              服务端内存里也只保留最近 {bufferSize} 行，因此页面实际最多只能拿到 {bufferSize} 行日志。
            </div>
          )}

          {configMessage && (
            <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-600 dark:text-emerald-400">
              {configMessage}
            </div>
          )}
        </div>
      )}

      <div className="bg-card border border-border rounded-lg shadow-sm p-3 space-y-3 flex-shrink-0">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 flex-1 min-w-[200px]">
            <span className="text-xs font-medium text-muted-foreground whitespace-nowrap">关键字</span>
            <div className="relative flex-1">
              <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="搜索日志内容"
                className="w-full bg-background border border-border rounded-md pl-9 pr-3 py-1.5 text-sm text-foreground h-8"
              />
            </div>
          </label>

          <label className="flex items-center gap-2 w-[160px]">
            <span className="text-xs font-medium text-muted-foreground whitespace-nowrap">输出流</span>
            <select
              value={streamFilter}
              onChange={e => setStreamFilter(e.target.value as 'ALL' | 'stdout' | 'stderr')}
              className="w-full bg-background border border-border rounded-md px-3 py-1.5 text-sm text-foreground h-8"
            >
              <option value="ALL">全部输出</option>
              <option value="stdout">仅 stdout</option>
              <option value="stderr">仅 stderr</option>
            </select>
          </label>

          <label className="flex items-center gap-2 w-[160px]">
            <span className="text-xs font-medium text-muted-foreground whitespace-nowrap">级别</span>
            <select
              value={levelFilter}
              onChange={e => setLevelFilter(e.target.value as 'ALL' | BackendLogLevel)}
              className="w-full bg-background border border-border rounded-md px-3 py-1.5 text-sm text-foreground h-8"
            >
              <option value="ALL">全部级别</option>
              {LEVEL_OPTIONS.map(level => (
                <option key={level} value={level}>{level}</option>
              ))}
            </select>
          </label>

          <button
            type="button"
            onClick={toggleErrorOnlyMode}
            className={`inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors h-8 ${
              errorOnlyMode
                ? 'border-red-500/40 bg-red-500/12 text-red-700 dark:text-red-300'
                : 'border-border bg-background text-foreground hover:bg-muted'
            }`}
          >
            <CircleAlert className="w-4 h-4" />
            {errorOnlyMode ? '仅错误' : '仅错误'}
          </button>
        </div>

        {(loggerOptions.length > 0 || hasLoggerFilter) && (
          <div className="space-y-2">
            <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span className="font-medium">快捷 logger 筛选</span>
                <span>支持搜索和折叠，logger 很多时也更好找。</span>
              </div>
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>共 {loggerOptions.length} 个 logger</span>
                {hasHiddenLoggerOptions && (
                  <button
                    type="button"
                    onClick={() => setShowAllLoggerOptions(prev => !prev)}
                    className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2.5 py-1 text-foreground hover:bg-muted"
                  >
                    {showAllLoggerOptions ? '收起' : `展开全部 (${filteredLoggerOptions.length})`}
                  </button>
                )}
              </div>
            </div>

            <div className="relative">
              <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input
                type="text"
                value={loggerSearchText}
                onChange={e => setLoggerSearchText(e.target.value)}
                placeholder="搜索 logger 名称，如 Zoaholic / httpx / watchfiles"
                className="w-full bg-background border border-border rounded-md pl-9 pr-3 py-1.5 text-sm text-foreground h-8"
              />
            </div>

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => setLoggerFilter('ALL')}
                className={`inline-flex items-center rounded-full border px-2.5 py-1 text-xs transition-colors ${
                  !hasLoggerFilter
                    ? 'border-primary bg-primary text-primary-foreground'
                    : 'border-border bg-background text-muted-foreground hover:bg-muted'
                }`}
              >
                全部 logger
              </button>
              {visibleLoggerOptions.map(name => (
                <button
                  key={name}
                  type="button"
                  onClick={() => setLoggerFilter(name)}
                  className={`inline-flex items-center rounded-full border px-2.5 py-1 text-xs transition-colors ${loggerFilter === name ? 'border-primary bg-primary/12 text-primary' : 'border-border bg-background text-foreground hover:bg-muted'}`}
                >{name}</button>
              ))}
            </div>
            {loggerSearchText.trim() && filteredLoggerOptions.length === 0 && (
              <div className="text-xs text-muted-foreground px-1">
                没找到匹配的 logger，可清空搜索后重试。
              </div>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
          <button
            type="button"
            onClick={() => setShowConfig(prev => !prev)}
            className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-muted border border-border hover:bg-muted/80 transition-colors"
          >
            {showConfig ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
            显示配置
          </button>
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-muted border border-border">
            <Server className="w-3.5 h-3.5" /> 当前结果 {items.length} 行
          </span>
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-muted border border-border">
            <Terminal className="w-3.5 h-3.5" /> 匹配总数 {total}
          </span>
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-muted border border-border">
            <ArrowDown className="w-3.5 h-3.5" /> 最新 ID {maxId}
          </span>
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-muted border border-border">
            <Server className="w-3.5 h-3.5" /> 后端当前保留 {effectiveBufferSize} 行
          </span>
          {hasLoggerFilter && (
            <button type="button" onClick={() => setLoggerFilter('ALL')} className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2 py-0.5 text-[10px] text-foreground hover:bg-muted">
              清除 logger 筛选
            </button>
          )}
          {errorOnlyMode && (
            <button type="button" onClick={toggleErrorOnlyMode} className="inline-flex items-center gap-1 rounded-full border border-red-500/30 bg-red-500/10 px-2 py-0.5 text-[10px] text-red-700 dark:text-red-300 hover:bg-red-500/15">
              退出仅错误模式
            </button>
          )}
        </div>

        {error && (
          <div className="flex items-start gap-2 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-600 dark:text-red-400">
            <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            <span>{error}</span>
          </div>
        )}
      </div>

      <div className="flex-1 min-h-0 min-w-0 bg-card border border-border rounded-lg overflow-hidden flex flex-col">
        {items.length === 0 && !loading ? (
          <div className="h-full flex flex-col items-center justify-center text-muted-foreground px-6 py-16 text-center">
            <Terminal className="w-12 h-12 mb-4 opacity-50" />
            <p className="text-sm">当前没有可展示的后台日志</p>
            <p className="text-xs mt-2 opacity-80">可尝试切换关键字、输出流、日志级别、仅错误模式或 logger 名称筛选，或等待应用产生新的日志。</p>
          </div>
        ) : (
          <div ref={scrollerRef} onScroll={checkNearBottom} className="flex-1 min-h-0 overflow-auto bg-background/60">
            <div className="w-full min-w-0 font-mono text-xs">
              {items.map(item => {
                const levelMeta = getLevelMeta(item.level);
                const LevelIcon = levelMeta.icon;
                return (
                  <div
                    key={item.id}
                    className={`flex flex-col lg:flex-row lg:items-start gap-1 lg:gap-3 border-b border-border/60 border-l-4 px-2.5 sm:px-3 py-2 hover:bg-muted/40 ${levelMeta.rowClasses}`}
                  >
                    <div className="flex items-center gap-2 lg:w-[280px] flex-shrink-0 flex-wrap lg:pt-0.5">
                      <span className="text-muted-foreground whitespace-nowrap text-[11px] font-medium opacity-80 mt-[1px]">{formatTime(item.captured_at)}</span>
                      <span
                        className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 border text-[10px] font-semibold tracking-wide flex-shrink-0 ${levelMeta.badgeClasses}`}
                      >
                        <LevelIcon className="w-3.5 h-3.5" />
                        {levelMeta.label}
                      </span>
                      <button
                        type="button"
                        onClick={() => item.logger_name && setLoggerFilter(item.logger_name)}
                        disabled={!item.logger_name}
                        className={`inline-flex items-center rounded px-1.5 py-0.5 border text-[10px] truncate max-w-[180px] lg:max-w-[130px] flex-shrink-0 ${item.logger_name ? 'bg-muted border-border text-foreground hover:bg-muted/80 cursor-pointer' : 'bg-muted border-border opacity-80 cursor-default'}`}
                      >{getSourceLabel(item)}</button>
                    </div>
                    <div className="min-w-0 flex-1 flex flex-row items-start gap-1.5 lg:gap-3">
                      <span
                        className={`inline-flex items-center justify-center rounded-md px-1.5 py-[1px] border text-[9px] font-bold tracking-wider w-[38px] lg:w-[44px] flex-shrink-0 h-fit mt-[4px] lg:mt-[4px] ${getStreamBadgeClasses(item.stream)}`}
                      >
                        {item.stream}
                      </span>
                      <div className="text-foreground whitespace-pre-wrap break-words leading-5 min-w-0 flex-1">
                        {item.message}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

