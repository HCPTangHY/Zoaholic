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
} from 'lucide-react';
import { apiFetch } from '../lib/api';
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

  /** 用于把「失效 key」标记为 disabled（仅修改当前编辑中的 formData，保存后生效） */
  onDisableKeys?: (indices: number[]) => void;
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

  const [isRunning, setIsRunning] = useState(false);
  const runningRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);

  const [results, setResults] = useState<Map<number, KeyTestResult>>(new Map());
  const [copiedKeyIndex, setCopiedKeyIndex] = useState<number | null>(null);

  const modelOptions = useMemo(() => {
    const set = new Set<string>();
    (availableModels || []).forEach(m => {
      const s = String(m || '').trim();
      if (s) set.add(s);
    });
    return Array.from(set);
  }, [availableModels]);

  // open 时初始化
  useEffect(() => {
    if (!open) return;

    const firstModel = modelOptions[0] || '';
    setModel(prev => (prev ? prev : firstModel));

    const init = new Map<number, KeyTestResult>();
    apiKeys.forEach((_, idx) => {
      init.set(idx, { status: 'pending' });
    });
    setResults(init);

    // 自动触发单 key 测试（如果从单 key 按钮进入）
    if (typeof initialKeyIndex === 'number' && initialKeyIndex >= 0) {
      // 延迟一帧，避免弹窗开启动画期间卡顿
      setTimeout(() => {
        void testSingleKey(initialKeyIndex);
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

  const testSingleKey = async (idx: number) => {
    const keyObj = apiKeys[idx];
    if (!keyObj) return;
    if (!includeDisabled && keyObj.disabled) return;

    const apiKey = keyObj.key.trim();
    if (!apiKey) return;

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
          model: model.trim(),
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
        return;
      }

      const errMsg = data?.error || data?.detail || data?.message || `HTTP ${res.status}`;
      const authFailed = Boolean(data?.auth_failed);

      setResults(prev => {
        const next = new Map(prev);
        next.set(idx, {
          status: 'error',
          latency_ms: data?.latency_ms ?? null,
          upstream_status_code: data?.upstream_status_code ?? null,
          auth_failed: authFailed,
          error: String(errMsg),
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

      setResults(prev => {
        const next = new Map(prev);
        next.set(idx, {
          status: 'error',
          error: e?.message || String(e),
        });
        return next;
      });
    }
  };

  const startAll = async () => {
    if (!canRun()) {
      alert('请先设置模型，并确保至少有一个可测试的 Key');
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
        await testSingleKey(idx);
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

  const statusIcon = (r: KeyTestResult) => {
    switch (r.status) {
      case 'pending':
        return <Clock className="w-5 h-5 text-muted-foreground" />;
      case 'testing':
        return <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />;
      case 'success':
        return <CheckCircle2 className="w-5 h-5 text-emerald-500" />;
      case 'error':
        return <XCircle className="w-5 h-5 text-red-500" />;
    }
  };

  const successCount = Array.from(results.values()).filter(r => r.status === 'success').length;
  const errorCount = Array.from(results.values()).filter(r => r.status === 'error').length;

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-[80] animate-in fade-in duration-200" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[760px] max-w-[96vw] max-h-[90vh] bg-background border border-border rounded-xl shadow-2xl z-[90] flex flex-col">
          {/* Header */}
          <div className="p-5 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
            <Dialog.Title className="text-lg font-bold text-foreground">
              {title || 'API Key 测试'}
            </Dialog.Title>
            <Dialog.Close className="text-muted-foreground hover:text-foreground">
              <X className="w-5 h-5" />
            </Dialog.Close>
          </div>

          {/* Controls */}
          <div className="p-4 border-b border-border flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              {!isRunning ? (
                <button
                  onClick={startAll}
                  className="bg-primary hover:bg-primary/90 text-primary-foreground px-4 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
                >
                  <Play className="w-4 h-4" /> 测试全部 Key
                </button>
              ) : (
                <button
                  onClick={stopAll}
                  className="bg-red-500/10 border border-red-500/40 text-red-600 dark:text-red-400 hover:bg-red-500/20 px-4 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
                >
                  <Square className="w-4 h-4" /> 停止
                </button>
              )}

              <div className="flex items-center gap-2 text-sm">
                <span className="text-muted-foreground">并发</span>
                <input
                  type="number"
                  min={1}
                  max={10}
                  value={concurrency}
                  onChange={e => setConcurrency(Math.max(1, Math.min(10, parseInt(e.target.value) || 1)))}
                  className="w-16 bg-background border border-border rounded px-2 py-1 text-center text-sm text-foreground"
                />
              </div>

              <div className="flex items-center gap-2 text-sm">
                <span className="text-muted-foreground">超时(秒)</span>
                <input
                  type="number"
                  min={1}
                  max={120}
                  value={timeoutSec}
                  onChange={e => setTimeoutSec(Math.max(1, Math.min(120, parseInt(e.target.value) || 30)))}
                  className="w-20 bg-background border border-border rounded px-2 py-1 text-center text-sm text-foreground"
                />
              </div>

              <div className="flex items-center gap-2 text-sm">
                <span className="text-muted-foreground">Max Tokens</span>
                <input
                  type="number"
                  min={1}
                  max={2048}
                  value={maxTokens}
                  onChange={e => setMaxTokens(Math.max(1, Math.min(2048, parseInt(e.target.value) || 16)))}
                  className="w-24 bg-background border border-border rounded px-2 py-1 text-center text-sm text-foreground"
                />
              </div>

              <div className="flex items-center gap-2 text-sm">
                <span className="text-muted-foreground">温度</span>
                <input
                  type="number"
                  step={0.1}
                  min={0}
                  max={2}
                  value={temperature}
                  onChange={e => setTemperature(Math.max(0, Math.min(2, parseFloat(e.target.value) || 0)))}
                  className="w-20 bg-background border border-border rounded px-2 py-1 text-center text-sm text-foreground"
                />
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-muted-foreground block mb-1">模型</label>
                {modelOptions.length > 0 ? (
                  <select
                    value={model}
                    onChange={e => setModel(e.target.value)}
                    className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground"
                  >
                    {modelOptions.map(m => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    value={model}
                    onChange={e => setModel(e.target.value)}
                    placeholder={'例如：gpt-4o-mini'}
                    className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground"
                  />
                )}
              </div>

              <div className="flex flex-col gap-2">
                <label className="text-xs text-muted-foreground block">选项</label>
                <div className="flex flex-wrap items-center gap-4 text-sm">
                  <label className="inline-flex items-center gap-2">
                    <input type="checkbox" checked={stream} onChange={e => setStream(e.target.checked)} />
                    <span>流式输出</span>
                  </label>
                  <label className="inline-flex items-center gap-2">
                    <input type="checkbox" checked={includeDisabled} onChange={e => setIncludeDisabled(e.target.checked)} />
                    <span>包含已禁用 Key</span>
                  </label>
                  <label className="inline-flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={autoDisableInvalid}
                      onChange={e => setAutoDisableInvalid(e.target.checked)}
                    />
                    <span>鉴权失败自动禁用</span>
                  </label>
                </div>
                {autoDisableInvalid && (
                  <p className="text-xs text-muted-foreground">
                    说明：仅在后端判断 <span className="font-mono">auth_failed=true</span>（通常是 401/403）时，才会自动把 Key 标记为禁用；需要保存渠道配置后才会生效。
                  </p>
                )}
              </div>
            </div>

            
          </div>

          {/* List */}
          <div className="flex-1 overflow-y-auto">
            <ul className="divide-y divide-border">
              {apiKeys.map((k, idx) => {
                const r = results.get(idx) || { status: 'pending' as const };
                const keyText = k.key?.trim() || '';

                const hint = r.status === 'success'
                  ? `${r.latency_ms ?? '-'}ms${r.upstream_status_code ? ` · ${r.upstream_status_code}` : ''}`
                  : r.status === 'error'
                    ? (r.error || '测试失败')
                    : r.status === 'testing'
                      ? '正在测试...'
                      : '等待测试';

                return (
                  <li key={idx} className={`px-4 py-3 flex items-center gap-3 ${k.disabled ? 'opacity-60' : ''}`}>
                    <div className="w-10 h-10 flex items-center justify-center flex-shrink-0">
                      {statusIcon(r)}
                    </div>

                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-xs text-muted-foreground w-8 text-right flex-shrink-0">{idx + 1}</span>
                        <span className={`font-mono text-sm truncate ${k.disabled ? 'line-through text-muted-foreground' : 'text-foreground'}`} title={keyText}>
                          {keyText || '(空)'}
                        </span>
                        {copiedKeyIndex === idx ? (
                          <CopyCheck className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0" />
                        ) : (
                          <button
                            className="text-muted-foreground hover:text-foreground"
                            onClick={() => copyKey(idx)}
                            title="复制 Key"
                          >
                            <Copy className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </div>

                      <div className={`text-xs truncate ${r.status === 'error' ? 'text-red-600 dark:text-red-400' : r.status === 'success' ? 'text-emerald-600 dark:text-emerald-400' : 'text-muted-foreground'}`} title={typeof r.error === 'string' ? r.error : undefined}>
                        {hint}
                        {r.auth_failed ? <span className="ml-2 font-mono">(auth_failed)</span> : null}
                      </div>

                      {r.response_preview && (
                        <pre className="mt-2 text-[11px] p-2 bg-muted/40 border border-border rounded max-h-[120px] overflow-auto whitespace-pre-wrap">
                          {r.response_preview}
                        </pre>
                      )}
                    </div>

                    <div className="flex items-center gap-1 flex-shrink-0">
                      <button
                        onClick={() => void testSingleKey(idx)}
                        disabled={r.status === 'testing' || (!includeDisabled && k.disabled) || !keyText}
                        className="px-3 py-2 rounded-lg text-sm bg-primary/10 text-primary hover:bg-primary/20 disabled:opacity-50"
                        title="测试此 Key"
                      >
                        <Play className="w-4 h-4" />
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>

          {/* Footer */}
          <div className="p-4 border-t border-border bg-muted/30 flex-shrink-0 flex items-center justify-between">
            <div className="text-xs text-muted-foreground">
              共 {apiKeys.length} 个 Key · {successCount} 成功 · {errorCount} 失败
            </div>
            <div className="text-xs text-muted-foreground">
              engine=<span className="font-mono">{engine || 'openai'}</span> · base_url=<span className="font-mono">{base_url || '-'}</span>
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
