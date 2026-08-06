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
import { toastSuccess, toastError, toastWarning } from '../components/Toast';
import { useAuthStore } from '../store/authStore';

export interface ApiKeyObj {
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
  const runningRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);

  const [results, setResults] = useState<Map<number, KeyTestResult>>(new Map());
  const [lastPreviewIdx, setLastPreviewIdx] = useState<number | null>(null);
  const [copiedKeyIndex, setCopiedKeyIndex] = useState<number | null>(null);
  // 错误详情不再依赖 title 悬浮提示：移动端没有 hover，且 title 文本无法复制。
  // 这里记录展开的 Key 和复制反馈，让每个 Key 的错误可以内联展开、选择并复制，同时保持默认列表简洁。
  const [expandedErrorKeyIndex, setExpandedErrorKeyIndex] = useState<number | null>(null);
  const [copiedErrorKeyIndex, setCopiedErrorKeyIndex] = useState<number | null>(null);

  // 修改原因：模型列表归一化逻辑需要和回归测试共用，避免弹窗重新打开后使用旧渠道模型。
  // 修改方式：改为调用纯 helper 去重、去空白，并保留当前渠道传入列表的顺序。
  // 目的：选择框和自动测试入口都基于同一份当前渠道模型列表。
  const modelOptions = useMemo(() => normalizeApiKeyTestModels(availableModels), [availableModels]);

  // 弹窗打开时初始化
  useEffect(() => {
    if (!open) return;

    // 修改原因：自动单 Key 测试会在同一个 effect 中触发，不能依赖 setModel 立即同步到闭包。
    // 修改方式：先从当前渠道 availableModels 解析 firstModel，再同时写入状态并传给自动测试调用。
    // 目的：避免请求体里的 model 落回上一次打开弹窗时缓存的模型名。
    const firstModel = getInitialApiKeyTestModel(availableModels);
    setModel(firstModel);

    const init = new Map<number, KeyTestResult>();
    apiKeys.forEach((_, idx) => {
      init.set(idx, { status: 'pending' });
    });
    setResults(init);
    // 重新打开弹窗时清空旧错误面板，避免用户看到上一次测试的详情。
    setExpandedErrorKeyIndex(null);
    setCopiedErrorKeyIndex(null);

    // 如果是单 key 测试入口，自动触发
    if (typeof initialKeyIndex === 'number' && initialKeyIndex >= 0) {
      setTimeout(() => {
        // 修改原因：这里的 testSingleKey 闭包仍可能读到 setModel 前的旧状态。
        // 修改方式：把当前渠道解析出的 firstModel 作为本次自动测试的显式覆盖值传入。
        // 目的：从 Key 行点击自动测试时，请求始终使用当前渠道模型。
        void testSingleKey(initialKeyIndex, firstModel);
      }, 50);
    }
  }, [open]);

  useEffect(() => {
    if (!open && isRunning) {
      stopAll();
    }
  }, [open]);

  const canRun = () => {
    const hasModel = Boolean(model.trim());
    const hasKey = apiKeys.some(k => (includeDisabled || !k.disabled) && k.key.trim());
    return hasModel && hasKey;
  };

  // 单 key 手动/自动测试增加 allowDisabled 参数（默认放行）；只有"测试全部"批量入口才继续遵守 includeDisabled。
  // 目的：解耦"批量测试范围"与"单个 key 手动测试"，方便用户按需验证任意一把 key。
  const testSingleKey = async (idx: number, modelOverride?: string, allowDisabled = true) => {
    const keyObj = apiKeys[idx];
    if (!keyObj) return;
    if (!allowDisabled && !includeDisabled && keyObj.disabled) return;

    const apiKey = keyObj.key.trim();
    if (!apiKey) return;

    // 修改原因：自动测试入口需要绕过 React 状态更新延迟，手动测试入口仍应读取当前选择框状态。
    // 修改方式：优先使用调用方传入的 modelOverride，否则回退到组件当前 model 状态。
    // 目的：同一个测试函数同时支持自动打开测试和用户点击测试两种路径。
    const requestModel = (modelOverride ?? model).trim();

    setResults(prev => {
      const next = new Map(prev);
      next.set(idx, { status: 'testing', latency_ms: null, error: null });
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
          // 修改原因：请求模型可能来自本次自动测试覆盖值，而不一定来自已同步的 React state。
          // 修改方式：统一发送前面解析出的 requestModel。
          // 目的：确保请求体中的 model 与当前渠道弹窗模型一致。
          model: requestModel,
          temperature,
          stream,
          max_tokens: maxTokens,
          timeout: timeoutSec,
        }),
        signal: abortControllerRef.current?.signal,
      });

      const data = await res.json().catch(() => ({} as any));

      if (res.ok && data?.success) {
        setResults(prev => {
          const next = new Map(prev);
          next.set(idx, {
            status: 'success',
            latency_ms: data.latency_ms ?? null,
            upstream_status_code: data.upstream_status_code ?? null,
            auth_failed: Boolean(data.auth_failed),
            error: null,
            response_preview: data.response_preview ?? null,
          });
          return next;
        });
        if (data.response_preview) setLastPreviewIdx(idx);
        return;
      }

      // 修改原因：测试接口失败时 detail/error/message 可能是对象，直接 String 会显示 [object Object]。
      // 修改方式：统一调用错误格式化 helper，并保留 HTTP 状态作为兜底文案。
      // 目的：错误摘要、详情和复制内容都能展示实际错误信息。
      const errMsg = formatApiKeyTestError(data, `HTTP ${res.status}`);
      const authFailed = Boolean(data?.auth_failed);

      setResults(prev => {
        const next = new Map(prev);
        next.set(idx, {
          status: 'error',
          latency_ms: data?.latency_ms ?? null,
          upstream_status_code: data?.upstream_status_code ?? null,
          auth_failed: authFailed,
          error: errMsg,
          response_preview: data?.response_preview ?? null,
        });
        return next;
      });

      if (autoDisableInvalid && authFailed && onDisableKeys) {
        onDisableKeys([idx]);
      }
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        setResults(prev => {
          const next = new Map(prev);
          next.set(idx, { status: 'pending' });
          return next;
        });
        return;
      }

      // 修改原因：网络异常或运行时异常也可能是普通对象，不能直接拼接或 String 化。
      // 修改方式：复用测试接口错误格式化 helper，把未知对象转换成可读文本。
      // 目的：保证 catch 分支不会在界面中显示 [object Object]。
      setResults(prev => {
        const next = new Map(prev);
        next.set(idx, {
          status: 'error',
          error: formatApiKeyTestError(e, '请求失败'),
        });
        return next;
      });
    }
  };

  const startAll = async () => {
    if (!canRun()) {
      toastWarning('请先设置模型，并确保至少有一个可测试的 Key');
      return;
    }

    runningRef.current = true;
    setIsRunning(true);
    abortControllerRef.current = new AbortController();

    // reset
    setResults(prev => {
      const next = new Map(prev);
      apiKeys.forEach((_, idx) => {
        next.set(idx, { status: 'pending' });
      });
      return next;
    });

    const queue = apiKeys
      .map((k, idx) => ({ k, idx }))
      .filter(({ k }) => (includeDisabled || !k.disabled) && Boolean(k.key.trim()))
      .map(({ idx }) => idx);

    const runNext = async () => {
      while (queue.length > 0) {
        if (!runningRef.current) return;
        const idx = queue.shift();
        if (idx === undefined) return;
        // 批量"测试全部"仍遵守 includeDisabled（queue 已按该开关过滤），显式传 allowDisabled=false。
        await testSingleKey(idx, undefined, false);
      }
    };

    const tasks: Promise<void>[] = [];
    for (let i = 0; i < Math.max(1, Math.min(10, concurrency)); i++) {
      tasks.push(runNext());
    }

    await Promise.all(tasks);
    runningRef.current = false;
    setIsRunning(false);
  };

  const stopAll = () => {
    runningRef.current = false;
    setIsRunning(false);
    abortControllerRef.current?.abort();
  };

  const copyKey = (idx: number) => {
    const apiKey = apiKeys[idx]?.key?.trim();
    if (!apiKey) return;
    navigator.clipboard.writeText(apiKey);
    setCopiedKeyIndex(idx);
    setTimeout(() => setCopiedKeyIndex(null), 1500);
  };

  const copyErrorText = async (idx: number, errorText: string) => {
    try {
      await navigator.clipboard.writeText(errorText);
      setCopiedErrorKeyIndex(idx);
      setTimeout(() => setCopiedErrorKeyIndex(null), 1500);
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

  const successCount = Array.from(results.values()).filter(r => r.status === 'success').length;
  const errorCount = Array.from(results.values()).filter(r => r.status === 'error').length;
  const testingCount = Array.from(results.values()).filter(r => r.status === 'testing').length;
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
                  const r = results.get(idx) || { status: 'pending' as const };
                  const keyText = k.key?.trim() || '';
                  const isSkipped = !includeDisabled && k.disabled;

                  const errorText = r.error || '测试失败';
                  const errorSummary = errorText.length > 36 ? `${errorText.slice(0, 36)}...` : errorText;
                  const isErrorExpanded = r.status === 'error' && expandedErrorKeyIndex === idx;

                  return (
                    <div
                      key={idx}
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
                            copiedKeyIndex === idx ? (
                              <CopyCheck className="w-3 h-3 text-emerald-500 flex-shrink-0" />
                            ) : (
                              <button
                                className="text-muted-foreground hover:text-foreground opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0"
                                onClick={() => copyKey(idx)}
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
                              onClick={() => setExpandedErrorKeyIndex(current => current === idx ? null : idx)}
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
                          onClick={() => void testSingleKey(idx)}
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
                              onClick={() => void copyErrorText(idx, errorText)}
                              className="inline-flex items-center gap-1 rounded-md border border-red-500/20 bg-background px-2 py-1 hover:bg-red-500/10 transition-colors"
                            >
                              {copiedErrorKeyIndex === idx ? <CopyCheck className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                              {copiedErrorKeyIndex === idx ? '已复制' : '复制'}
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
            {lastPreviewIdx != null && results.get(lastPreviewIdx)?.response_preview && (
              <div className="mx-4 my-2 p-2.5 bg-muted/40 border border-border rounded-lg">
                <div className="text-[10px] text-muted-foreground mb-1">Key #{lastPreviewIdx + 1} 响应预览</div>
                <pre className="text-[11px] max-h-[100px] overflow-auto whitespace-pre-wrap text-foreground">
                  {results.get(lastPreviewIdx)!.response_preview}
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
