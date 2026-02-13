import { useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '../lib/api';
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
  extensions?: any;
  dependencies?: any;
  loaded_at?: string | null;
  error?: string | null;
  metadata?: any;
}

type PluginStatus = {
  initialized?: boolean;
  loader?: {
    plugin_dirs?: string[];
    total_plugins?: number;
    enabled_plugins?: number;
  };
  registry?: any;
};

export default function Plugins() {
  const { token } = useAuthStore();

  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [status, setStatus] = useState<PluginStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const [search, setSearch] = useState('');

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);

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
        alert(`加载插件列表失败: ${err.detail || listRes.status}`);
      }

      if (statusRes.ok) {
        const data = await statusRes.json();
        setStatus(data || null);
      }
    } catch (e) {
      console.error(e);
      alert('加载插件信息失败（网络错误）');
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
      const hay = `${p.name} ${p.version || ''} ${p.description || ''} ${p.author || ''} ${p.source || ''}`.toLowerCase();
      return hay.includes(q);
    });
  }, [plugins, search]);

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
        alert(data?.detail || `操作失败: HTTP ${res.status}`);
        return;
      }

      if (successMessage) alert(successMessage);
      if (refreshAfter) await fetchAll();
    } catch (e) {
      console.error(e);
      alert('操作失败（网络错误）');
    }
  };

  const handleUploadClick = () => {
    fileInputRef.current?.click();
  };

  const handleUploadFile = async (file: File) => {
    if (!token) return;
    setUploading(true);
    try {
      const fd = new FormData();
      fd.append('file', file);

      const res = await apiFetch('/v1/plugins/upload', {
        method: 'POST',
        headers: authHeaders,
        body: fd,
      });

      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        alert(data?.detail || `上传失败: HTTP ${res.status}`);
        return;
      }

      alert(data?.message || '插件上传成功');
      await fetchAll();
    } catch (e) {
      console.error(e);
      alert('上传失败（网络错误）');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleLoadAll = async () => {
    await callAction('/v1/plugins/load-all', { method: 'POST' }, '已触发重新加载所有插件');
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
              if (f) handleUploadFile(f);
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
            placeholder="搜索插件：名称/版本/作者/描述..."
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
              你可以点击右上角“上传插件”上传 <code className="bg-muted px-1 py-0.5 rounded">.py</code> 或{' '}
              <code className="bg-muted px-1 py-0.5 rounded">.zip</code>。
            </p>
          </div>
        ) : (
          filtered.map((p) => {
            const hasError = !!p.error;
            return (
              <div key={p.name} className="bg-card border border-border rounded-xl overflow-hidden">
                <div className="p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-foreground">{p.name}</span>
                      {p.version && (
                        <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded font-mono">v{p.version}</span>
                      )}
                      <span
                        className={`text-xs px-2 py-0.5 rounded font-medium ${
                          p.enabled
                            ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                            : 'bg-zinc-500/10 text-zinc-600 dark:text-zinc-400'
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

                    {p.description && <div className="text-sm text-muted-foreground mt-1 break-words">{p.description}</div>}

                    <div className="text-xs text-muted-foreground mt-2 space-y-0.5">
                      {p.author && (
                        <div>
                          <span className="font-medium">作者：</span>
                          {p.author}
                        </div>
                      )}
                      {p.path && (
                        <div className="font-mono break-all">
                          <span className="font-sans font-medium">路径：</span>
                          {p.path}
                        </div>
                      )}
                      {p.loaded_at && (
                        <div>
                          <span className="font-medium">加载时间：</span>
                          {p.loaded_at}
                        </div>
                      )}
                      {hasError && (
                        <div className="text-destructive break-words">
                          <span className="font-medium">错误：</span>
                          {p.error}
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="flex flex-wrap gap-2">
                    {p.enabled ? (
                      <button
                        onClick={() =>
                          callAction(`/v1/plugins/${encodeURIComponent(p.name)}/disable`, { method: 'POST' }, `已禁用 ${p.name}`)
                        }
                        className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
                      >
                        <PowerOff className="w-4 h-4" /> 禁用
                      </button>
                    ) : (
                      <button
                        onClick={() =>
                          callAction(`/v1/plugins/${encodeURIComponent(p.name)}/enable`, { method: 'POST' }, `已启用 ${p.name}`)
                        }
                        className="bg-emerald-600 hover:bg-emerald-600/90 text-white px-3 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
                      >
                        <Power className="w-4 h-4" /> 启用
                      </button>
                    )}

                    <button
                      onClick={() =>
                        callAction(`/v1/plugins/${encodeURIComponent(p.name)}/reload`, { method: 'POST' }, `已重载 ${p.name}`)
                      }
                      className="bg-primary hover:bg-primary/90 text-primary-foreground px-3 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
                    >
                      <RotateCcw className="w-4 h-4" /> 重载
                    </button>

                    <button
                      onClick={async () => {
                        const ok = confirm(`确定要卸载并删除插件 "${p.name}" 吗？\n\n注意：source=entry_point 的插件无法在此删除。`);
                        if (!ok) return;
                        await callAction(`/v1/plugins/${encodeURIComponent(p.name)}`, { method: 'DELETE' }, `已卸载 ${p.name}`);
                      }}
                      className="bg-destructive hover:bg-destructive/90 text-destructive-foreground px-3 py-2 rounded-lg flex items-center gap-2 text-sm font-medium transition-colors"
                    >
                      <Trash2 className="w-4 h-4" /> 卸载
                    </button>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </section>

      {/* Tips */}
      <section className="bg-muted/30 border border-border rounded-xl p-4 text-sm text-muted-foreground">
        <div className="font-medium text-foreground mb-2">提示</div>
        <ul className="list-disc pl-5 space-y-1">
          <li>上传插件会写入后端的 <code className="bg-muted px-1 py-0.5 rounded">plugins/</code> 目录，并尝试立即加载。</li>
          <li>你也可以把插件文件直接放到服务器的 <code className="bg-muted px-1 py-0.5 rounded">plugins/</code> 目录，然后点“重新扫描并加载”。</li>
          <li>禁用=卸载（保留文件）；卸载=卸载并删除文件。</li>
        </ul>
      </section>
    </div>
  );
}
