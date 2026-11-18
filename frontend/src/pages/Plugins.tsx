import { useEffect, useMemo, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { apiFetch } from '../lib/api';
import { toastSuccess, toastError } from '../components/Toast';
import {
  Puzzle,
  Upload,
  RefreshCw,
  Trash2,
  Power,
  PowerOff,
  RotateCcw,
  Search,
  Folder,
  Info,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Settings2,
  Tag,
  Code2,
  FileText,
  Zap,
  Lock,
  X,
} from 'lucide-react';
import { useAuthStore } from '../store/authStore';

interface PluginInfo {
  name: string;
  version?: string;
  description?: string;
  author?: string;
  source?: string;
  path?: string;
  enabled: boolean;
  extensions?: string[];
  dependencies?: string[];
  loaded_at?: string | null;
  error?: string | null;
  metadata?: {
    category?: string;
    tags?: string[];
    params_hint?: string;
    provider_config?: {
      key: string;
      type?: 'json' | 'text';
      title?: string;
      description?: string;
      example?: unknown;
    };
    [key: string]: unknown;
  };
}

type PluginStatus = {
  initialized?: boolean;
  loader?: {
    plugin_dirs?: string[];
    total_plugins?: number;
    enabled_plugins?: number;
  };
  registry?: Record<string, unknown>;
};

export default function Plugins() {
  const { token } = useAuthStore();

  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [status, setStatus] = useState<PluginStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [pendingAction, setPendingAction] = useState<
    | { type: 'upload'; file: File }
    | { type: 'uninstall'; pluginName: string }
    | null
  >(null);
  const [adminPassword, setAdminPassword] = useState('');
  const [confirmSubmitting, setConfirmSubmitting] = useState(false);

  const authHeaders = useMemo<Record<string, string>>(
    () => (token ? { Authorization: `Bearer ${token}` } : ({} as Record<string, string>)),
    [token]
  );

  const fetchAll = async () => {
    if (!token) return;
    setLoading(true);
    try {
      const [listRes, statusRes] = await Promise.all([
        apiFetch('/v1/plugins', { headers: authHeaders }),
        apiFetch('/v1/plugins/status', { headers: authHeaders }),
      ]);

      if (listRes.ok) {
        const data = await listRes.json();
        setPlugins(Array.isArray(data?.plugins) ? data.plugins : []);
      } else {
        const err = await listRes.json().catch(() => ({}));
        toastError(err, "加载插件列表失败");
      }

      if (statusRes.ok) {
        const data = await statusRes.json();
        setStatus(data || null);
      }
    } catch (e) {
      console.error(e);
      toastError('加载插件信息失败（网络错误）');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return plugins;
    return plugins.filter(p => {
      const tags = p.metadata?.tags?.join(' ') || '';
      const hay = `${p.name} ${p.version || ''} ${p.description || ''} ${p.author || ''} ${p.source || ''} ${tags}`.toLowerCase();
      return hay.includes(q);
    });
  }, [plugins, search]);

  const toggleExpand = (name: string) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const callAction = async (
    url: string,
    options?: RequestInit,
    successMessage?: string,
    refreshAfter: boolean = true
  ) => {
    if (!token) return;
    try {
      const res = await apiFetch(url, {
        ...options,
        headers: {
          ...((options?.headers || {}) as Record<string, string>),
          ...authHeaders,
        },
      });

      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toastError(data?.detail || `操作失败: HTTP ${res.status}`);
        return;
      }

      if (successMessage) toastSuccess(successMessage);
      if (refreshAfter) await fetchAll();
    } catch (e) {
      console.error(e);
      toastError('操作失败（网络错误）');
    }
  };

  const handleUploadClick = () => {
    fileInputRef.current?.click();
  };

  const closePasswordDialog = () => {
    if (confirmSubmitting) return;
    setPendingAction(null);
    setAdminPassword('');
  };

  const requestUploadFile = (file: File) => {
    setAdminPassword('');
    setPendingAction({ type: 'upload', file });
  };

  const requestUninstallPlugin = (pluginName: string) => {
    setAdminPassword('');
    setPendingAction({ type: 'uninstall', pluginName });
  };

  const handleConfirmProtectedAction = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token || !pendingAction) return;
    if (!adminPassword) {
      toastError('请输入管理员密码');
      return;
    }

    setConfirmSubmitting(true);
    if (pendingAction.type === 'upload') setUploading(true);
    try {
      if (pendingAction.type === 'upload') {
        const fd = new FormData();
        fd.append('file', pendingAction.file);
        fd.append('admin_password', adminPassword);

        const res = await apiFetch('/v1/plugins/upload', {
          method: 'POST',
          headers: authHeaders,
          body: fd,
        });

        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          toastError(data?.detail || `上传失败: HTTP ${res.status}`);
          return;
        }

        setPendingAction(null);
        setAdminPassword('');
        toastSuccess(data?.message || '插件上传成功');
        await fetchAll();
      } else {
        const res = await apiFetch(`/v1/plugins/${encodeURIComponent(pendingAction.pluginName)}`, {
          method: 'DELETE',
          headers: {
            ...authHeaders,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ admin_password: adminPassword }),
        });

        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          toastError(data?.detail || `卸载失败: HTTP ${res.status}`);
          return;
        }

        const removedName = pendingAction.pluginName;
        setPendingAction(null);
        setAdminPassword('');
        toastSuccess(`已卸载 ${removedName}`);
        await fetchAll();
      }
    } catch (err) {
      console.error(err);
      toastError(pendingAction.type === 'upload' ? '上传失败（网络错误）' : '卸载失败（网络错误）');
    } finally {
      setConfirmSubmitting(false);
      setUploading(false);
    }
  };

  const handleLoadAll = async () => {
    await callAction('/v1/plugins/load-all', { method: 'POST' }, '已触发重新加载所有插件');
  };

  // 解析 extensions 获取拦截器类型列表
  const getInterceptorTypes = (ext: unknown): string[] => {
    if (!ext || !Array.isArray(ext)) return [];
    return ext.map((item) => {
      const e = String(item);
      if (e.includes('request')) return '请求拦截';
      if (e.includes('response')) return '响应拦截';
      return e;
    });
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
        <RefreshCw className="w-8 h-8 animate-spin mb-4" />
        <p>加载插件系统中...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-500 font-sans max-w-6xl mx-auto pb-12">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4 border-b border-border pb-6">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-foreground flex items-center gap-2">
            <Puzzle className="w-7 h-7 text-primary" /> 插件管理
          </h1>
          <p className="text-muted-foreground mt-1">
            上传/启用/禁用/重载/卸载后端插件（支持 <code className="bg-muted px-1 py-0.5 rounded">.py</code> /{' '}
            <code className="bg-muted px-1 py-0.5 rounded">.zip</code>）。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={fetchAll}
            className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
          >
            <RefreshCw className="w-4 h-4" /> 刷新
          </button>

          <button
            onClick={handleLoadAll}
            className="bg-primary hover:bg-primary/90 text-primary-foreground px-3 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
          >
            <RotateCcw className="w-4 h-4" /> 重新扫描并加载
          </button>

          <button
            onClick={handleUploadClick}
            disabled={uploading}
            className="bg-emerald-600 hover:bg-emerald-600/90 text-white px-3 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors disabled:opacity-50"
          >
            <Upload className="w-4 h-4" /> {uploading ? '上传中...' : '上传插件'}
          </button>

          <input
            ref={fileInputRef}
            type="file"
            accept=".py,.zip"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = '';
              if (f) requestUploadFile(f);
            }}
          />
        </div>
      </div>

      {/* Status */}
      <section className="bg-card border border-border rounded-xl overflow-hidden">
        <div className="p-4 border-b border-border bg-muted/30 flex items-center gap-2 font-medium text-foreground">
          <Info className="w-5 h-5 text-blue-500" /> 插件系统状态
        </div>
        <div className="p-6 grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
          <div className="bg-muted/30 border border-border rounded-lg p-4">
            <div className="text-muted-foreground">初始化状态</div>
            <div className="mt-1 font-semibold">
              {status?.initialized ? '已初始化' : '未初始化'}
            </div>
          </div>
          <div className="bg-muted/30 border border-border rounded-lg p-4">
            <div className="text-muted-foreground">插件数量</div>
            <div className="mt-1 font-semibold">
              {status?.loader?.enabled_plugins ?? 0} / {status?.loader?.total_plugins ?? plugins.length} 已启用
            </div>
          </div>
          <div className="bg-muted/30 border border-border rounded-lg p-4">
            <div className="text-muted-foreground flex items-center gap-1">
              <Folder className="w-4 h-4" /> 插件目录
            </div>
            <div className="mt-1 font-mono text-xs break-all">
              {(status?.loader?.plugin_dirs || ['plugins']).join(', ')}
            </div>
          </div>
        </div>
      </section>

      {/* Search */}
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="w-4 h-4 text-muted-foreground absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="搜索插件：名称/版本/作者/描述/标签..."
            className="w-full bg-background border border-border rounded-lg pl-9 pr-3 py-2 text-sm text-foreground"
          />
        </div>
        <div className="text-xs text-muted-foreground whitespace-nowrap">
          共 {filtered.length} 个
        </div>
      </div>

      {/* Plugin List */}
      <section className="space-y-3">
        {filtered.length === 0 ? (
          <div className="bg-card border border-border rounded-xl p-8 text-center text-muted-foreground">
            <p>暂无插件</p>
            <p className="text-xs mt-2">
              你可以点击右上角"上传插件"上传 <code className="bg-muted px-1 py-0.5 rounded">.py</code> 或{' '}
              <code className="bg-muted px-1 py-0.5 rounded">.zip</code>。
            </p>
          </div>
        ) : (
          filtered.map((p) => {
            const hasError = !!p.error;
            const isExpanded = expanded.has(p.name);
            const tags = p.metadata?.tags || [];
            const category = p.metadata?.category;
            const paramsHint = p.metadata?.params_hint;
            const providerConfig = p.metadata?.provider_config;
            const interceptorTypes = getInterceptorTypes(p.extensions);
            const hasDetails = !!(p.description || paramsHint || providerConfig || p.path || p.author || p.loaded_at || hasError || interceptorTypes.length);

            return (
              <div
                key={p.name}
                className={`bg-card border rounded-xl overflow-hidden transition-colors ${
                  p.enabled
                    ? 'border-emerald-500/30'
                    : hasError
                    ? 'border-destructive/30'
                    : 'border-border'
                }`}
              >
                {/* Card Header — always visible */}
                <div
                  className="p-4 flex items-start gap-3 cursor-pointer select-none hover:bg-muted/20 transition-colors"
                  onClick={() => hasDetails && toggleExpand(p.name)}
                >
                  {/* Expand icon */}
                  <div className="text-muted-foreground flex-shrink-0 mt-0.5">
                    {hasDetails ? (
                      isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />
                    ) : (
                      <div className="w-4" />
                    )}
                  </div>

                  {/* Info */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-foreground">{p.name}</span>
                      {p.version && (
                        <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded font-mono">v{p.version}</span>
                      )}
                      <span
                        className={`text-xs px-2 py-0.5 rounded font-medium ${
                          p.enabled
                            ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                            : 'bg-muted text-muted-foreground'
                        }`}
                      >
                        {p.enabled ? '已启用' : '已禁用'}
                      </span>
                      {p.source && (
                        <span className="text-xs bg-blue-500/10 text-blue-600 dark:text-blue-400 px-2 py-0.5 rounded">
                          {p.source}
                        </span>
                      )}
                      {hasError && (
                        <span className="text-xs bg-destructive/10 text-destructive px-2 py-0.5 rounded flex items-center gap-1">
                          <AlertTriangle className="w-3.5 h-3.5" /> 加载失败
                        </span>
                      )}
                    </div>

                    {/* Description — visible in collapsed state */}
                    {p.description && (
                      <p className={`text-sm text-muted-foreground mt-1.5 ${isExpanded ? '' : 'line-clamp-2'}`}>
                        {p.description}
                      </p>
                    )}

                    {/* Tags + interceptor types — visible in collapsed state */}
                    {(tags.length > 0 || category || interceptorTypes.length > 0) && (
                      <div className="flex items-center gap-1.5 mt-2 flex-wrap">
                        {tags.map(tag => (
                          <span
                            key={tag}
                            className="text-[10px] px-2 py-0.5 rounded-full font-mono bg-blue-500/10 text-blue-600 dark:text-blue-400"
                          >
                            {tag}
                          </span>
                        ))}
                        {category && (
                          <span className="text-[10px] px-2 py-0.5 rounded-full font-mono bg-amber-500/10 text-amber-600 dark:text-amber-400">
                            {category}
                          </span>
                        )}
                        {interceptorTypes.map((t, i) => (
                          <span
                            key={i}
                            className="text-[10px] px-2 py-0.5 rounded-full font-mono bg-purple-500/10 text-purple-600 dark:text-purple-400 flex items-center gap-0.5"
                          >
                            <Zap className="w-2.5 h-2.5" /> {t}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="flex flex-wrap gap-1.5 flex-shrink-0" onClick={e => e.stopPropagation()}>
                    {p.enabled ? (
                      <button
                        onClick={() =>
                          callAction(`/v1/plugins/${encodeURIComponent(p.name)}/disable`, { method: 'POST' }, `已禁用 ${p.name}`)
                        }
                        className="bg-muted hover:bg-muted/80 text-foreground px-2.5 py-1.5 rounded-lg flex items-center gap-1.5 text-xs font-medium transition-colors"
                      >
                        <PowerOff className="w-3.5 h-3.5" /> 禁用
                      </button>
                    ) : (
                      <button
                        onClick={() =>
                          callAction(`/v1/plugins/${encodeURIComponent(p.name)}/enable`, { method: 'POST' }, `已启用 ${p.name}`)
                        }
                        className="bg-emerald-600 hover:bg-emerald-600/90 text-white px-2.5 py-1.5 rounded-lg flex items-center gap-1.5 text-xs font-medium transition-colors"
                      >
                        <Power className="w-3.5 h-3.5" /> 启用
                      </button>
                    )}

                    <button
                      onClick={() =>
                        callAction(`/v1/plugins/${encodeURIComponent(p.name)}/reload`, { method: 'POST' }, `已重载 ${p.name}`)
                      }
                      className="bg-primary hover:bg-primary/90 text-primary-foreground px-2.5 py-1.5 rounded-lg flex items-center gap-1.5 text-xs font-medium transition-colors"
                    >
                      <RotateCcw className="w-3.5 h-3.5" /> 重载
                    </button>

                    <button
                      onClick={() => requestUninstallPlugin(p.name)}
                      className="bg-destructive hover:bg-destructive/90 text-destructive-foreground px-2.5 py-1.5 rounded-lg flex items-center gap-1.5 text-xs font-medium transition-colors"
                    >
                      <Trash2 className="w-3.5 h-3.5" /> 卸载
                    </button>
                  </div>
                </div>

                {/* Expanded Details */}
                {isExpanded && (
                  <div className="border-t border-border bg-muted/10 px-4 pb-4 pt-3 ml-7 space-y-4">
                    {/* params_hint */}
                    {paramsHint && (
                      <div>
                        <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2 flex items-center gap-1.5">
                          <Settings2 className="w-3.5 h-3.5" /> 插件参数
                        </div>
                        <div className="bg-background border border-border rounded-lg p-3 font-mono text-xs text-foreground leading-relaxed">
                          {paramsHint}
                        </div>
                      </div>
                    )}

                    {/* provider_config */}
                    {providerConfig && (
                      <div>
                        <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2 flex items-center gap-1.5">
                          <Code2 className="w-3.5 h-3.5" /> 渠道级配置
                          {providerConfig.title && (
                            <span className="font-normal normal-case tracking-normal">— {providerConfig.title}</span>
                          )}
                        </div>
                        {providerConfig.description && (
                          <p className="text-xs text-muted-foreground mb-2">{providerConfig.description}</p>
                        )}
                        <div className="space-y-2">
                          <div className="flex items-center gap-2 text-xs text-muted-foreground">
                            <Tag className="w-3 h-3" />
                            <span>配置 key: <code className="bg-muted px-1.5 py-0.5 rounded text-foreground font-mono">{providerConfig.key}</code></span>
                            <span className="text-muted-foreground/60">·</span>
                            <span>类型: <code className="bg-muted px-1.5 py-0.5 rounded text-foreground font-mono">{providerConfig.type || 'json'}</code></span>
                          </div>
                          {providerConfig.example != null && (
                            <div>
                              <div className="text-[10px] text-muted-foreground mb-1 flex items-center gap-1">
                                <FileText className="w-3 h-3" /> 配置示例
                              </div>
                              <pre className="bg-background border border-border rounded-lg p-3 text-xs font-mono text-foreground overflow-x-auto max-h-48 overflow-y-auto leading-relaxed">
                                {typeof providerConfig.example === 'string'
                                  ? providerConfig.example
                                  : JSON.stringify(providerConfig.example, null, 2)}
                              </pre>
                            </div>
                          )}
                        </div>
                      </div>
                    )}

                    {/* Extensions */}
                    {p.extensions && Array.isArray(p.extensions) && p.extensions.length > 0 && (
                      <div>
                        <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2 flex items-center gap-1.5">
                          <Zap className="w-3.5 h-3.5" /> 注册的扩展点
                        </div>
                        <div className="flex flex-wrap gap-1.5">
                          {p.extensions.map((ext: string, i: number) => (
                            <code
                              key={i}
                              className="text-[11px] bg-background border border-border px-2 py-1 rounded font-mono text-foreground"
                            >
                              {ext}
                            </code>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Meta info */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs text-muted-foreground">
                      {p.author && (
                        <div className="bg-background border border-border rounded-lg p-2.5">
                          <span className="font-medium text-foreground">作者</span>
                          <div className="mt-0.5">{p.author}</div>
                        </div>
                      )}
                      {p.path && (
                        <div className="bg-background border border-border rounded-lg p-2.5">
                          <span className="font-medium text-foreground">文件路径</span>
                          <div className="mt-0.5 font-mono break-all">{p.path}</div>
                        </div>
                      )}
                      {p.loaded_at && (
                        <div className="bg-background border border-border rounded-lg p-2.5">
                          <span className="font-medium text-foreground">加载时间</span>
                          <div className="mt-0.5">{p.loaded_at}</div>
                        </div>
                      )}
                      {p.dependencies && Array.isArray(p.dependencies) && p.dependencies.length > 0 && (
                        <div className="bg-background border border-border rounded-lg p-2.5">
                          <span className="font-medium text-foreground">依赖</span>
                          <div className="mt-0.5 font-mono">{p.dependencies.join(', ')}</div>
                        </div>
                      )}
                    </div>

                    {/* Error */}
                    {hasError && (
                      <div className="bg-destructive/5 border border-destructive/20 rounded-lg p-3">
                        <div className="text-xs font-semibold text-destructive flex items-center gap-1.5 mb-1">
                          <AlertTriangle className="w-3.5 h-3.5" /> 加载错误
                        </div>
                        <pre className="text-xs text-destructive font-mono whitespace-pre-wrap break-words">
                          {p.error}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </section>

      {/* Tips */}
      <section className="bg-muted/30 border border-border rounded-xl p-4 text-sm text-muted-foreground">
        <div className="font-medium text-foreground mb-2">提示</div>
        <ul className="list-disc pl-5 space-y-1">
          <li>上传插件与卸载插件需要再次验证管理员登录密码，验证通过后才会写入或删除 <code className="bg-muted px-1 py-0.5 rounded">plugins/</code> 目录文件。</li>
          <li>你也可以把插件文件直接放到服务器的 <code className="bg-muted px-1 py-0.5 rounded">plugins/</code> 目录，然后点"重新扫描并加载"。</li>
          <li>禁用=卸载（保留文件）；卸载=卸载并删除文件。</li>
          <li>点击插件卡片可展开查看详细参数说明、配置示例和扩展点信息。</li>
        </ul>
      </section>

      {/* Administrator Password Confirmation Dialog */}
      <Dialog.Root
        open={Boolean(pendingAction)}
        onOpenChange={(open) => {
          if (!open) closePasswordDialog();
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[min(92vw,420px)] -translate-x-1/2 -translate-y-1/2 rounded-xl border border-border bg-background p-5 shadow-2xl">
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-2 text-foreground font-semibold">
                <Lock className="w-4 h-4 text-primary" />
                <Dialog.Title className="text-base">验证管理员密码</Dialog.Title>
              </div>
              <Dialog.Close
                disabled={confirmSubmitting}
                className="rounded-md p-1 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-50"
              >
                <X className="w-4 h-4" />
              </Dialog.Close>
            </div>

            <Dialog.Description className="text-xs text-muted-foreground mt-2 leading-relaxed">
              {pendingAction?.type === 'upload'
                ? `准备上传并加载插件 "${pendingAction.file.name}"。请输入管理员登录密码确认该操作。`
                : pendingAction?.type === 'uninstall'
                ? `确定要卸载并删除插件 "${pendingAction.pluginName}" 吗？请输入管理员登录密码确认删除（entry_point 插件无法在此删除）。`
                : ''}
            </Dialog.Description>

            <form onSubmit={handleConfirmProtectedAction} className="mt-4 space-y-4">
              <div>
                <label htmlFor="plugin-admin-password" className="block text-xs font-medium text-foreground mb-1.5">
                  管理员密码
                </label>
                <input
                  id="plugin-admin-password"
                  type="password"
                  autoComplete="current-password"
                  autoFocus
                  value={adminPassword}
                  onChange={(e) => setAdminPassword(e.target.value)}
                  placeholder="请输入管理员登录密码"
                  disabled={confirmSubmitting}
                  className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/40"
                />
              </div>

              <div className="flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={closePasswordDialog}
                  disabled={confirmSubmitting}
                  className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                >
                  取消
                </button>
                <button
                  type="submit"
                  disabled={confirmSubmitting || !adminPassword}
                  className={`px-3.5 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 ${
                    pendingAction?.type === 'uninstall'
                      ? 'bg-destructive hover:bg-destructive/90 text-destructive-foreground'
                      : 'bg-emerald-600 hover:bg-emerald-600/90 text-white'
                  }`}
                >
                  {confirmSubmitting
                    ? pendingAction?.type === 'uninstall'
                      ? '卸载中...'
                      : '上传中...'
                    : pendingAction?.type === 'uninstall'
                    ? '确认卸载'
                    : '确认上传'}
                </button>
              </div>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
