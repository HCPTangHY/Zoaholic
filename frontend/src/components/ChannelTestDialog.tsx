import { useState, useRef, useEffect } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { X, Play, Square, Search, Loader2, CheckCircle2, XCircle, Clock, Copy, CopyCheck } from 'lucide-react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';

interface ModelInfo {
  display: string;  // 别名
  upstream: string; // 上游模型名
}

interface TestResult {
  status: 'pending' | 'testing' | 'success' | 'error';
  latency: number | null;
  error: string | null;
}

interface ChannelTestDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  provider: any;
}

export function ChannelTestDialog({ open, onOpenChange, provider }: ChannelTestDialogProps) {
  const { token } = useAuthStore();
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [results, setResults] = useState<Map<string, TestResult>>(new Map());
  const [searchKeyword, setSearchKeyword] = useState('');
  const [concurrency, setConcurrency] = useState(3);
  const [isRunning, setIsRunning] = useState(false);
  const [copiedModel, setCopiedModel] = useState<string | null>(null);
  // 错误详情不再依赖 title 悬浮提示：移动端没有 hover，且 title 文本无法复制。
  // 这里记录展开项和复制反馈，让错误可以内联展开、选择并复制，目的在于保留列表简洁同时提供完整错误。
  const [expandedErrorModel, setExpandedErrorModel] = useState<string | null>(null);
  const [copiedErrorModel, setCopiedErrorModel] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  // 解析模型列表
  useEffect(() => {
    if (!provider) return;

    const rawModels = Array.isArray(provider.model) ? provider.model :
      Array.isArray(provider.models) ? provider.models : [];

    // 构建别名映射：上游 -> 别名
    const aliasMap = new Map<string, string>();
    const modelInfos: ModelInfo[] = [];

    rawModels.forEach((m: any) => {
      if (typeof m === 'string') {
        modelInfos.push({ display: m, upstream: m });
      } else if (typeof m === 'object' && m !== null) {
        Object.entries(m).forEach(([upstream, alias]) => {
          aliasMap.set(upstream, alias as string);
          modelInfos.push({ display: alias as string, upstream });
        });
      }
    });

    setModels(modelInfos);

    // 初始化结果
    const initialResults = new Map<string, TestResult>();
    modelInfos.forEach(m => {
      initialResults.set(m.display, { status: 'pending', latency: null, error: null });
    });
    setResults(initialResults);
    // 切换测试对象时清空旧错误面板，避免展示上一渠道的错误详情。
    setExpandedErrorModel(null);
    setCopiedErrorModel(null);
  }, [provider]);

  // 关闭时停止测试
  useEffect(() => {
    if (!open && isRunning) {
      stopTest();
    }
  }, [open]);

  const getFirstActiveApiKey = () => {
    if (provider.api) {
      const keys = Array.isArray(provider.api) ? provider.api : [provider.api];
      const firstActive = keys.find((k: unknown) => {
        if (typeof k !== 'string') return false;
        const trimmed = k.trim();
        return Boolean(trimmed) && !trimmed.startsWith('!');
      });
      if (typeof firstActive === 'string') return firstActive;

      const firstAny = keys.find((k: unknown) => typeof k === 'string' && k.trim());
      if (typeof firstAny === 'string') {
        return firstAny.startsWith('!') ? firstAny.substring(1) : firstAny;
      }
    }

    if (provider.api_keys && provider.api_keys.length > 0) {
      const firstActive = provider.api_keys.find((k: unknown) => {
        if (typeof k !== 'string') return false;
        const trimmed = k.trim();
        return Boolean(trimmed) && !trimmed.startsWith('!');
      });
      if (typeof firstActive === 'string') return firstActive;

      const firstAny = provider.api_keys.find((k: unknown) => typeof k === 'string' && k.trim());
      if (typeof firstAny === 'string') {
        return firstAny.startsWith('!') ? firstAny.substring(1) : firstAny;
      }
    }

    return '';
  };

  const buildProviderSnapshot = () => {
    try {
      return JSON.parse(JSON.stringify(provider));
    } catch {
      return provider;
    }
  };

  const testSingleModel = async (modelInfo: ModelInfo) => {
    const { display, upstream } = modelInfo;

    setResults(prev => {
      const newResults = new Map(prev);
      newResults.set(display, { status: 'testing', latency: null, error: null });
      return newResults;
    });

    try {
      const res = await apiFetch('/v1/channels/test', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({
          engine: provider.engine || 'openai',
          provider_snapshot: buildProviderSnapshot(),
          // 修改原因：虚拟路由手风琴复用本弹窗，但它不能按单个真实渠道直连测试。
          // 修改方式：当 provider 快照带 _virtual_route_test 时，在测试请求中同步传递 virtual_route_test 标记。
          // 目的：后端可以进入虚拟路由解析分支，用虚拟模型名触发完整 chain，而不是把它当普通渠道模型。
          virtual_route_test: provider._virtual_route_test === true,
          // 这里传别名模型，确保与正式路由保持一致（映射、覆写按别名匹配）
          model: display,
          upstream_model: upstream,

          // 保留旧字段，兼容后端 fallback 逻辑
          base_url: provider.base_url,
          api_key: getFirstActiveApiKey(),
          timeout: 30,
        }),
        signal: abortControllerRef.current?.signal,
      });

      const data = await res.json().catch(() => ({}));

      if (res.ok && data.success) {
        setResults(prev => {
          const newResults = new Map(prev);
          newResults.set(display, { status: 'success', latency: data.latency_ms || null, error: null });
          return newResults;
        });
      } else {
        const errorMsg = data.error || data.detail || data.message || `HTTP ${res.status}`;
        setResults(prev => {
          const newResults = new Map(prev);
          newResults.set(display, { status: 'error', latency: null, error: errorMsg });
          return newResults;
        });
      }
    } catch (e: any) {
      if (e.name === 'AbortError') {
        setResults(prev => {
          const newResults = new Map(prev);
          newResults.set(display, { status: 'pending', latency: null, error: null });
          return newResults;
        });
      } else {
        setResults(prev => {
          const newResults = new Map(prev);
          newResults.set(display, { status: 'error', latency: null, error: e.message });
          return newResults;
        });
      }
    }
  };

  const startAllTests = async () => {
    setIsRunning(true);
    abortControllerRef.current = new AbortController();

    // 重置所有状态
    setResults(prev => {
      const newResults = new Map(prev);
      models.forEach(m => {
        newResults.set(m.display, { status: 'pending', latency: null, error: null });
      });
      return newResults;
    });

    // 并发测试
    const queue = [...models];
    const runNext = async () => {
      while (queue.length > 0 && isRunning) {
        const modelInfo = queue.shift();
        if (!modelInfo) break;
        await testSingleModel(modelInfo);
      }
    };

    const tasks = [];
    for (let i = 0; i < concurrency; i++) {
      tasks.push(runNext());
    }
    await Promise.all(tasks);

    setIsRunning(false);
  };

  const stopTest = () => {
    setIsRunning(false);
    abortControllerRef.current?.abort();
  };

  const copyModelName = (name: string) => {
    navigator.clipboard.writeText(name);
    setCopiedModel(name);
    setTimeout(() => setCopiedModel(null), 2000);
  };

  const copyErrorText = async (modelName: string, errorText: string) => {
    try {
      await navigator.clipboard.writeText(errorText);
      setCopiedErrorModel(modelName);
      setTimeout(() => setCopiedErrorModel(null), 1500);
    } catch (error) {
      // 复制失败只记录到控制台，避免用弹窗或临时 Toast 打断批量测试流程。
      console.error('Failed to copy channel test error', error);
    }
  };

  const filteredModels = models.filter(m =>
    !searchKeyword || m.display.toLowerCase().includes(searchKeyword.toLowerCase())
  );

  const getStatusIcon = (status: TestResult['status']) => {
    switch (status) {
      case 'pending': return <Clock className="w-5 h-5 text-muted-foreground" />;
      case 'testing': return <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />;
      case 'success': return <CheckCircle2 className="w-5 h-5 text-emerald-500" />;
      case 'error': return <XCircle className="w-5 h-5 text-red-500" />;
    }
  };

  const getStatusText = (result: TestResult, modelName: string) => {
    switch (result.status) {
      case 'pending': return <span className="text-muted-foreground">等待测试</span>;
      case 'testing': return <span className="text-blue-600 dark:text-blue-400">正在测试...</span>;
      case 'success':
        return (
          <span className="text-emerald-600 dark:text-emerald-400">
            {result.latency !== null && <span className="font-mono">{result.latency}ms</span>}
            <span className="mx-1">·</span>
            测试通过
          </span>
        );
      case 'error': {
        const errorText = result.error || '测试失败';
        const truncated = errorText.length > 40 ? errorText.substring(0, 40) + '...' : errorText;
        const expanded = expandedErrorModel === modelName;
        return (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              setExpandedErrorModel(current => current === modelName ? null : modelName);
            }}
            className="max-w-full truncate text-left text-red-600 dark:text-red-400 hover:underline"
          >
            {expanded ? '收起错误详情' : `查看错误：${truncated}`}
          </button>
        );
      }
    }
  };

  if (!provider) return null;

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40 animate-in fade-in duration-200" />
        <Dialog.Content className="fixed right-0 top-0 h-full w-[600px] max-w-full bg-background border-l border-border shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300">
          {/* Header */}
          <div className="p-5 border-b border-border flex justify-between items-center bg-muted/30 flex-shrink-0">
            <Dialog.Title className="text-lg font-bold text-foreground">
              测试渠道: {provider.provider}
            </Dialog.Title>
            <Dialog.Close className="text-muted-foreground hover:text-foreground">
              <X className="w-5 h-5" />
            </Dialog.Close>
          </div>

          {/* Controls */}
          <div className="p-4 border-b border-border flex flex-wrap items-center gap-3">
            {!isRunning ? (
              <button
                onClick={startAllTests}
                className="bg-primary hover:bg-primary/90 text-primary-foreground px-4 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
              >
                <Play className="w-4 h-4" /> 全部测试
              </button>
            ) : (
              <button
                onClick={stopTest}
                className="bg-red-500/10 border border-red-500/40 text-red-600 dark:text-red-400 hover:bg-red-500/20 px-4 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
              >
                <Square className="w-4 h-4" /> 停止
              </button>
            )}

            <div className="flex items-center gap-2 text-sm">
              <span className="text-muted-foreground">并发:</span>
              <input
                type="number"
                min="1"
                max="10"
                value={concurrency}
                onChange={e => setConcurrency(Math.max(1, Math.min(10, parseInt(e.target.value) || 1)))}
                className="w-14 bg-background border border-border rounded px-2 py-1 text-center text-sm text-foreground focus:border-primary outline-none"
              />
            </div>

            <div className="flex-1 relative min-w-[120px]">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
              <input
                type="text"
                placeholder="搜索模型..."
                value={searchKeyword}
                onChange={e => setSearchKeyword(e.target.value)}
                className="w-full bg-muted border border-border rounded-full pl-9 pr-4 py-1.5 text-sm text-foreground focus:border-primary outline-none"
              />
            </div>
          </div>

          {/* Model List */}
          <div className="flex-1 overflow-y-auto">
            {filteredModels.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
                <Search className="w-10 h-10 mb-3 opacity-50" />
                <span>没有匹配的模型</span>
              </div>
            ) : (
              <ul className="divide-y divide-border">
                {filteredModels.map(modelInfo => {
                  const result = results.get(modelInfo.display) || { status: 'pending', latency: null, error: null };
                  const displayText = modelInfo.display !== modelInfo.upstream
                    ? `${modelInfo.display} (${modelInfo.upstream})`
                    : modelInfo.display;

                  const errorText = result.error || '测试失败';
                  const isErrorExpanded = result.status === 'error' && expandedErrorModel === modelInfo.display;

                  return (
                    <li
                      key={modelInfo.display}
                      className="px-4 hover:bg-muted/50 transition-colors group"
                    >
                      <div
                        className="flex items-center min-h-14 cursor-pointer"
                        onClick={() => copyModelName(modelInfo.display)}
                        title="点击复制模型名"
                      >
                        {/* Status Icon */}
                        <div className="w-10 h-10 flex items-center justify-center flex-shrink-0">
                          {getStatusIcon(result.status)}
                        </div>

                        {/* Content */}
                        <div className="flex-1 min-w-0 ml-2">
                          <div className="font-mono text-sm text-foreground truncate flex items-center gap-2">
                            {displayText}
                            {copiedModel === modelInfo.display ? (
                              <CopyCheck className="w-3.5 h-3.5 text-emerald-500" />
                            ) : (
                              <Copy className="w-3.5 h-3.5 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity" />
                            )}
                          </div>
                          <div className="text-xs truncate">
                            {getStatusText(result, modelInfo.display)}
                          </div>
                        </div>

                        {/* Test Button */}
                        <button
                          onClick={(e) => { e.stopPropagation(); testSingleModel(modelInfo); }}
                          disabled={result.status === 'testing'}
                          className="p-2 text-primary hover:bg-primary/10 rounded-lg transition-colors disabled:opacity-50"
                          title="测试此模型"
                        >
                          <Play className="w-4 h-4" />
                        </button>
                      </div>

                      {isErrorExpanded && (
                        <div
                          className="ml-12 mb-3 rounded-lg border border-red-500/20 bg-red-500/5 p-3"
                          onClick={(event) => event.stopPropagation()}
                        >
                          {/* 错误正文使用 pre 保留换行，并开启 select-text；这样手机端和桌面端都能完整查看并选择复制。 */}
                          <div className="flex items-center justify-between gap-3 text-[11px] text-red-600 dark:text-red-400">
                            <span>错误详情</span>
                            <button
                              type="button"
                              onClick={() => void copyErrorText(modelInfo.display, errorText)}
                              className="inline-flex items-center gap-1 rounded-md border border-red-500/20 bg-background px-2 py-1 hover:bg-red-500/10 transition-colors"
                            >
                              {copiedErrorModel === modelInfo.display ? <CopyCheck className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                              {copiedErrorModel === modelInfo.display ? '已复制' : '复制'}
                            </button>
                          </div>
                          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words select-text text-[11px] leading-relaxed text-red-700 dark:text-red-300 font-mono">{errorText}</pre>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {/* Footer */}
          <div className="p-4 border-t border-border bg-muted/30 flex-shrink-0">
            <div className="text-xs text-muted-foreground text-center">
              共 {models.length} 个模型 ·
              {Array.from(results.values()).filter(r => r.status === 'success').length} 成功 ·
              {Array.from(results.values()).filter(r => r.status === 'error').length} 失败
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
