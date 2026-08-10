import { useEffect, useMemo, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import {
  X,
  Play,
  Square,
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
  Copy,
  CopyCheck,
  ChevronDown,
  ChevronUp,
  Settings2,
  ToggleLeft,
  ToggleRight,
  Trash2,
} from 'lucide-react';
import { apiFetch } from '../lib/api';
import { formatApiKeyTestError, getInitialApiKeyTestModel, normalizeApiKeyTestModels } from '../lib/apiKeyTestDialog';
import { toastWarning } from '../components/Toast';
import { useAuthStore } from '../store/authStore';

export interface ApiKeyObj {
  _clientId: string;
  key: string;
  disabled: boolean;
}

interface KeyTestResult {
  status: 'pending' | 'testing' | 'success' | 'error';
  latency_ms?: number | null;
  upstream_status_code?: number | null;
  auth_failed?: boolean;
  error?: string | null;
  response_preview?: string | null;
}

export interface ApiKeyTestDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;

  title?: string;

  engine: string;
  base_url: string;
  provider_snapshot: any;

  apiKeys: ApiKeyObj[];
  availableModels: string[];

  initialKeyIndex?: number | null;

  /** 把「失效 key」标记为 disabled（仅修改当前编辑中的 formData，保存后生效） */
  onDisableKeys?: (indices: number[]) => void;
  /** 切换单个 key 的启用/禁用状态（保存后生效） */
  onToggleKeyDisabled?: (idx: number) => void;
  /** 删除单个 key（保存后生效；OAuth 账号可能即时生效，由调用方决定） */
  onDeleteKey?: (idx: number) => void;
}

export function ApiKeyTestDialog({
  open,
  onOpenChange,
  title,
  engine,
  base_url,
  provider_snapshot,
  apiKeys,
  availableModels,
  initialKeyIndex,
  onDisableKeys,
  onToggleKeyDisabled,
  onDeleteKey,
}: ApiKeyTestDialogProps) {
  const { token } = useAuthStore();

  const [model, setModel] = useState('');
  const [temperature, setTemperature] = useState(0.5);
  const [stream, setStream] = useState(false);
  const [maxTokens, setMaxTokens] = useState(16);
  const [timeoutSec, setTimeoutSec] = useState(30);
  const [concurrency, setConcurrency] = useState(3);

  const [includeDisabled, setIncludeDisabled] = useState(false);
  const [autoDisableInvalid, setAutoDisableInvalid] = useState(true);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const [isRunning, setIsRunning] = useState(false);
  const openRef = useRef(open);
  const lifecycleEpochRef = useRef(0);
  const nextBatchRunIdRef = useRef(0);
  const activeBatchRunIdRef = useRef<number | null>(null);
  const requestVersionsRef = useRef(new Map<string, number>());
  const activeRequestsRef = useRef(new Map<string, { controller: AbortController; batchRunId: number | null; clientId: string }>());
  const autoTestTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const autoDisableInvalidRef = useRef(autoDisableInvalid);
  // 异步测试完成时必须读取最新 Key 列表，不能使用发起请求那次渲染的旧数组。
  const apiKeysRef = useRef(apiKeys);
  apiKeysRef.current = apiKeys;
  openRef.current = open;
  autoDisableInvalidRef.current = autoDisableInvalid;

  // 所有测试及交互状态按稳定 client id 保存，数组删除/重排不会把旧状态顶给后继 Key。
  const [results, setResults] = useState<Map<string, KeyTestResult>>(new Map());
  const [lastPreviewKeyId, setLastPreviewKeyId] = useState<string | null>(null);
  const [copiedKeyId, setCopiedKeyId] = useState<string | null>(null);
  // 错误详情不再依赖 title 悬浮提示：移动端没有 hover，且 title 文本无法复制。
  // 这里记录展开的 Key 和复制反馈，让每个 Key 的错误可以内联展开、选择并复制，同时保持默认列表简洁。
  const [expandedErrorKeyId, setExpandedErrorKeyId] = useState<string | null>(null);
  const [copiedErrorKeyId, setCopiedErrorKeyId] = useState<string | null>(null);

  // 修改原因：模型列表归一化逻辑需要和回归测试共用，避免弹窗重新打开后使用旧渠道模型。
  // 修改方式：改为调用纯 helper 去重、去空白，并保留当前渠道传入列表的顺序。
  // 目的：选择框和自动测试入口都基于同一份当前渠道模型列表。
  const modelOptions = useMemo(() => normalizeApiKeyTestModels(availableModels), [availableModels]);

  // 弹窗每次打开/关闭都切换生命周期代际，并取消上一代的定时器和请求。
  useEffect(() => {
    lifecycleEpochRef.current += 1;
    activeBatchRunIdRef.current = null;
    setIsRunning(false);
    if (autoTestTimerRef.current) clearTimeout(autoTestTimerRef.current);
    autoTestTimerRef.current = null;
    activeRequestsRef.current.forEach(({ controller }) => controller.abort());
    activeRequestsRef.current.clear();
    requestVersionsRef.current.clear();
    if (!open) return;

    // 修改原因：自动单 Key 测试会在同一个 effect 中触发，不能依赖 setModel 立即同步到闭包。
    // 修改方式：先从当前渠道 availableModels 解析 firstModel，再同时写入状态并传给自动测试调用。
    // 目的：避免请求体里的 model 落回上一次打开弹窗时缓存的模型名。
    const firstModel = getInitialApiKeyTestModel(availableModels);
    setModel(firstModel);

    const init = new Map<string, KeyTestResult>();
    apiKeys.forEach(keyObj => {
      init.set(keyObj._clientId, { status: 'pending' });
    });
    setResults(init);
    setLastPreviewKeyId(null);
    setCopiedKeyId(null);
    // 重新打开弹窗时清空旧错误面板，避免用户看到上一次测试的详情。
    setExpandedErrorKeyId(null);
    setCopiedErrorKeyId(null);

    // 如果是单 key 测试入口，先把入口下标转换成稳定身份再自动触发。
    if (typeof initialKeyIndex === 'number' && initialKeyIndex >= 0) {
      const initialClientId = apiKeys[initialKeyIndex]?._clientId;
      if (initialClientId) autoTestTimerRef.current = setTimeout(() => {
        autoTestTimerRef.current = null;
        // 修改原因：这里的 testSingleKey 闭包仍可能读到 setModel 前的旧状态。
        // 修改方式：把当前渠道解析出的 firstModel 作为本次自动测试的显式覆盖值传入。
        // 目的：从 Key 行点击自动测试时，请求始终使用当前渠道模型。
        void testSingleKey(initialClientId, firstModel);
      }, 50);
    }

    return () => {
      if (autoTestTimerRef.current) clearTimeout(autoTestTimerRef.current);
      autoTestTimerRef.current = null;
      lifecycleEpochRef.current += 1;
      activeBatchRunIdRef.current = null;
        activeRequestsRef.current.forEach(({ controller }) => controller.abort());
      activeRequestsRef.current.clear();
    };
  }, [open]);

  // 弹窗打开期间删除或新增 Key 时，只保留仍存在 id 的结果并初始化新增项。
  useEffect(() => {
    if (!open) return;
    const activeIds = new Set(apiKeys.map(keyObj => keyObj._clientId));
    setResults(prev => {
      const next = new Map<string, KeyTestResult>();
      apiKeys.forEach(keyObj => next.set(keyObj._clientId, prev.get(keyObj._clientId) || { status: 'pending' }));
      return next;
    });
    setLastPreviewKeyId(current => current && activeIds.has(current) ? current : null);
    setCopiedKeyId(current => current && activeIds.has(current) ? current : null);
    setExpandedErrorKeyId(current => current && activeIds.has(current) ? current : null);
    setCopiedErrorKeyId(current => current && activeIds.has(current) ? current : null);
  }, [open, apiKeys]);

  const canRun = () => {
    const hasModel = Boolean(model.trim());
    const hasKey = apiKeys.some(k => (includeDisabled || !k.disabled) && k.key.trim());
    return hasModel && hasKey;
  };

  // 单 key 手动/自动测试增加 allowDisabled 参数（默认放行）；只有"测试全部"批量入口才继续遵守 includeDisabled。
  // 目的：解耦"批量测试范围"与"单个 key 手动测试"，方便用户按需验证任意一把 key。
  const testSingleKey = async (clientId: string, modelOverride?: string, allowDisabled = true, batchRunId: number | null = null) => {
    const keyObj = apiKeysRef.current.find(item => item._clientId === clientId);
    if (!keyObj) return;
    if (!allowDisabled && !includeDisabled && keyObj.disabled) return;

    const apiKey = keyObj.key.trim();
    if (!apiKey || !openRef.current) return;

    const requestEpoch = lifecycleEpochRef.current;
    const requestVersion = (requestVersionsRef.current.get(clientId) || 0) + 1;
    requestVersionsRef.current.set(clientId, requestVersion);
    const requestId = `${requestEpoch}:${clientId}:${requestVersion}`;
    const controller = new AbortController();
    activeRequestsRef.current.set(requestId, { controller, batchRunId, clientId });
    const isCurrentRequest = () => {
      if (!openRef.current || lifecycleEpochRef.current !== requestEpoch) return false;
      if (requestVersionsRef.current.get(clientId) !== requestVersion) return false;
      if (batchRunId !== null && activeBatchRunIdRef.current !== batchRunId) return false;
      const currentKey = apiKeysRef.current.find(item => item._clientId === clientId);
      return currentKey?.key.trim() === apiKey;
    };

    // 修改原因：自动测试入口需要绕过 React 状态更新延迟，手动测试入口仍应读取当前选择框状态。
    // 修改方式：优先使用调用方传入的 modelOverride，否则回退到组件当前 model 状态。
    // 目的：同一个测试函数同时支持自动打开测试和用户点击测试两种路径。
    const requestModel = (modelOverride ?? model).trim();

    setResults(prev => {
      const next = new Map(prev);
      next.set(clientId, { status: 'testing', latency_ms: null, error: null });
      return next;
    });

    try {
      const res = await apiFetch('/v1/channels/test', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          engine: engine || 'openai',
          base_url,
          provider_snapshot,
          api_key: apiKey,
          model: requestModel,
          temperature,
          stream,
          max_tokens: maxTokens,
          timeout: timeoutSec,
        }),
        signal: controller.signal,
      });

      const data = await res.json().catch(() => ({} as any));
      if (!isCurrentRequest()) return;

      if (res.ok && data?.success) {
        setResults(prev => {
          if (!isCurrentRequest()) return prev;
          const next = new Map(prev);
          next.set(clientId, {
            status: 'success',
            latency_ms: data.latency_ms ?? null,
            upstream_status_code: data.upstream_status_code ?? null,
            auth_failed: Boolean(data.auth_failed),
            error: null,
            response_preview: data.response_preview ?? null,
          });
          return next;
        });
        if (data.response_preview && isCurrentRequest()) setLastPreviewKeyId(clientId);
        return;
      }

      const errMsg = formatApiKeyTestError(data, `HTTP ${res.status}`);
      const authFailed = Boolean(data?.auth_failed);
      setResults(prev => {
        if (!isCurrentRequest()) return prev;
        const next = new Map(prev);
        next.set(clientId, {
          status: 'error',
          latency_ms: data?.latency_ms ?? null,
          upstream_status_code: data?.upstream_status_code ?? null,
          auth_failed: authFailed,
          error: errMsg,
          response_preview: data?.response_preview ?? null,
        });
        return next;
      });

      if (autoDisableInvalidRef.current && authFailed && onDisableKeys && isCurrentRequest()) {
        const currentIndex = apiKeysRef.current.findIndex(item => item._clientId === clientId && item.key.trim() === apiKey);
        if (currentIndex >= 0) onDisableKeys([currentIndex]);
      }
    } catch (e: any) {
      if (!isCurrentRequest()) return;
      setResults(prev => {
        if (!isCurrentRequest()) return prev;
        const next = new Map(prev);
        next.set(clientId, e?.name === 'AbortError'
          ? { status: 'pending' }
          : { status: 'error', error: formatApiKeyTestError(e, '请求失败') });
        return next;
      });
    } finally {
      activeRequestsRef.current.delete(requestId);
    }
  };

  const startAll = async () => {
    if (!canRun()) {
      toastWarning('请先设置模型，并确保至少有一个可测试的 Key');
      return;
    }

    const batchRunId = ++nextBatchRunIdRef.current;
    activeBatchRunIdRef.current = batchRunId;
    setIsRunning(true);

    // reset
    setResults(prev => {
      const next = new Map(prev);
      apiKeys.forEach(keyObj => {
        next.set(keyObj._clientId, { status: 'pending' });
      });
      return next;
    });

    const queue = apiKeys
      .filter(k => (includeDisabled || !k.disabled) && Boolean(k.key.trim()))
      .map(k => k._clientId);

    const runNext = async () => {
      while (queue.length > 0) {
        if (activeBatchRunIdRef.current !== batchRunId) return;
        const clientId = queue.shift();
        if (clientId === undefined) return;
        // 批量"测试全部"仍遵守 includeDisabled（queue 已按该开关过滤），显式传 allowDisabled=false。
        await testSingleKey(clientId, undefined, false, batchRunId);
      }
    };

    const tasks: Promise<void>[] = [];
    for (let i = 0; i < Math.max(1, Math.min(10, concurrency)); i++) {
      tasks.push(runNext());
    }

    await Promise.all(tasks);
    // 旧批次结束时不能覆盖用户刚启动的新批次运行态。
    if (activeBatchRunIdRef.current === batchRunId) {
      activeBatchRunIdRef.current = null;
        setIsRunning(false);
    }
  };

  const stopAll = () => {
    const batchRunId = activeBatchRunIdRef.current;
    activeBatchRunIdRef.current = null;
    setIsRunning(false);
    if (batchRunId === null) return;
    const stoppedClientIds = new Set<string>();
    activeRequestsRef.current.forEach(({ controller, batchRunId: ownerRunId, clientId }) => {
      if (ownerRunId === batchRunId) {
        stoppedClientIds.add(clientId);
        controller.abort();
      }
    });
    setResults(prev => {
      const next = new Map(prev);
      stoppedClientIds.forEach(clientId => {
        if (next.get(clientId)?.status === 'testing') next.set(clientId, { status: 'pending' });
      });
      return next;
    });
  };

  const copyKey = (clientId: string) => {
    const apiKey = apiKeysRef.current.find(item => item._clientId === clientId)?.key?.trim();
    if (!apiKey) return;
    navigator.clipboard.writeText(apiKey);
    setCopiedKeyId(clientId);
    setTimeout(() => setCopiedKeyId(null), 1500);
  };

  const copyErrorText = async (clientId: string, errorText: string) => {
    try {
      await navigator.clipboard.writeText(errorText);
      setCopiedErrorKeyId(clientId);
      setTimeout(() => setCopiedErrorKeyId(null), 1500);
    } catch (error) {
      // 复制失败只记录到控制台，避免用弹窗或临时 Toast 打断批量 Key 测试流程。
      console.error('Failed to copy API key test error', error);
    }
  };

  const statusIcon = (r: KeyTestResult, small = false) => {
    const cls = small ? 'w-4 h-4' : 'w-[18px] h-[18px]';
    switch (r.status) {
      case 'pending':
        return <Clock className={`${cls} text-muted-foreground`} />;
      case 'testing':
        return <Loader2 className={`${cls} text-blue-500 animate-spin`} />;
      case 'success':
        return <CheckCircle2 className={`${cls} text-emerald-500`} />;
      case 'error':
        return <XCircle className={`${cls} text-red-500`} />;
    }
  };

  const currentResults = apiKeys.map(keyObj => results.get(keyObj._clientId)).filter((result): result is KeyTestResult => Boolean(result));
  const successCount = currentResults.filter(r => r.status === 'success').length;
  const errorCount = currentResults.filter(r => r.status === 'error').length;
  const testingCount = currentResults.filter(r => r.status === 'testing').length;
  const totalTestable = apiKeys.filter(k => (includeDisabled || !k.disabled) && k.key.trim()).length;

  // 将 key 文本脱敏显示：保留前6后4，中间用 *** 代替
  const maskKey = (key: string) => {
    if (key.length <= 12) return key;
    return `${key.slice(0, 6)}***${key.slice(-4)}`;
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-[80] animate-in fade-in duration-200" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[720px] max-w-[96vw] max-h-[88vh] bg-background border border-border rounded-xl shadow-2xl z-[90] flex flex-col">
          {/* ── Header ── */}
          <div className="px-5 py-4 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
            <div className="min-w-0">
              <Dialog.Title className="text-base font-bold text-foreground truncate">
                {title || 'API Key 测试'}
              </Dialog.Title>
              <p className="text-xs text-muted-foreground mt-0.5 truncate">
                <span className="font-mono">{engine || 'openai'}</span>
                {base_url && <span className="ml-1.5">· {base_url}</span>}
              </p>
            </div>
            <Dialog.Close className="text-muted-foreground hover:text-foreground flex-shrink-0 ml-3">
              <X className="w-5 h-5" />
            </Dialog.Close>
          </div>

          {/* ── Controls ── */}
          <div className="px-5 py-3 border-b border-border flex flex-col gap-2.5 flex-shrink-0">
            {/* 第一行：操作按钮 + 模型选择 */}
            <div className="flex items-center gap-2.5">
              {!isRunning ? (
                <button
                  onClick={startAll}
                  disabled={!canRun()}
                  className="bg-primary hover:bg-primary/90 text-primary-foreground px-3.5 py-1.5 rounded-lg flex items-center gap-1.5 text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0"
                >
                  <Play className="w-3.5 h-3.5" /> 测试全部
                </button>
              ) : (
                <button
                  onClick={stopAll}
                  className="bg-red-500/10 border border-red-500/40 text-red-600 dark:text-red-400 hover:bg-red-500/20 px-3.5 py-1.5 rounded-lg flex items-center gap-1.5 text-sm font-medium transition-colors flex-shrink-0"
                >
                  <Square className="w-3.5 h-3.5" /> 停止
                </button>
              )}

              {/* 模型选择 - 占据剩余宽度 */}
              <div className="flex-1 min-w-0">
                {modelOptions.length > 0 ? (
                  <select
                    value={model}
                    onChange={e => setModel(e.target.value)}
                    className="w-full bg-background border border-border rounded-lg px-3 py-1.5 text-sm font-mono text-foreground truncate"
                  >
                    {modelOptions.map(m => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    value={model}
                    onChange={e => setModel(e.target.value)}
                    placeholder="输入测试模型名，如 gpt-4o-mini"
                    className="w-full bg-background border border-border rounded-lg px-3 py-1.5 text-sm font-mono text-foreground"
                  />
                )}
              </div>

              {/* 高级参数折叠按钮 */}
              <button
                onClick={() => setShowAdvanced(v => !v)}
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground px-2 py-1.5 rounded-lg hover:bg-muted transition-colors flex-shrink-0"
              >
                <Settings2 className="w-3.5 h-3.5" />
                <span className="hidden sm:inline">参数</span>
                {showAdvanced ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
              </button>
            </div>

            {/* 第二行：高级参数（可折叠） */}
            {showAdvanced && (
              <div className="grid grid-cols-4 gap-2.5 p-3 bg-muted/40 rounded-lg border border-border">
                <div>
                  <label className="text-[10px] text-muted-foreground block mb-1">并发数</label>
                  <input
                    type="number" min={1} max={10} value={concurrency}
                    onChange={e => setConcurrency(Math.max(1, Math.min(10, parseInt(e.target.value) || 1)))}
                    className="w-full bg-background border border-border rounded px-2 py-1 text-center text-xs font-mono text-foreground"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-muted-foreground block mb-1">超时 (秒)</label>
                  <input
                    type="number" min={1} max={120} value={timeoutSec}
                    onChange={e => setTimeoutSec(Math.max(1, Math.min(120, parseInt(e.target.value) || 30)))}
                    className="w-full bg-background border border-border rounded px-2 py-1 text-center text-xs font-mono text-foreground"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-muted-foreground block mb-1">Max Tokens</label>
                  <input
                    type="number" min={1} max={2048} value={maxTokens}
                    onChange={e => setMaxTokens(Math.max(1, Math.min(2048, parseInt(e.target.value) || 16)))}
                    className="w-full bg-background border border-border rounded px-2 py-1 text-center text-xs font-mono text-foreground"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-muted-foreground block mb-1">温度</label>
                  <input
                    type="number" step={0.1} min={0} max={2} value={temperature}
                    onChange={e => setTemperature(Math.max(0, Math.min(2, parseFloat(e.target.value) || 0)))}
                    className="w-full bg-background border border-border rounded px-2 py-1 text-center text-xs font-mono text-foreground"
                  />
                </div>

                {/* 选项 checkbox 行 */}
                <div className="col-span-4 flex flex-wrap items-center gap-x-5 gap-y-1 pt-2 border-t border-border mt-1 text-xs">
                  <label className="inline-flex items-center gap-1.5 cursor-pointer">
                    <input type="checkbox" checked={stream} onChange={e => setStream(e.target.checked)} className="rounded" />
                    <span className="text-foreground">流式</span>
                  </label>
                  <label className="inline-flex items-center gap-1.5 cursor-pointer">
                    <input type="checkbox" checked={includeDisabled} onChange={e => setIncludeDisabled(e.target.checked)} className="rounded" />
                    <span className="text-foreground">包含已禁用</span>
                  </label>
                  <label className="inline-flex items-center gap-1.5 cursor-pointer">
                    <input type="checkbox" checked={autoDisableInvalid} onChange={e => setAutoDisableInvalid(e.target.checked)} className="rounded" />
                    <span className="text-foreground">401/403 自动禁用</span>
                  </label>
                  {autoDisableInvalid && (
                    <span className="text-muted-foreground">（需保存后生效）</span>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* ── Key List ── */}
          <div className="flex-1 overflow-y-auto min-h-0">
            {apiKeys.length === 0 ? (
              <div className="flex items-center justify-center py-16 text-sm text-muted-foreground">
                暂无 API Key
              </div>
            ) : (
              <div className="divide-y divide-border">
                {apiKeys.map((k, idx) => {
                  const clientId = k._clientId;
                  const r = results.get(clientId) || { status: 'pending' as const };
                  const keyText = k.key?.trim() || '';
                  const isSkipped = !includeDisabled && k.disabled;

                  const errorText = r.error || '测试失败';
                  const errorSummary = errorText.length > 36 ? `${errorText.slice(0, 36)}...` : errorText;
                  const isErrorExpanded = r.status === 'error' && expandedErrorKeyId === clientId;

                  return (
                    <div
                      key={clientId}
                      className={`group transition-colors hover:bg-muted/30 ${isSkipped ? 'opacity-40' : ''}`}
                    >
                      <div className="flex items-center gap-2 px-4 py-2">
                        {/* 状态图标 */}
                        <div className="w-6 flex items-center justify-center flex-shrink-0">
                          {statusIcon(r)}
                        </div>

                        {/* 序号 */}
                        <span className="text-[11px] text-muted-foreground w-5 text-right flex-shrink-0 font-mono tabular-nums">
                          {idx + 1}
                        </span>

                        {/* Key 文本 + 状态信息 */}
                        <div className="flex-1 min-w-0 flex items-center gap-2">
                          <span
                            className={`font-mono text-xs truncate ${
                              k.disabled ? 'line-through text-muted-foreground' : 'text-foreground'
                            }`}
                            title={keyText}
                          >
                            {keyText ? maskKey(keyText) : '(空)'}
                          </span>

                          {/* 复制按钮 */}
                          {keyText && (
                            copiedKeyId === clientId ? (
                              <CopyCheck className="w-3 h-3 text-emerald-500 flex-shrink-0" />
                            ) : (
                              <button
                                className="text-muted-foreground hover:text-foreground opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0"
                                onClick={() => copyKey(clientId)}
                                title="复制 Key"
                              >
                                <Copy className="w-3 h-3" />
                              </button>
                            )
                          )}
                        </div>

                        {/* 测试结果信息 */}
                        <div className="flex items-center gap-1.5 flex-shrink-0 min-w-0 max-w-[260px]">
                          {r.status === 'success' && (
                            <span className="text-[11px] font-mono text-emerald-600 dark:text-emerald-400 flex-shrink-0">
                              {r.latency_ms ?? '-'}ms
                              {r.upstream_status_code ? ` · ${r.upstream_status_code}` : ''}
                            </span>
                          )}
                          {r.status === 'error' && (
                            <button
                              type="button"
                              onClick={() => setExpandedErrorKeyId(current => current === clientId ? null : clientId)}
                              className="max-w-full truncate text-left text-[11px] text-red-600 dark:text-red-400 hover:underline"
                            >
                              {isErrorExpanded ? '收起错误详情' : `${r.auth_failed ? '[auth] ' : ''}查看错误：${errorSummary}`}
                            </button>
                          )}
                          {r.status === 'testing' && (
                            <span className="text-[11px] text-blue-500">测试中</span>
                          )}
                        </div>

                        {/* 单个测试按钮 */}
                        {/* 未勾选"包含已禁用"时也应能测试单个（包括已禁用）key。
                            按钮不再因 isSkipped 而禁用，testSingleKey 默认 allowDisabled=true。 */}
                        <button
                          onClick={() => void testSingleKey(clientId)}
                          disabled={r.status === 'testing' || !keyText}
                          className="p-1.5 rounded-md text-primary hover:bg-primary/10 disabled:opacity-30 disabled:cursor-not-allowed transition-colors flex-shrink-0"
                          title="测试此 Key"
                        >
                          <Play className="w-3.5 h-3.5" />
                        </button>

                        {/* 在测试按钮旁新增开关和删除按钮，仅在回调存在时渲染（保存后生效）。 */}
                        {onToggleKeyDisabled && (
                          <button
                            onClick={() => onToggleKeyDisabled(idx)}
                            className={`p-1 flex-shrink-0 transition-colors ${k.disabled ? 'text-muted-foreground hover:text-foreground' : 'text-emerald-500 hover:text-emerald-600'}`}
                            title={k.disabled ? '启用此 Key' : '禁用此 Key'}
                          >
                            {k.disabled ? <ToggleLeft className="w-4 h-4" /> : <ToggleRight className="w-4 h-4" />}
                          </button>
                        )}
                        {onDeleteKey && (
                          <button
                            onClick={() => onDeleteKey(idx)}
                            className="p-1 text-red-500 hover:text-red-600 transition-colors flex-shrink-0"
                            title="删除此 Key"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </div>

                      {isErrorExpanded && (
                        <div className="mx-4 mb-2 ml-14 rounded-lg border border-red-500/20 bg-red-500/5 p-3">
                          {/* 错误正文使用 pre 保留换行，并开启 select-text；这样手机端和桌面端都能完整查看并选择复制。 */}
                          <div className="flex items-center justify-between gap-3 text-[11px] text-red-600 dark:text-red-400">
                            <span>Key #{idx + 1} 错误详情</span>
                            <button
                              type="button"
                              onClick={() => void copyErrorText(clientId, errorText)}
                              className="inline-flex items-center gap-1 rounded-md border border-red-500/20 bg-background px-2 py-1 hover:bg-red-500/10 transition-colors"
                            >
                              {copiedErrorKeyId === clientId ? <CopyCheck className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                              {copiedErrorKeyId === clientId ? '已复制' : '复制'}
                            </button>
                          </div>
                          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words select-text text-[11px] leading-relaxed text-red-700 dark:text-red-300 font-mono">{errorText}</pre>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* 响应预览区（仅显示最近一个有 preview 的结果） */}
            {lastPreviewKeyId != null && results.get(lastPreviewKeyId)?.response_preview && (
              <div className="mx-4 my-2 p-2.5 bg-muted/40 border border-border rounded-lg">
                <div className="text-[10px] text-muted-foreground mb-1">Key #{apiKeys.findIndex(item => item._clientId === lastPreviewKeyId) + 1} 响应预览</div>
                <pre className="text-[11px] max-h-[100px] overflow-auto whitespace-pre-wrap text-foreground">
                  {results.get(lastPreviewKeyId)!.response_preview}
                </pre>
              </div>
            )}
          </div>

          {/* ── Footer ── */}
          <div className="px-5 py-3 border-t border-border bg-muted/30 flex-shrink-0 flex items-center justify-between gap-4">
            <div className="flex items-center gap-3 text-xs">
              <span className="text-muted-foreground">
                共 <span className="font-mono text-foreground">{totalTestable}</span>/{apiKeys.length} 可测
              </span>
              {successCount > 0 && (
                <span className="flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
                  <CheckCircle2 className="w-3 h-3" /> {successCount}
                </span>
              )}
              {errorCount > 0 && (
                <span className="flex items-center gap-1 text-red-600 dark:text-red-400">
                  <XCircle className="w-3 h-3" /> {errorCount}
                </span>
              )}
              {testingCount > 0 && (
                <span className="flex items-center gap-1 text-blue-500">
                  <Loader2 className="w-3 h-3 animate-spin" /> {testingCount}
                </span>
              )}
            </div>
            <Dialog.Close className="px-3 py-1 text-xs text-muted-foreground hover:text-foreground bg-muted hover:bg-muted/80 rounded-md transition-colors">
              关闭
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
