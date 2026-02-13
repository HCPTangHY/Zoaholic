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
  }, [provider]);

  // 关闭时停止测试
  useEffect(() => {
    if (!open && isRunning) {
      stopTest();
    }
  }, [open]);

  const getApiKey = () => {
    if (provider.api) {
      const key = Array.isArray(provider.api) ? provider.api[0] : provider.api;
      // 处理 ! 前缀
      return typeof key === 'string' && key.startsWith('!') ? key.substring(1) : key;
    }
    if (provider.api_keys && provider.api_keys.length > 0) {
      const key = provider.api_keys[0];
      return typeof key === 'string' && key.startsWith('!') ? key.substring(1) : key;
    }
    return '';
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
          base_url: provider.base_url,
          api_key: getApiKey(),
          model: upstream,
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

  const filteredModels = models.filter(m =>
    !searchKeyword || m.display.toLowerCase().includes(searchKeyword.toLowerCase())
  );

  const getStatusIcon = (status: TestResult['status']) => {
    switch (status) {
      case 'pending': return <Clock className="w-5 h-5 text-zinc-500" />;
      case 'testing': return <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />;
      case 'success': return <CheckCircle2 className="w-5 h-5 text-emerald-500" />;
      case 'error': return <XCircle className="w-5 h-5 text-red-500" />;
    }
  };

  const getStatusText = (result: TestResult) => {
    switch (result.status) {
      case 'pending': return <span className="text-zinc-500">等待测试</span>;
      case 'testing': return <span className="text-blue-400">正在测试...</span>;
      case 'success':
        return (
          <span className="text-emerald-400">
            {result.latency !== null && <span className="font-mono">{result.latency}ms</span>}
            <span className="mx-1">·</span>
            测试通过
          </span>
        );
      case 'error':
        const errorText = result.error || '测试失败';
        const truncated = errorText.length > 40 ? errorText.substring(0, 40) + '...' : errorText;
        return <span className="text-red-400" title={errorText}>{truncated}</span>;
    }
  };

  if (!provider) return null;

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40 animate-in fade-in duration-200" />
        <Dialog.Content className="fixed right-0 top-0 h-full w-[600px] max-w-full bg-zinc-950 border-l border-zinc-800 shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300">
          {/* Header */}
          <div className="p-5 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/50 flex-shrink-0">
            <Dialog.Title className="text-lg font-bold text-zinc-100">
              测试渠道: {provider.provider}
            </Dialog.Title>
            <Dialog.Close className="text-zinc-400 hover:text-zinc-100">
              <X className="w-5 h-5" />
            </Dialog.Close>
          </div>

          {/* Controls */}
          <div className="p-4 border-b border-zinc-800 flex flex-wrap items-center gap-3">
            {!isRunning ? (
              <button
                onClick={startAllTests}
                className="bg-primary hover:bg-blue-600 text-white px-4 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
              >
                <Play className="w-4 h-4" /> 全部测试
              </button>
            ) : (
              <button
                onClick={stopTest}
                className="bg-red-500/20 border border-red-500/50 text-red-400 hover:bg-red-500/30 px-4 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
              >
                <Square className="w-4 h-4" /> 停止
              </button>
            )}

            <div className="flex items-center gap-2 text-sm">
              <span className="text-zinc-400">并发:</span>
              <input
                type="number"
                min="1"
                max="10"
                value={concurrency}
                onChange={e => setConcurrency(Math.max(1, Math.min(10, parseInt(e.target.value) || 1)))}
                className="w-14 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-center text-sm"
              />
            </div>

            <div className="flex-1 relative min-w-[120px]">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
              <input
                type="text"
                placeholder="搜索模型..."
                value={searchKeyword}
                onChange={e => setSearchKeyword(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-700 rounded-full pl-9 pr-4 py-1.5 text-sm focus:border-primary outline-none"
              />
            </div>
          </div>

          {/* Model List */}
          <div className="flex-1 overflow-y-auto">
            {filteredModels.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-zinc-500">
                <Search className="w-10 h-10 mb-3 opacity-50" />
                <span>没有匹配的模型</span>
              </div>
            ) : (
              <ul className="divide-y divide-zinc-800">
                {filteredModels.map(modelInfo => {
                  const result = results.get(modelInfo.display) || { status: 'pending', latency: null, error: null };
                  const displayText = modelInfo.display !== modelInfo.upstream
                    ? `${modelInfo.display} (${modelInfo.upstream})`
                    : modelInfo.display;

                  return (
                    <li
                      key={modelInfo.display}
                      className="flex items-center h-14 px-4 hover:bg-zinc-800/50 transition-colors cursor-pointer group"
                      onClick={() => copyModelName(modelInfo.display)}
                      title="点击复制模型名"
                    >
                      {/* Status Icon */}
                      <div className="w-10 h-10 flex items-center justify-center flex-shrink-0">
                        {getStatusIcon(result.status)}
                      </div>

                      {/* Content */}
                      <div className="flex-1 min-w-0 ml-2">
                        <div className="font-mono text-sm text-zinc-100 truncate flex items-center gap-2">
                          {displayText}
                          {copiedModel === modelInfo.display ? (
                            <CopyCheck className="w-3.5 h-3.5 text-emerald-500" />
                          ) : (
                            <Copy className="w-3.5 h-3.5 text-zinc-600 opacity-0 group-hover:opacity-100 transition-opacity" />
                          )}
                        </div>
                        <div className="text-xs truncate">
                          {getStatusText(result)}
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
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {/* Footer */}
          <div className="p-4 border-t border-zinc-800 bg-zinc-900/50 flex-shrink-0">
            <div className="text-xs text-zinc-500 text-center">
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
