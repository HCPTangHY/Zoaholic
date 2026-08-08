import { useState, useEffect, useRef } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import {
  RefreshCw, Filter, ChevronDown, ChevronRight, FileText,
  Clock, ArrowDownToLine, CheckCircle2, XCircle,
  Globe, Key, Server, RotateCcw, Eye, EyeOff,
  Flag, Users, Zap, AlertTriangle, X, Search, Calendar
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
  // Prompt Caching 字段来自后端 request_stats，用于展示缓存命中和缓存创建 token。
  cached_tokens?: number;
  cache_creation_tokens?: number;
  prompt_price?: number;
  completion_price?: number;
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
  // 修改原因：日志详情需要展示后端已保存的上游请求头和新增的上游响应头。
  // 修改方式：在前端 LogEntry 类型中补齐两个可选字段。
  // 目的：让 TypeScript 能识别接口返回的上下游头信息。
  upstream_request_headers?: string;
  upstream_request_body?: string;
  upstream_response_headers?: string;
  upstream_response_body?: string;
  response_body?: string;
  raw_data_expires_at?: string;
}

// ── 时间快捷选项 ──
const TIME_PRESETS = [
  { label: '1h', hours: 1 },
  { label: '6h', hours: 6 },
  { label: '24h', hours: 24 },
  { label: '3d', hours: 72 },
  { label: '7d', hours: 168 },
] as const;

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
  // 修改原因：文本筛选原来直接参与请求依赖，输入每个字符都会触发日志请求并放大竞态风险。
  // 修改方式：将文本框的输入态和真正参与请求的提交态拆开，输入态只负责界面显示。
  // 目的：让模型、渠道、Key 三个文本筛选只在防抖提交或手动清除时影响请求。
  const [inputModel, setInputModel] = useState('');
  const [inputProvider, setInputProvider] = useState('');
  const [inputApiKey, setInputApiKey] = useState('');
  const [committedModel, setCommittedModel] = useState('');
  const [committedProvider, setCommittedProvider] = useState('');
  const [committedApiKey, setCommittedApiKey] = useState('');
  const [filterSuccess, setFilterSuccess] = useState<string>('ALL');
  const [filterTimePreset, setFilterTimePreset] = useState<number | null>(null);
  const [filterStartTime, setFilterStartTime] = useState('');
  const [filterEndTime, setFilterEndTime] = useState('');
  const [showFilters, setShowFilters] = useState(false);
  // 修改原因：清除按钮可能只清空尚未提交的输入值，此时提交态不变，普通依赖不会触发请求。
  // 修改方式：维护一个刷新序号，清除操作递增它，让筛选请求立即重新执行。
  // 目的：确保清除按钮不等待防抖，并且始终能立即刷新日志列表。
  const [filterRefreshNonce, setFilterRefreshNonce] = useState(0);
  // 修改原因：需要手写 500ms 防抖并取消上一次待提交任务，避免依赖 lodash。
  // 修改方式：使用 useRef 保存 setTimeout 返回值，输入变化时清理旧定时器后创建新定时器。
  // 目的：连续输入时只在最后一次输入停止 500ms 后提交文本筛选。
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 修改原因：快速筛选会产生并发请求，旧响应可能后到并覆盖新结果。
  // 修改方式：使用 useRef 保存当前 AbortController，每次请求前 abort 上一次请求。
  // 目的：只允许最新请求更新日志列表，避免搜索结果回退。
  const abortControllerRef = useRef<AbortController | null>(null);
  // 修改原因：列表接口为性能不再返回请求体、响应体和头信息，展开日志时需要单独保存完整详情。
  // 修改方式：按日志 ID 缓存 /v1/logs/{id} 的返回，并为每条详情维护加载和错误状态。
  // 目的：列表页保持轻量，用户展开某一条日志时才按需读取大字段。
  const [logDetails, setLogDetails] = useState<Record<number, LogEntry>>({});
  const [detailLoadingIds, setDetailLoadingIds] = useState<Set<number>>(new Set());
  const [detailErrorById, setDetailErrorById] = useState<Record<number, string>>({});
  const detailAbortControllersRef = useRef<Map<number, AbortController>>(new Map());

  // Accordion State
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());

  // ── 构造时间参数 ──
  const getTimeParams = () => {
    if (filterTimePreset) {
      const start = new Date(Date.now() - filterTimePreset * 3600_000);
      return { start_time: start.toISOString(), end_time: '' };
    }
    return { start_time: filterStartTime, end_time: filterEndTime };
  };

  const fetchLogs = async (resetPage = false) => {
    if (!token) return;

    // 修改原因：筛选和分页可能连续触发请求，旧请求如果后完成会覆盖新结果。
    // 修改方式：每次请求前取消上一个 AbortController，并为本次请求创建新的 controller。
    // 目的：让 fetch 层面停止旧请求，配合状态更新检查消除竞态覆盖。
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const controller = new AbortController();
    abortControllerRef.current = controller;
    setLoading(true);

    const currentPage = resetPage ? 1 : page;
    if (resetPage) setPage(1);

    try {
      const queryParams = new URLSearchParams({
        page: currentPage.toString(),
        page_size: pageSize.toString(),
      });

      if (committedModel.trim()) queryParams.append('model', committedModel.trim());
      if (committedProvider.trim()) queryParams.append('provider', committedProvider.trim());
      if (committedApiKey.trim()) queryParams.append('api_key', committedApiKey.trim());
      if (filterSuccess === 'SUCCESS') queryParams.append('success', 'true');
      if (filterSuccess === 'FAILED') queryParams.append('success', 'false');

      const { start_time, end_time } = getTimeParams();
      if (start_time) queryParams.append('start_time', start_time);
      if (end_time) queryParams.append('end_time', end_time);

      const res = await apiFetch(`/v1/logs?${queryParams.toString()}`, {
        signal: controller.signal,
      });

      if (res.ok) {
        const data = await res.json();
        // 修改原因：即使请求已被取消，也可能已经进入响应解析阶段。
        // 修改方式：更新状态前确认当前 controller 仍是最新请求且未被 abort。
        // 目的：防止旧请求在极端时序下覆盖新筛选结果。
        if (controller.signal.aborted || abortControllerRef.current !== controller) return;
        const fetchedLogs = data.items || [];
        setLogs(fetchedLogs);
        setTotalCount(data.total || 0);
        setHasMore(currentPage * pageSize < (data.total || 0));
      }
    } catch (err) {
      const errorName = typeof err === 'object' && err !== null && 'name' in err ? String(err.name) : '';
      if (errorName === 'AbortError') return;
      console.error('Failed to fetch logs:', err);
    } finally {
      // 修改原因：被取消的旧请求进入 finally 时，不应关闭新请求的加载状态。
      // 修改方式：只有当前 controller 仍是最新请求时，才清理引用并结束 loading。
      // 目的：避免旧请求影响新请求的界面状态。
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    fetchLogs(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [committedModel, committedProvider, committedApiKey, filterSuccess, filterTimePreset, filterStartTime, filterEndTime, filterRefreshNonce]);

  const loadMore = () => {
    setPage(prev => prev + 1);
  };

  useEffect(() => {
    if (page > 1) {
      fetchLogs();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const fetchLogDetail = async (id: number) => {
    if (!token || logDetails[id] || detailLoadingIds.has(id)) return;

    // 修改原因：展开详情会触发单条日志请求，用户可能快速折叠、切换或离开页面。
    // 修改方式：为每个日志详情请求创建独立 AbortController，并按日志 ID 清理加载状态。
    // 目的：避免无效详情请求更新界面，同时保持列表请求的取消逻辑不受影响。
    const previousController = detailAbortControllersRef.current.get(id);
    if (previousController) previousController.abort();

    const controller = new AbortController();
    detailAbortControllersRef.current.set(id, controller);
    setDetailLoadingIds(prev => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    setDetailErrorById(prev => {
      const next = { ...prev };
      delete next[id];
      return next;
    });

    try {
      const res = await apiFetch(`/v1/logs/${id}`, {
        signal: controller.signal,
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      if (controller.signal.aborted || detailAbortControllersRef.current.get(id) !== controller) return;
      setLogDetails(prev => ({ ...prev, [id]: data }));
    } catch (err) {
      const errorName = typeof err === 'object' && err !== null && 'name' in err ? String(err.name) : '';
      if (errorName === 'AbortError') return;
      console.error('Failed to fetch log detail:', err);
      setDetailErrorById(prev => ({ ...prev, [id]: '详情加载失败' }));
    } finally {
      if (detailAbortControllersRef.current.get(id) === controller) {
        detailAbortControllersRef.current.delete(id);
        setDetailLoadingIds(prev => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    }
  };

  const toggleExpand = (id: number) => {
    const willExpand = !expandedIds.has(id);
    setExpandedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    if (willExpand) {
      void fetchLogDetail(id);
    }
  };

  useEffect(() => {
    if (
      inputModel === committedModel &&
      inputProvider === committedProvider &&
      inputApiKey === committedApiKey
    ) {
      return;
    }

    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    debounceTimerRef.current = setTimeout(() => {
      setCommittedModel(inputModel);
      setCommittedProvider(inputProvider);
      setCommittedApiKey(inputApiKey);
      debounceTimerRef.current = null;
    }, 500);

    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
    };
  }, [inputModel, inputProvider, inputApiKey, committedModel, committedProvider, committedApiKey]);

  useEffect(() => {
    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      // 修改原因：详情请求独立于列表请求，组件卸载时也需要取消，避免卸载后继续更新状态。
      // 修改方式：遍历当前按日志 ID 保存的 AbortController 并清空 Map。
      // 目的：防止详情懒加载在页面切换后留下悬空请求。
      detailAbortControllersRef.current.forEach(controller => controller.abort());
      detailAbortControllersRef.current.clear();
    };
  }, []);

  const hasActiveFilters = Boolean(
    inputModel || inputProvider || inputApiKey ||
    committedModel || committedProvider || committedApiKey ||
    filterSuccess !== 'ALL' || filterTimePreset || filterStartTime || filterEndTime
  );

  const triggerImmediateFilterRefresh = () => {
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }
    setFilterRefreshNonce(prev => prev + 1);
  };

  const clearModelFilter = () => {
    setInputModel('');
    setCommittedModel('');
    triggerImmediateFilterRefresh();
  };

  const clearProviderFilter = () => {
    setInputProvider('');
    setCommittedProvider('');
    triggerImmediateFilterRefresh();
  };

  const clearApiKeyFilter = () => {
    setInputApiKey('');
    setCommittedApiKey('');
    triggerImmediateFilterRefresh();
  };

  const clearAllFilters = () => {
    setInputModel('');
    setInputProvider('');
    setInputApiKey('');
    setCommittedModel('');
    setCommittedProvider('');
    setCommittedApiKey('');
    setFilterSuccess('ALL');
    setFilterTimePreset(null);
    setFilterStartTime('');
    setFilterEndTime('');
    triggerImmediateFilterRefresh();
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
        month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit'
      });
    } catch { return ts; }
  };

  const formatFullTimestamp = (ts: string) => {
    try { return new Date(ts).toLocaleString('zh-CN'); }
    catch { return ts; }
  };

  const formatJsonBestEffort = (raw: string): { formatted: string; isJson: boolean } => {
    const input = String(raw ?? '').trim();
    if (!input) return { formatted: '', isJson: false };
    try {
      let parsed: unknown = JSON.parse(input);
      if (typeof parsed === 'string') {
        const inner = parsed.trim();
        try { parsed = JSON.parse(inner); }
        catch { return { formatted: inner, isJson: false }; }
      }
      if (parsed === null) return { formatted: 'null', isJson: true };
      if (typeof parsed === 'object') return { formatted: JSON.stringify(parsed, null, 2), isJson: true };
      return { formatted: String(parsed), isJson: false };
    } catch { return { formatted: raw, isJson: false }; }
  };

  // ── Smart body rendering ──────────────────────────────────────

  const ROLE_STYLES: Record<string, { badge: string; bg: string; border: string }> = {
    system: { badge: 'bg-slate-500/20 text-slate-700 dark:text-slate-300', bg: 'bg-slate-500/5', border: 'border-slate-500/15' },
    user: { badge: 'bg-blue-500/20 text-blue-700 dark:text-blue-300', bg: 'bg-blue-500/5', border: 'border-blue-500/15' },
    assistant: { badge: 'bg-emerald-500/20 text-emerald-700 dark:text-emerald-300', bg: 'bg-emerald-500/5', border: 'border-emerald-500/15' },
    tool: { badge: 'bg-violet-500/20 text-violet-700 dark:text-violet-300', bg: 'bg-violet-500/5', border: 'border-violet-500/15' },
    info: { badge: 'bg-amber-500/20 text-amber-700 dark:text-amber-300', bg: 'bg-amber-500/5', border: 'border-amber-500/15 border-dashed' },
  };

  const extractTextContent = (content: any): string => {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) return content.map(p => {
      if (typeof p === 'string') return p;
      if (p.type === 'text') return p.text || '';
      if (p.type === 'image_url') return '[\uD83D\uDDBC Image]';
      if (p.type === 'tool_use') return `[Tool: ${p.name}]\n${JSON.stringify(p.input, null, 2)}`;
      if (p.type === 'tool_result') return `[Result]\n${typeof p.content === 'string' ? p.content : JSON.stringify(p.content, null, 2)}`;
      return JSON.stringify(p, null, 2);
    }).join('\n');
    return content == null ? '' : JSON.stringify(content, null, 2);
  };

  /** Try to fix truncated JSON: close open brackets/braces */
  const tryFixTruncatedJson = (raw: string): any => {
    // First try as-is
    try { return JSON.parse(raw); } catch {}
    // Strip trailing truncation markers like '... [截断总计 XXX 字符]'
    let s = raw.replace(/\.{3}\s*\[截断[^\]]*\]\s*$/, '').replace(/\.{3}\s*\[truncat[^\]]*\]\s*$/i, '');
    // Try stripping trailing incomplete string/value
    s = s.replace(/,\s*"[^"]*$/, '').replace(/,\s*$/, '');
    // Count unclosed brackets
    const opens: string[] = [];
    let inStr = false, esc = false;
    for (const ch of s) {
      if (esc) { esc = false; continue; }
      if (ch === '\\') { esc = true; continue; }
      if (ch === '"') { inStr = !inStr; continue; }
      if (inStr) continue;
      if (ch === '{' || ch === '[') opens.push(ch);
      else if (ch === '}' && opens.length && opens[opens.length - 1] === '{') opens.pop();
      else if (ch === ']' && opens.length && opens[opens.length - 1] === '[') opens.pop();
    }
    // If we're inside a string, close it first
    if (inStr) s += '"';
    // Close remaining brackets in reverse
    for (let i = opens.length - 1; i >= 0; i--) {
      s += opens[i] === '{' ? '}' : ']';
    }
    try { return JSON.parse(s); } catch {}
    return null;
  };

  const parseRequestPayload = (raw: string) => {
    try {
      let p = tryFixTruncatedJson(raw);
      if (typeof p === 'string') p = tryFixTruncatedJson(p);
      if (!p) return null;
      // OpenAI / Anthropic (both use messages array)
      if (p?.messages && Array.isArray(p.messages)) {
        const { model, messages, system, ...meta } = p;
        const allMessages: any[] = [];
        // Anthropic puts system at top level (string or content-block array)
        if (system) {
          const sysText = typeof system === 'string' ? system
            : Array.isArray(system) ? system.map((b: any) => b.text || JSON.stringify(b)).join('\n')
            : JSON.stringify(system);
          allMessages.push({ role: 'system', content: sysText });
        }
        // Filter out truncation markers like "[... 169 \u66f4\u591a\u9879]" and convert non-object items
        for (const msg of messages) {
          if (typeof msg === 'string') {
            // Truncation marker from backend: "[... N \u66f4\u591a\u9879]"
            const truncMatch = msg.match(/^\[(?:\.{3}|\u2026)\s*(\d+)\s*\u66f4\u591a\u9879\]$/);
            if (truncMatch) {
              allMessages.push({ role: 'info', content: `[\u5df2\u7701\u7565 ${truncMatch[1]} \u6761\u6d88\u606f]` });
            } else {
              allMessages.push({ role: 'unknown', content: msg });
            }
          } else if (msg && typeof msg === 'object') {
            allMessages.push(msg);
          }
        }
        return { model, messages: allMessages, meta };
      }
      // Gemini (uses contents array with parts)
      if (p?.contents && Array.isArray(p.contents)) {
        const messages: any[] = [];
        if (p.systemInstruction?.parts) {
          const text = p.systemInstruction.parts.map((pt: any) => pt.text || '').join('');
          if (text) messages.push({ role: 'system', content: text });
        }
        for (const c of p.contents) {
          if (typeof c === 'string') {
            const truncMatch = c.match(/^\[(?:\.{3}|\u2026)\s*(\d+)\s*\u66f4\u591a\u9879\]$/);
            if (truncMatch) {
              messages.push({ role: 'info', content: `[\u5df2\u7701\u7565 ${truncMatch[1]} \u6761\u6d88\u606f]` });
            } else {
              messages.push({ role: 'unknown', content: c });
            }
            continue;
          }
          const text = c.parts?.map((pt: any) => {
            if (pt.text) return pt.text;
            if (pt.inlineData) return '[Image]';
            return JSON.stringify(pt);
          }).join('') || '';
          messages.push({ role: c.role || 'user', content: text });
        }
        const { contents, systemInstruction, generationConfig, ...rest } = p;
        const meta = { ...rest, ...(generationConfig || {}) };
        return { model: p.model, messages, meta };
      }
    } catch {}
    return null;
  };

  const parseSSEResponse = (raw: string) => {
    if (!raw.includes('data: ')) return null;
    let content = '', reasoning = '', model = '';
    let usage: any = null;
    const tcMap = new Map<number, { id: string; name: string; args: string }>();
    for (const line of raw.split('\n')) {
      const t = line.trim();
      if (!t.startsWith('data: ') && !t.startsWith('data:')) continue;
      const jsonStr = t.slice(t.indexOf(':') + 1).trim();
      if (!jsonStr || jsonStr === '[DONE]') continue;
      try {
        const c = JSON.parse(jsonStr);
        if (!model && c.model) model = c.model;
        // OpenAI format: choices[].delta
        const d = c.choices?.[0]?.delta;
        if (d?.content) content += d.content;
        if (d?.reasoning_content) reasoning += d.reasoning_content;
        if (d?.tool_calls) for (const tc of d.tool_calls) {
          const idx = tc.index ?? 0;
          if (!tcMap.has(idx)) tcMap.set(idx, { id: '', name: '', args: '' });
          const e = tcMap.get(idx)!;
          if (tc.id) e.id = tc.id;
          if (tc.function?.name) e.name = tc.function.name;
          if (tc.function?.arguments) e.args += tc.function.arguments;
        }
        if (c.usage) usage = c.usage;
        // Gemini format: candidates[].content.parts
        const parts = c.candidates?.[0]?.content?.parts;
        if (parts) for (const pt of parts) {
          if (pt.thought) reasoning += pt.text || '';
          else if (pt.text) content += pt.text;
        }
        if (c.usageMetadata && !usage) usage = {
          prompt_tokens: c.usageMetadata.promptTokenCount,
          completion_tokens: c.usageMetadata.candidatesTokenCount,
          total_tokens: c.usageMetadata.totalTokenCount,
        };
      } catch {}
    }
    if (!content && !reasoning && tcMap.size === 0) return null;
    return { content, reasoning, model: model || undefined, usage, toolCalls: tcMap.size > 0 ? [...tcMap.values()] : undefined };
  };

  const parseAnthropicSSE = (raw: string) => {
    if (!raw.includes('message_start') && !raw.includes('content_block_delta')) return null;
    let content = '', reasoning = '', model = '';
    let usage: any = null;
    const blockTypes = new Map<number, string>();
    const blockAccum = new Map<number, string>();
    const tcInfo = new Map<number, { id: string; name: string }>();
    for (const line of raw.split('\n')) {
      const t = line.trim();
      if (!t.startsWith('data:')) continue;
      const jsonStr = t.slice(t.indexOf(':') + 1).trim();
      if (!jsonStr) continue;
      try {
        const d = JSON.parse(jsonStr);
        if (d.type === 'message_start') {
          model = d.message?.model || '';
          if (d.message?.usage) usage = { ...d.message.usage };
        } else if (d.type === 'content_block_start') {
          const idx = d.index ?? 0;
          const btype = d.content_block?.type || 'text';
          blockTypes.set(idx, btype);
          blockAccum.set(idx, '');
          if (btype === 'tool_use') tcInfo.set(idx, { id: d.content_block?.id || '', name: d.content_block?.name || '' });
        } else if (d.type === 'content_block_delta') {
          const idx = d.index ?? 0;
          const delta = d.delta;
          if (delta?.type === 'text_delta') blockAccum.set(idx, (blockAccum.get(idx) || '') + (delta.text || ''));
          else if (delta?.type === 'thinking_delta') blockAccum.set(idx, (blockAccum.get(idx) || '') + (delta.thinking || ''));
          else if (delta?.type === 'input_json_delta') blockAccum.set(idx, (blockAccum.get(idx) || '') + (delta.partial_json || ''));
        } else if (d.type === 'message_delta') {
          if (d.usage) usage = { ...usage, ...d.usage };
        }
      } catch {}
    }
    const toolCalls: Array<{ id: string; name: string; args: string }> = [];
    for (const [idx, btype] of blockTypes) {
      const text = blockAccum.get(idx) || '';
      if (btype === 'thinking') reasoning += text;
      else if (btype === 'text') content += text;
      else if (btype === 'tool_use') {
        const info = tcInfo.get(idx);
        toolCalls.push({ id: info?.id || '', name: info?.name || '', args: text });
      }
    }
    if (!content && !reasoning && toolCalls.length === 0) return null;
    return { content, reasoning, model: model || undefined, usage, toolCalls: toolCalls.length > 0 ? toolCalls : undefined };
  };

  const parseNonStreamResponse = (raw: string) => {
    try {
      let p = tryFixTruncatedJson(raw);
      if (typeof p === 'string') p = tryFixTruncatedJson(p);
      if (!p) return null;
      // OpenAI format: choices[].message
      const msg = p?.choices?.[0]?.message;
      if (msg) {
        const tcs = msg.tool_calls?.map((tc: any) => ({
          id: tc.id || '', name: tc.function?.name || '',
          args: typeof tc.function?.arguments === 'string' ? tc.function.arguments : JSON.stringify(tc.function?.arguments || {}),
        }));
        return { content: msg.content || '', reasoning: msg.reasoning_content || '', model: p.model, usage: p.usage, toolCalls: tcs?.length ? tcs : undefined };
      }
      // Anthropic format: type=message, content[] with text/thinking/tool_use blocks
      if (p?.type === 'message' && Array.isArray(p?.content)) {
        let content = '', reasoning = '';
        const toolCalls: Array<{ id: string; name: string; args: string }> = [];
        for (const block of p.content) {
          if (block.type === 'text') content += block.text || '';
          else if (block.type === 'thinking') reasoning += block.thinking || '';
          else if (block.type === 'tool_use') toolCalls.push({ id: block.id || '', name: block.name || '', args: JSON.stringify(block.input || {}, null, 2) });
        }
        return { content, reasoning, model: p.model, usage: p.usage, toolCalls: toolCalls.length > 0 ? toolCalls : undefined };
      }
      // Gemini format: candidates[].content.parts
      const parts = p?.candidates?.[0]?.content?.parts;
      if (parts) {
        let content = '', reasoning = '';
        for (const pt of parts) {
          if (pt.thought) reasoning += pt.text || '';
          else if (pt.text) content += pt.text;
        }
        const usage = p.usageMetadata ? { prompt_tokens: p.usageMetadata.promptTokenCount, completion_tokens: p.usageMetadata.candidatesTokenCount, total_tokens: p.usageMetadata.totalTokenCount } : undefined;
        return { content, reasoning, model: p.modelVersion, usage, toolCalls: undefined };
      }
    } catch {}
    return null;
  };

  const MessageBubble = ({ role, content, name, toolCallId, toolCalls }: {
    role: string; content: string; name?: string; toolCallId?: string;
    toolCalls?: Array<{ id: string; type: string; function: { name: string; arguments: string } }>;
  }) => {
    const [expanded, setExpanded] = useState(false);
    const s = ROLE_STYLES[role] || ROLE_STYLES.user;
    const isLong = content.length > 600;
    return (
      <div className={`rounded-lg border ${s.border} ${s.bg} p-2.5 space-y-1.5`}>
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${s.badge}`}>{role}</span>
          {name && <span className="text-[10px] font-mono text-muted-foreground">{name}</span>}
          {toolCallId && <span className="text-[10px] font-mono text-muted-foreground truncate max-w-[200px]">id:{toolCallId}</span>}
          {content && <span className="text-[10px] text-muted-foreground ml-auto">{content.length} chars</span>}
        </div>
        {content && <pre className="text-xs text-foreground whitespace-pre-wrap break-words leading-relaxed">{isLong && !expanded ? content.slice(0, 600) : content}</pre>}
        {isLong && <button onClick={() => setExpanded(!expanded)} className="text-[10px] text-primary hover:underline">{expanded ? '\u2191 Collapse' : `\u2193 Expand (${content.length} chars)`}</button>}
        {toolCalls?.map((tc, i) => (
          <div key={i} className="rounded border border-violet-500/15 bg-violet-500/5 p-2 mt-1">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-[10px] font-bold uppercase px-1 py-0.5 rounded bg-violet-500/20 text-violet-700 dark:text-violet-300">call</span>
              <span className="text-xs font-mono text-foreground">{tc.function.name}</span>
            </div>
            <pre className="text-[11px] font-mono text-foreground/80 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
              {(() => { try { return JSON.stringify(JSON.parse(tc.function.arguments), null, 2); } catch { return tc.function.arguments; } })()}
            </pre>
          </div>
        ))}
      </div>
    );
  };

  const ResponseContentView = ({ content, reasoning, model, usage, toolCalls }: {
    content: string; reasoning: string; model?: string; usage?: any;
    toolCalls?: Array<{ id: string; name: string; args: string }>;
  }) => {
    const [showThinking, setShowThinking] = useState(false);
    return (
      <div className="bg-background p-3 space-y-2">
        <div className="flex flex-wrap gap-1.5 pb-2 border-b border-border text-[10px] font-mono">
          {model && <span className="px-1.5 py-0.5 rounded bg-muted border border-border text-muted-foreground">model: <span className="text-foreground">{model}</span></span>}
          {usage?.prompt_tokens != null && <span className="px-1.5 py-0.5 rounded bg-muted border border-border text-muted-foreground">in: <span className="text-foreground">{usage.prompt_tokens}</span></span>}
          {usage?.completion_tokens != null && <span className="px-1.5 py-0.5 rounded bg-muted border border-border text-muted-foreground">out: <span className="text-foreground">{usage.completion_tokens}</span></span>}
          {usage?.prompt_tokens_details?.cached_tokens > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-emerald-500/10 border border-emerald-500/20 text-emerald-700 dark:text-emerald-400">cached: {usage.prompt_tokens_details.cached_tokens}</span>
          )}
        </div>
        {reasoning && (
          <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 overflow-hidden">
            <div className="flex items-center gap-2 px-2.5 py-1.5 cursor-pointer hover:bg-amber-500/10 transition-colors" onClick={() => setShowThinking(!showThinking)}>
              {showThinking ? <ChevronDown className="w-3 h-3 text-muted-foreground" /> : <ChevronRight className="w-3 h-3 text-muted-foreground" />}
              <span className="text-[10px] font-bold uppercase px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-700 dark:text-amber-400">Thinking</span>
              <span className="text-[10px] text-muted-foreground">{reasoning.length} chars</span>
            </div>
            {showThinking && <pre className="px-2.5 pb-2.5 text-xs text-foreground/80 whitespace-pre-wrap break-words max-h-60 overflow-y-auto">{reasoning}</pre>}
          </div>
        )}
        {content && (
          <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-2.5">
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-[10px] font-bold uppercase px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-700 dark:text-emerald-400">Content</span>
              <span className="text-[10px] text-muted-foreground">{content.length} chars</span>
            </div>
            <pre className="text-xs text-foreground whitespace-pre-wrap break-words leading-relaxed">{content}</pre>
          </div>
        )}
        {toolCalls?.map((tc, i) => (
          <div key={i} className="rounded-lg border border-violet-500/20 bg-violet-500/5 p-2.5">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-[10px] font-bold uppercase px-1.5 py-0.5 rounded bg-violet-500/20 text-violet-700 dark:text-violet-300">Tool Call</span>
              <span className="text-xs font-mono text-foreground">{tc.name}</span>
              {tc.id && <span className="text-[10px] font-mono text-muted-foreground truncate max-w-[200px]">{tc.id}</span>}
            </div>
            <pre className="text-xs font-mono text-foreground/80 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
              {(() => { try { return JSON.stringify(JSON.parse(tc.args), null, 2); } catch { return tc.args; } })()}
            </pre>
          </div>
        ))}
      </div>
    );
  };

  /** DB 有时把 body 存为 JSON 转义字符串（\n \" 等字面量），需要先还原。 */
  const normalizeBody = (raw: string): string => {
    const t = raw.trim();
    if (!t) return t;
    // 尝试 JSON.parse：如果整体是一个 JSON 字符串（"..."），解开得到真实内容
    try {
      const p = JSON.parse(t);
      if (typeof p === 'string') return p;
      // parse 成功且是 object/array → 原始文本本身就是合法 JSON，直接返回
      return t;
    } catch {}
    // JSON.parse 失败时的兜底：如果含有字面 \n 但无真实换行，手动替换
    if (t.includes('\\n') && !t.includes('\n')) {
      return t.replace(/\\n/g, '\n').replace(/\\"/g, '"');
    }
    return t;
  };

  const BodyAccordion = ({ title, data, icon, variant }: {
    title: string; data?: string; icon?: import('react').ReactNode; variant: 'request' | 'response';
  }) => {
    const [isOpen, setIsOpen] = useState(false);
    const [showRaw, setShowRaw] = useState(false);
    if (!data) return null;

    const normalized = normalizeBody(data);
    const canParse = variant === 'request'
      ? normalized.includes('"messages"') || normalized.includes('"contents"')
      : normalized.includes('data: ') || normalized.includes('data:') || normalized.includes('"choices"') || normalized.includes('"candidates"') || normalized.includes('"message_start"') || normalized.includes('"type":"message"');

    const { formatted } = formatJsonBestEffort(data);
    const previewText = formatted.length > 80 ? formatted.substring(0, 80) + '...' : formatted;

    let parsedView: import('react').ReactNode = null;
    if (isOpen && !showRaw && canParse) {
      if (variant === 'request') {
        const payload = parseRequestPayload(normalized);
        if (payload?.messages) {
          const chips: Array<{ k: string; v: string }> = [];
          if (payload.model) chips.push({ k: 'model', v: payload.model });
          if (payload.meta?.temperature != null) chips.push({ k: 'temp', v: String(payload.meta.temperature) });
          if (payload.meta?.max_tokens) chips.push({ k: 'max_tokens', v: String(payload.meta.max_tokens) });
          if (payload.meta?.stream != null) chips.push({ k: 'stream', v: String(payload.meta.stream) });
          chips.push({ k: 'messages', v: String(payload.messages.length) });
          if (payload.meta?.tools) chips.push({ k: 'tools', v: String(payload.meta.tools.length) });
          parsedView = (
            <div className="bg-background p-3 space-y-2">
              <div className="flex flex-wrap gap-1.5 pb-2 border-b border-border">
                {chips.map(c => (
                  <span key={c.k} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-muted border border-border text-muted-foreground">
                    {c.k}: <span className="text-foreground">{c.v}</span>
                  </span>
                ))}
              </div>
              {payload.messages.map((msg: any, i: number) => (
                <MessageBubble key={i} role={msg.role || 'unknown'} content={extractTextContent(msg.content)} name={msg.name} toolCallId={msg.tool_call_id} toolCalls={msg.tool_calls} />
              ))}
            </div>
          );
        }
      } else {
        // Try Anthropic SSE first (has event: lines + message_start)
        let parsed = parseAnthropicSSE(normalized);
        // Then OpenAI/Gemini SSE
        if (!parsed) parsed = parseSSEResponse(normalized);
        // Then non-streaming (OpenAI/Anthropic/Gemini JSON)
        if (!parsed) parsed = parseNonStreamResponse(normalized);
        if (parsed) parsedView = <ResponseContentView {...parsed} />;
      }
    }

    return (
      <div className="border border-border rounded-lg overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-2 bg-muted/50 cursor-pointer hover:bg-muted transition-colors" onClick={() => setIsOpen(!isOpen)}>
          <div className="flex-shrink-0 text-muted-foreground">
            {isOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          </div>
          <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">{icon}{title}</div>
          {!isOpen && <div className="flex-1 text-xs font-mono text-muted-foreground/60 truncate ml-2">{previewText.replace(/\n/g, ' ')}</div>}
          {isOpen && canParse && (
            <button onClick={e => { e.stopPropagation(); setShowRaw(!showRaw); }}
              className="ml-auto text-[10px] px-2 py-0.5 rounded border border-border text-muted-foreground hover:text-foreground hover:bg-muted transition-colors">
              {showRaw ? '\u2728 Parsed' : '{ } Raw'}
            </button>
          )}
        </div>
        {isOpen && (
          <div className="border-t border-border max-h-[32rem] overflow-y-auto">
            {(showRaw || !parsedView) ? (
              <pre className="bg-background p-3 text-xs font-mono text-foreground overflow-x-auto whitespace-pre-wrap max-h-80 overflow-y-auto">{formatted}</pre>
            ) : parsedView}
          </div>
        )}
      </div>
    );
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
    } catch { items = null; }

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
                <div className="flex-shrink-0 text-xs font-mono text-muted-foreground mt-0.5">#{idx + 1}</div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-foreground truncate" title={provider}>{provider}</span>
                    <span className={`px-2 py-0.5 rounded text-xs font-mono border ${getHttpCodeColor(code)}`}>{code ?? '-'}</span>
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
    // 修改原因：列表行不再携带 body/header 大字段，展开区域必须优先使用单条详情接口返回的数据。
    // 修改方式：按日志 ID 从详情缓存中取完整记录，加载完成前仍用列表行显示基础信息。
    // 目的：保持展开布局不变，同时把大字段读取延后到用户真正展开时。
    const detailLog = logDetails[log.id] || log;
    const isDetailLoading = detailLoadingIds.has(log.id);
    const detailError = detailErrorById[log.id];
    // 缓存字段统一转成数字，目的是避免旧日志缺字段时影响列表和展开详情渲染。
    const cachedTokens = log.cached_tokens || 0;
    const cacheCreationTokens = log.cache_creation_tokens || 0;
    const hasCacheInfo = cachedTokens > 0 || cacheCreationTokens > 0;
    return (
      <div className="bg-card border border-border rounded-xl overflow-hidden">
        <div className="cursor-pointer hover:bg-muted/50 transition-colors" onClick={() => toggleExpand(log.id)}>
          {/* 第一行：核心信息 */}
          <div className="flex items-center gap-2 sm:gap-3 p-3 sm:p-4">
            <div className="flex-shrink-0 text-muted-foreground">
              {isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            </div>
            <div className="flex-shrink-0">
              {log.success ? <CheckCircle2 className="w-5 h-5 text-emerald-500" /> : <XCircle className="w-5 h-5 text-red-500" />}
            </div>
            <div className="flex-shrink-0 text-xs sm:text-sm font-mono text-muted-foreground w-[85px] sm:w-[100px]">
              {formatTimestamp(log.timestamp)}
            </div>
            <div className="flex-1 min-w-0">
              <div className="font-medium text-foreground text-sm truncate" title={log.model || '-'}>{log.model || '-'}</div>
              <div className="text-xs text-muted-foreground truncate">
                {log.provider || '未知'}
                {log.provider_key_index !== undefined && <span className="opacity-60"> [{log.provider_key_index}]</span>}
              </div>
            </div>
            <div className="hidden sm:flex items-center gap-1.5 flex-shrink-0">
              {log.is_flagged && <span className="text-yellow-500" title="已标记"><Flag className="w-4 h-4" /></span>}
              {(log.retry_count ?? 0) > 0 && (
                <span className="text-orange-500 flex items-center gap-0.5 text-xs" title={`重试 ${log.retry_count} 次`}>
                  <RotateCcw className="w-3.5 h-3.5" />{log.retry_count}
                </span>
              )}
            </div>
            <div className="flex-shrink-0">
              <span className={`px-1.5 sm:px-2 py-0.5 sm:py-1 rounded text-xs font-mono font-medium border ${getStatusColor(log.success, log.status_code)}`}>
                {log.status_code || '-'}
              </span>
            </div>
          </div>
          {/* 第二行：详细指标 */}
          <div className="flex items-center gap-2 sm:gap-4 px-3 sm:px-4 pb-3 sm:pb-4 pt-0 text-xs flex-wrap">
            <div className="flex items-center gap-1 text-muted-foreground" title={`API Key: ${log.api_key_name || log.api_key_prefix || '-'}`}>
              <Key className="w-3.5 h-3.5" />
              <span className="max-w-[80px] sm:max-w-[120px] truncate">{log.api_key_name || log.api_key_prefix || '-'}</span>
            </div>
            {log.api_key_group && (
              <div className="hidden sm:flex items-center gap-1 text-muted-foreground">
                <Users className="w-3.5 h-3.5" /><span>{log.api_key_group}</span>
              </div>
            )}
            <div className="hidden lg:flex items-center gap-1 text-muted-foreground font-mono">
              <Globe className="w-3.5 h-3.5" /><span>{log.client_ip || '-'}</span>
            </div>
            <div className="flex-1" />
            <div className="flex items-center gap-1 font-mono" title={`输入: ${log.prompt_tokens || 0}${cachedTokens > 0 ? `，缓存命中 ${cachedTokens}` : ''}${cacheCreationTokens > 0 ? `，缓存创建 ${cacheCreationTokens}` : ''}；输出: ${log.completion_tokens || 0}；总计: ${log.total_tokens || 0}`}>
              {/* 列表摘要只在命中缓存时追加弱化文本，避免缓存数字抢占主要 token 信息。 */}
              {cachedTokens > 0 ? (
                <>
                  <span className="text-muted-foreground">{(log.prompt_tokens || 0) - cachedTokens}</span>
                  <span className="text-[10px] text-emerald-600/80 dark:text-emerald-400/80">(+{cachedTokens} {(cachedTokens / (log.prompt_tokens || 1) * 100).toFixed(1)}%)</span>
                </>
              ) : (
                <span className="text-muted-foreground">{log.prompt_tokens || 0}</span>
              )}
              <span className="text-muted-foreground/50">→</span>
              <span className="text-blue-600 dark:text-blue-400">{log.completion_tokens || 0}</span>
            </div>
            {log.success && (log.prompt_price || log.completion_price) ? (() => {
              const cost = ((log.prompt_tokens || 0) * (log.prompt_price || 0) + (log.completion_tokens || 0) * (log.completion_price || 0)) / 1_000_000;
              return cost > 0 ? (
                <span className="text-amber-600 dark:text-amber-400 font-mono text-xs" title={`输入 $${log.prompt_price}/M · 输出 $${log.completion_price}/M`}>
                  ${cost >= 0.01 ? cost.toFixed(4) : cost.toFixed(6)}
                </span>
              ) : null;
            })() : null}
            <div className="flex items-center gap-1 text-muted-foreground" title={`总耗时: ${log.process_time?.toFixed(2)}s, 首响: ${log.first_response_time?.toFixed(2) || '-'}s`}>
              <Clock className="w-3.5 h-3.5" />
              <span className="font-mono">{log.process_time?.toFixed(2) || '-'}s</span>
              {log.first_response_time !== undefined && (
                <span className="text-muted-foreground/60 hidden sm:inline">(首响 {log.first_response_time.toFixed(2)}s)</span>
              )}
            </div>
            {speedInfo && (
              <div className={`flex items-center gap-1 font-mono ${speedInfo.color}`} title="生成速度">
                <Zap className="w-3.5 h-3.5" /><span>{speedInfo.speed} t/s</span>
              </div>
            )}
          </div>
        </div>
        {/* Expanded Content */}
        {isExpanded && (
          <div className="border-t border-border bg-muted/30 p-4 space-y-4">
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 text-sm">
              <InfoItem label="日志 ID" value={String(detailLog.id)} mono />
              <InfoItem label="完整时间" value={formatFullTimestamp(detailLog.timestamp)} />
              <InfoItem label="Endpoint" value={detailLog.endpoint || '-'} mono />
              <InfoItem label="客户端 IP" value={detailLog.client_ip || '-'} mono />
              <InfoItem label="Provider ID" value={detailLog.provider_id || '-'} />
              {detailLog.raw_data_expires_at && <InfoItem label="数据过期" value={formatFullTimestamp(detailLog.raw_data_expires_at)} />}
            </div>

            {detailLog.retry_path && (
              <div className="space-y-1">
                <div className="text-xs font-medium text-muted-foreground flex items-center gap-1">
                  <RotateCcw className="w-3.5 h-3.5" /> 重试路径
                </div>
                <RetryPathView retryPathJson={detailLog.retry_path} />
              </div>
            )}
            {isDetailLoading && (
              <div className="rounded-lg border border-border bg-background/60 p-3 text-sm text-muted-foreground">
                正在加载日志详情...
              </div>
            )}
            {detailError && !isDetailLoading && (
              <div className="rounded-lg border border-red-500/20 bg-red-500/10 p-3 text-sm text-red-600 dark:text-red-400">
                {detailError}
              </div>
            )}
            {!isDetailLoading && !detailError && (
              <div className="space-y-2">
                {/* 按数据流方向分组：请求链路（用户→上游）、响应链路（上游→用户） */}
                <div className="space-y-2 rounded-lg border border-border bg-background/60 p-3">
                  <div className="text-xs font-medium text-muted-foreground tracking-wide">请求 →</div>
                  <JsonAccordion title="客户端请求头" data={detailLog.request_headers} icon={<FileText className="w-4 h-4" />} />
                  <BodyAccordion title="客户端请求体" data={detailLog.request_body} icon={<Eye className="w-4 h-4" />} variant="request" />
                  <JsonAccordion title="上游请求头" data={detailLog.upstream_request_headers} icon={<Server className="w-4 h-4" />} />
                  <BodyAccordion title="上游请求体" data={detailLog.upstream_request_body} icon={<Server className="w-4 h-4" />} variant="request" />
                </div>
                <div className="space-y-2 rounded-lg border border-border bg-background/60 p-3">
                  <div className="text-xs font-medium text-muted-foreground tracking-wide">← 响应</div>
                  <JsonAccordion title="上游响应头" data={detailLog.upstream_response_headers} icon={<Server className="w-4 h-4" />} />
                  <BodyAccordion title="上游响应体" data={detailLog.upstream_response_body} icon={<Server className="w-4 h-4" />} variant="response" />
                  <BodyAccordion title="客户端响应体" data={detailLog.response_body} icon={<EyeOff className="w-4 h-4" />} variant="response" />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  const InfoItem = ({ label, value, mono }: { label: string; value: string; mono?: boolean }) => (
    <div className="space-y-0.5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-sm text-foreground truncate ${mono ? 'font-mono' : ''}`} title={value}>{value}</div>
    </div>
  );

  const JsonAccordion = ({ title, data, icon, defaultOpen = false }: { title: string; data?: string; icon?: import('react').ReactNode; defaultOpen?: boolean }) => {
    const [isOpen, setIsOpen] = useState(defaultOpen);
    if (!data) return null;
    const { formatted } = formatJsonBestEffort(data);
    const previewText = formatted.length > 80 ? formatted.substring(0, 80) + '...' : formatted;
    return (
      <div className="border border-border rounded-lg overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-2 bg-muted/50 cursor-pointer hover:bg-muted transition-colors" onClick={() => setIsOpen(!isOpen)}>
          <div className="flex-shrink-0 text-muted-foreground">
            {isOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          </div>
          <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">{icon}{title}</div>
          {!isOpen && <div className="flex-1 text-xs font-mono text-muted-foreground/60 truncate ml-2">{previewText.replace(/\n/g, ' ')}</div>}
        </div>
        {isOpen && (
          <pre className="bg-background p-3 text-xs font-mono text-foreground overflow-x-auto whitespace-pre-wrap max-h-80 overflow-y-auto border-t border-border">
            {formatted}
          </pre>
        )}
      </div>
    );
  };

  // ── datetime-local 工具 ──
  const toLocalDatetimeStr = (iso: string) => {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      const pad = (n: number) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch { return ''; }
  };

  const fromLocalDatetimeStr = (local: string) => {
    if (!local) return '';
    try { return new Date(local).toISOString(); }
    catch { return ''; }
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

      {/* ── Toolbar ── */}
      <div className="bg-card border border-border rounded-xl shadow-sm flex-shrink-0">
        <div className="p-3 sm:p-4 space-y-3">
          {/* Mobile Filter Toggle */}
          <button
            onClick={() => setShowFilters(!showFilters)}
            className="flex items-center gap-2 text-sm text-muted-foreground md:hidden w-full justify-center py-1"
          >
            <Filter className="w-4 h-4" />
            {showFilters ? '收起筛选' : '展开筛选'}
            {hasActiveFilters && <span className="w-1.5 h-1.5 rounded-full bg-primary" />}
            <ChevronDown className={`w-4 h-4 transition-transform ${showFilters ? 'rotate-180' : ''}`} />
          </button>

          <div className={`space-y-3 ${showFilters ? 'block' : 'hidden md:block'}`}>
            {/* 第一排：关键词筛选 */}
            <div className="flex flex-col sm:flex-row gap-2">
              <div className="relative flex-1 min-w-0">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
                <input
                  type="text" placeholder="模型名" value={inputModel}
                  onChange={e => setInputModel(e.target.value)}
                  className="w-full bg-background border border-border text-sm pl-8 pr-7 py-2 rounded-lg text-foreground placeholder:text-muted-foreground focus:border-primary outline-none"
                />
                {inputModel && (
                  <button onClick={clearModelFilter} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground">
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
              <div className="relative flex-1 min-w-0">
                <Server className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
                <input
                  type="text" placeholder="渠道名" value={inputProvider}
                  onChange={e => setInputProvider(e.target.value)}
                  className="w-full bg-background border border-border text-sm pl-8 pr-7 py-2 rounded-lg text-foreground placeholder:text-muted-foreground focus:border-primary outline-none"
                />
                {inputProvider && (
                  <button onClick={clearProviderFilter} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground">
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
              <div className="relative flex-1 min-w-0">
                <Key className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
                <input
                  type="text" placeholder="Key 名称 / 分组" value={inputApiKey}
                  onChange={e => setInputApiKey(e.target.value)}
                  className="w-full bg-background border border-border text-sm pl-8 pr-7 py-2 rounded-lg text-foreground placeholder:text-muted-foreground focus:border-primary outline-none"
                />
                {inputApiKey && (
                  <button onClick={clearApiKeyFilter} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground">
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
              <select
                value={filterSuccess} onChange={e => setFilterSuccess(e.target.value)}
                className="bg-background border border-border text-sm px-3 py-2 rounded-lg text-foreground sm:w-[110px] flex-shrink-0"
              >
                <option value="ALL">全部状态</option>
                <option value="SUCCESS">成功</option>
                <option value="FAILED">失败</option>
              </select>
            </div>

            {/* 第二排：时间筛选 */}
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2">
              <div className="flex items-center gap-1 flex-shrink-0">
                <Calendar className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
                {TIME_PRESETS.map(preset => (
                  <button
                    key={preset.hours}
                    onClick={() => {
                      if (filterTimePreset === preset.hours) {
                        setFilterTimePreset(null);
                      } else {
                        setFilterTimePreset(preset.hours);
                        setFilterStartTime('');
                        setFilterEndTime('');
                      }
                    }}
                    className={`px-2 py-1 text-xs rounded-md transition-colors ${
                      filterTimePreset === preset.hours
                        ? 'bg-primary text-primary-foreground'
                        : 'bg-muted text-muted-foreground hover:text-foreground hover:bg-muted/80'
                    }`}
                  >
                    {preset.label}
                  </button>
                ))}
              </div>

              <span className="hidden sm:block text-muted-foreground/40 text-xs">|</span>

              <div className="flex items-center gap-1.5 flex-1 min-w-0">
                <input
                  type="datetime-local"
                  value={toLocalDatetimeStr(filterStartTime)}
                  onChange={e => { setFilterStartTime(fromLocalDatetimeStr(e.target.value)); setFilterTimePreset(null); }}
                  className="bg-background border border-border text-xs px-2 py-1.5 rounded-lg text-foreground flex-1 min-w-0"
                  title="开始时间"
                />
                <span className="text-muted-foreground text-xs flex-shrink-0">至</span>
                <input
                  type="datetime-local"
                  value={toLocalDatetimeStr(filterEndTime)}
                  onChange={e => { setFilterEndTime(fromLocalDatetimeStr(e.target.value)); setFilterTimePreset(null); }}
                  className="bg-background border border-border text-xs px-2 py-1.5 rounded-lg text-foreground flex-1 min-w-0"
                  title="结束时间"
                />
              </div>

              <div className="flex items-center gap-2 flex-shrink-0">
                {hasActiveFilters && (
                  <button
                    onClick={clearAllFilters}
                    className="flex items-center gap-1 px-2 py-1.5 text-xs text-muted-foreground hover:text-foreground bg-muted hover:bg-muted/80 rounded-lg transition-colors"
                  >
                    <X className="w-3 h-3" /> 清除
                  </button>
                )}
                <div className="text-xs text-muted-foreground whitespace-nowrap">
                  共 <span className="font-mono text-foreground">{totalCount}</span> 条
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Logs List */}
      <div className="flex-1 overflow-auto space-y-2">
        {logs.length === 0 && !loading ? (
          <div className="flex flex-col items-center justify-center p-16 text-muted-foreground bg-card border border-border rounded-xl">
            <FileText className="w-12 h-12 mb-4 opacity-50" />
            <p>未找到匹配的日志</p>
          </div>
        ) : (
          logs.map((log) => <LogAccordionItem key={log.id} log={log} />)
        )}

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
