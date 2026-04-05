import { useState, useEffect } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import {
  Save, RefreshCw, AlertCircle, Zap, Shield,
  Timer, Database, Blocks, Plus, Trash2, Link
} from 'lucide-react';

type CleanupAction = 'clear_fields' | 'delete_rows';
type CleanupTimeMode = 'older_than_hours' | 'custom_range' | 'all';

interface ExternalClient {
  icon: string;
  name: string;
  link: string;
}

interface Preferences {
  max_retry_count?: number;
  cooldown_period?: number;
  SCHEDULING_ALGORITHM?: string;
  rate_limit?: string;
  model_timeout?: { default?: number; [key: string]: number | undefined };
  keepalive_interval?: { default?: number; [key: string]: number | undefined };
  log_raw_data_retention_hours?: number;
  log_retention_mode?: string;
  log_retention_days?: number;
  log_retention_run_at?: string;
  external_clients?: ExternalClient[];
}
type CleanupSuccessMode = 'ALL' | 'SUCCESS' | 'FAILED';

interface LogsCleanupResponse {
  dry_run: boolean;
  action: CleanupAction;
  matched_rows: number;
  affected_rows: number;
  selected_fields: string[];
  non_null_counts: Record<string, number>;
  filters: Record<string, unknown>;
  message: string;
}

const LOG_CLEANUP_FIELD_OPTIONS: { key: string; label: string }[] = [
  { key: 'request_headers', label: '用户请求头' },
  { key: 'request_body', label: '用户请求体' },
  { key: 'upstream_request_headers', label: '上游请求头' },
  { key: 'upstream_request_body', label: '上游请求体' },
  { key: 'upstream_response_body', label: '上游响应体' },
  { key: 'response_body', label: '返回给用户的响应体' },
  { key: 'retry_path', label: '重试路径' },
  { key: 'text', label: '文本摘要' },
];

const DEFAULT_CLEANUP_FIELDS = LOG_CLEANUP_FIELD_OPTIONS
  .filter(item => item.key !== 'text')
  .map(item => item.key);

export default function Settings() {
  const { token } = useAuthStore();
  const [preferences, setPreferences] = useState<Preferences>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // 数据库清理状态
  const [cleanupAction, setCleanupAction] = useState<CleanupAction>('clear_fields');
  const [cleanupTimeMode, setCleanupTimeMode] = useState<CleanupTimeMode>('older_than_hours');
  const [cleanupOlderThanHours, setCleanupOlderThanHours] = useState(168);
  const [cleanupStartTime, setCleanupStartTime] = useState('');
  const [cleanupEndTime, setCleanupEndTime] = useState('');
  const [cleanupProvider, setCleanupProvider] = useState('');
  const [cleanupModel, setCleanupModel] = useState('');
  const [cleanupApiKey, setCleanupApiKey] = useState('');
  const [cleanupStatusCodes, setCleanupStatusCodes] = useState('');
  const [cleanupSuccessMode, setCleanupSuccessMode] = useState<CleanupSuccessMode>('ALL');
  const [cleanupFlaggedOnly, setCleanupFlaggedOnly] = useState(false);
  const [cleanupFields, setCleanupFields] = useState<string[]>(DEFAULT_CLEANUP_FIELDS);
  const [cleanupRunning, setCleanupRunning] = useState(false);
  const [cleanupResult, setCleanupResult] = useState<LogsCleanupResponse | null>(null);
  const [cleanupConfirmText, setCleanupConfirmText] = useState('');
  const [cleanupMessage, setCleanupMessage] = useState('');

  // Load configuration
  useEffect(() => {
    const fetchConfig = async () => {
      if (!token) return;
      setLoading(true);
      try {
        const res = await apiFetch('/v1/api_config', {
          headers: { Authorization: `Bearer ${token}` }
        });
        if (res.ok) {
          const data = await res.json();
          const loadedPreferences: Preferences = (data.api_config?.preferences || data.preferences || {}) as Preferences;

          // Ensure default external clients exist if not defined
          if (!loadedPreferences.external_clients) {
            loadedPreferences.external_clients = [
              { name: 'IdoFront', icon: '🌚', link: 'https://idofront.pages.dev/?baseurl={address}/v1&key={key}' }
            ];
          }
          setPreferences(loadedPreferences);
        }
      } catch (err) {
        console.error('Failed to load settings:', err);
      } finally {
        setLoading(false);
      }
    };
    fetchConfig();
  }, [token]);

  const updatePreference = <K extends keyof Preferences>(key: K, value: Preferences[K]) => {
    setPreferences(prev => ({ ...prev, [key]: value }));
  };

  const parseErrorMessage = async (res: Response) => {
    try {
      const data = await res.json();
      return data?.detail || data?.message || `HTTP ${res.status}`;
    } catch {
      return `HTTP ${res.status}`;
    }
  };

  const toIsoStringOrUndefined = (localDateTime: string) => {
    if (!localDateTime) return undefined;
    const dt = new Date(localDateTime);
    if (Number.isNaN(dt.getTime())) return undefined;
    return dt.toISOString();
  };

  const toggleCleanupField = (field: string) => {
    setCleanupFields(prev => (
      prev.includes(field) ? prev.filter(item => item !== field) : [...prev, field]
    ));
  };

  const buildCleanupPayload = (dryRun: boolean) => {
    const payload: Record<string, unknown> = {
      dry_run: dryRun,
      action: cleanupAction,
      flagged_only: cleanupFlaggedOnly,
    };

    if (cleanupAction === 'clear_fields') {
      payload.fields = cleanupFields;
    }

    if (cleanupTimeMode === 'older_than_hours') {
      payload.older_than_hours = cleanupOlderThanHours;
    } else if (cleanupTimeMode === 'custom_range') {
      const startIso = toIsoStringOrUndefined(cleanupStartTime);
      const endIso = toIsoStringOrUndefined(cleanupEndTime);
      if (startIso) payload.start_time = startIso;
      if (endIso) payload.end_time = endIso;
    }

    if (cleanupProvider.trim()) payload.provider = cleanupProvider.trim();
    if (cleanupModel.trim()) payload.model = cleanupModel.trim();
    if (cleanupApiKey.trim()) payload.api_key = cleanupApiKey.trim();
    if (cleanupStatusCodes.trim()) {
      const parsedCodes = cleanupStatusCodes
        .split(',')
        .map(item => parseInt(item.trim(), 10))
        .filter(code => !Number.isNaN(code));
      if (parsedCodes.length > 0) {
        payload.status_codes = parsedCodes;
      }
    }

    if (cleanupSuccessMode === 'SUCCESS') payload.success = true;
    if (cleanupSuccessMode === 'FAILED') payload.success = false;

    return payload;
  };

  const handleCleanupPreview = async () => {
    if (!token) return;

    if (cleanupAction === 'clear_fields' && cleanupFields.length === 0) {
      alert('请至少选择一个要清空的字段');
      return;
    }

    if (cleanupTimeMode === 'older_than_hours' && cleanupOlderThanHours < 1) {
      alert('按小时清理时，小时数必须大于等于 1');
      return;
    }

    setCleanupRunning(true);
    setCleanupMessage('');
    try {
      const res = await apiFetch('/v1/logs/cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(buildCleanupPayload(true)),
      });

      if (!res.ok) {
        const msg = await parseErrorMessage(res);
        setCleanupMessage(`预览失败：${msg}`);
        return;
      }

      const data = await res.json();
      setCleanupResult(data);
      setCleanupMessage('预览成功：仅统计未执行写入。');
    } catch {
      setCleanupMessage('预览失败：网络错误');
    } finally {
      setCleanupRunning(false);
    }
  };

  const requiredConfirmPhrase = cleanupAction === 'delete_rows' ? 'DELETE' : 'CLEAR';

  const handleCleanupExecute = async () => {
    if (!token) return;

    if (cleanupAction === 'clear_fields' && cleanupFields.length === 0) {
      alert('请至少选择一个要清空的字段');
      return;
    }

    if (cleanupConfirmText.trim().toUpperCase() !== requiredConfirmPhrase) {
      alert(`请输入确认词 ${requiredConfirmPhrase} 后再执行`);
      return;
    }

    if (!window.confirm('该操作会修改数据库，是否确认执行？')) {
      return;
    }

    setCleanupRunning(true);
    setCleanupMessage('');
    try {
      const res = await apiFetch('/v1/logs/cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(buildCleanupPayload(false)),
      });

      if (!res.ok) {
        const msg = await parseErrorMessage(res);
        setCleanupMessage(`执行失败：${msg}`);
        return;
      }

      const data: LogsCleanupResponse = await res.json();
      setCleanupResult(data);
      setCleanupConfirmText('');
      setCleanupMessage(`执行完成：影响 ${data.affected_rows} 条记录`);
    } catch {
      setCleanupMessage('执行失败：网络错误');
    } finally {
      setCleanupRunning(false);
    }
  };

  const handleSave = async () => {
    if (!token) return;
    setSaving(true);
    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ preferences })
      });
      if (res.ok) {
        alert('配置已保存成功');
      } else {
        const msg = await parseErrorMessage(res);
        alert(`保存失败：${msg}`);
      }
    } catch {
      alert('网络错误');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
        <RefreshCw className="w-8 h-8 animate-spin mb-4" />
        <p>加载配置中...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-500 font-sans max-w-4xl mx-auto pb-12">
      {/* Header */}
      <div className="flex justify-between items-center border-b border-border pb-6">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-foreground">系统设置</h1>
          <p className="text-muted-foreground mt-1">管理全局配置和系统首选项</p>
        </div>
        <button
          onClick={handleSave}
          disabled={saving}
          className="bg-primary hover:bg-primary/90 text-primary-foreground px-4 py-2 rounded-lg flex items-center gap-2 font-medium transition-colors disabled:opacity-50"
        >
          {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          保存配置
        </button>
      </div>

      <div className="space-y-8">
        {/* 高可用性设置 */}
        <section className="bg-card border border-border rounded-xl overflow-hidden">
          <div className="p-4 border-b border-border bg-muted/30 flex items-center gap-2 font-medium text-foreground">
            <Zap className="w-5 h-5 text-amber-500" /> 高可用性与调度
          </div>
          <div className="p-6 space-y-6">
            <div className="grid grid-cols-2 gap-6">
              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">最大重试次数</label>
                <input
                  type="number" min="1" max="100"
                  value={preferences.max_retry_count ?? 10}
                  onChange={e => updatePreference('max_retry_count', parseInt(e.target.value))}
                  className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                />
                <p className="text-xs text-muted-foreground mt-1">多渠道场景下的最大重试次数上限（1-100）</p>
              </div>
              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">渠道冷却时间 (秒)</label>
                <input
                  type="number" min="0"
                  value={preferences.cooldown_period ?? 3}
                  onChange={e => updatePreference('cooldown_period', parseInt(e.target.value))}
                  className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                />
                <p className="text-xs text-muted-foreground mt-1">失败渠道的冷却时间，设为 0 禁用</p>
              </div>
            </div>

            <div>
              <label className="text-sm font-medium text-foreground mb-1.5 block">全局调度算法</label>
              <select
                value={preferences.SCHEDULING_ALGORITHM || 'fixed_priority'}
                onChange={e => updatePreference('SCHEDULING_ALGORITHM', e.target.value)}
                className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
              >
                <option value="fixed_priority">固定优先级 (fixed_priority) - 始终使用第一个可用渠道</option>
                <option value="round_robin">轮询 (round_robin) - 按顺序依次请求</option>
                <option value="weighted_round_robin">加权轮询 (weighted_round_robin) - 按渠道权重分配</option>
                <option value="lottery">抽奖 (lottery) - 按权重随机选择</option>
                <option value="random">随机 (random) - 完全随机</option>
                <option value="smart_round_robin">智能轮询 (smart_round_robin) - 基于历史成功率</option>
              </select>
            </div>
          </div>
        </section>

        {/* 速率限制 */}
        <section className="bg-card border border-border rounded-xl overflow-hidden">
          <div className="p-4 border-b border-border bg-muted/30 flex items-center gap-2 font-medium text-foreground">
            <Shield className="w-5 h-5 text-emerald-500" /> 安全与速率限制
          </div>
          <div className="p-6">
            <label className="text-sm font-medium text-foreground mb-1.5 block">全局速率限制</label>
            <input
              type="text"
              value={preferences.rate_limit || '999999/min'}
              onChange={e => updatePreference('rate_limit', e.target.value)}
              placeholder="100/hour,1000/day"
              className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm font-mono text-foreground"
            />
            <p className="text-xs text-muted-foreground mt-2">支持组合：例如 "15/min,100/hour,1000/day"</p>
          </div>
        </section>

        {/* 超时与心跳 */}
        <section className="bg-card border border-border rounded-xl overflow-hidden">
          <div className="p-4 border-b border-border bg-muted/30 flex items-center gap-2 font-medium text-foreground">
            <Timer className="w-5 h-5 text-blue-500" /> 超时与心跳配置
          </div>
          <div className="p-6 space-y-6">
            <div className="grid grid-cols-2 gap-6">
              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">默认模型超时时间 (秒)</label>
                <input
                  type="number" min="30" max="3600"
                  value={preferences.model_timeout?.default ?? 600}
                  onChange={e => updatePreference('model_timeout', { ...(preferences.model_timeout || {}), default: parseInt(e.target.value) })}
                  className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                />
              </div>
              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">Keepalive 心跳间隔 (秒)</label>
                <input
                  type="number" min="0" max="300"
                  value={preferences.keepalive_interval?.default ?? 25}
                  onChange={e => updatePreference('keepalive_interval', { ...(preferences.keepalive_interval || {}), default: parseInt(e.target.value) })}
                  className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                />
              </div>
            </div>

            <div className="p-4 bg-blue-500/10 border border-blue-500/20 rounded-lg flex gap-3 text-sm">
              <AlertCircle className="w-5 h-5 text-blue-500 flex-shrink-0" />
              <div>
                <div className="font-medium text-blue-700 dark:text-blue-400 mb-1">长思考模型配置建议</div>
                <ul className="list-disc pl-4 space-y-1 text-blue-600 dark:text-blue-300/80">
                  <li>Nginx 反向代理请设置 <code className="bg-blue-500/20 px-1 rounded">proxy_read_timeout 600s;</code></li>
                  <li>对于 DeepSeek R1 / Claude Thinking，建议心跳间隔设为 20-30 秒</li>
                  <li>Keepalive 可以有效防止 CDN 因空闲时间过长断开连接</li>
                </ul>
              </div>
            </div>
          </div>
        </section>

        {/* 数据管理 */}
        <section className="bg-card border border-border rounded-xl overflow-hidden">
          <div className="p-4 border-b border-border bg-muted/30 flex items-center gap-2 font-medium text-foreground">
            <Database className="w-5 h-5 text-purple-500" /> 数据保留策略
          </div>
          <div className="p-6 space-y-6">
            <div>
              <label className="text-sm font-medium text-foreground mb-1.5 block">请求/响应包体保留时间（小时）</label>
              <input
                type="number" min="0"
                value={preferences.log_raw_data_retention_hours ?? 24}
                onChange={e => updatePreference('log_raw_data_retention_hours', parseInt(e.target.value))}
                className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
              />
              <p className="text-xs text-muted-foreground mt-2">控制日志中请求体、响应体等包体字段的保留时长。超时后包体不再展示，但日志记录本身不受影响。设为 0 表示不保存包体，仅记录基础日志信息</p>
            </div>

            <div>
              <label className="text-sm font-medium text-foreground mb-1.5 block">日志记录保留策略</label>
              <select
                value={preferences.log_retention_mode ?? 'keep'}
                onChange={e => updatePreference('log_retention_mode', e.target.value)}
                className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
              >
                <option value="keep">不自动清理（永久保留）</option>
                <option value="manual">仅手动清理</option>
                <option value="auto_delete">自动清理（删除过期日志）</option>
              </select>
              <p className="text-xs text-muted-foreground mt-2">控制整条日志记录的保留，与上方包体保留时间相互独立</p>
            </div>

            {(preferences.log_retention_mode ?? 'keep') === 'auto_delete' && (
              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">保留天数</label>
                <div className="flex flex-wrap gap-2 items-center">
                  <input
                    type="number" min="1" max="3650"
                    value={preferences.log_retention_days ?? 30}
                    onChange={e => {
                      const v = parseInt(e.target.value, 10);
                      updatePreference('log_retention_days', Number.isFinite(v) ? v : 30);
                    }}
                    className="w-40 bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                  />
                  <button type="button" onClick={() => updatePreference('log_retention_days', 7)} className="text-xs bg-muted hover:bg-muted/80 border border-border px-2 py-1 rounded">7 天</button>
                  <button type="button" onClick={() => updatePreference('log_retention_days', 30)} className="text-xs bg-muted hover:bg-muted/80 border border-border px-2 py-1 rounded">30 天</button>
                  <button type="button" onClick={() => updatePreference('log_retention_days', 90)} className="text-xs bg-muted hover:bg-muted/80 border border-border px-2 py-1 rounded">90 天</button>
                </div>
                <p className="text-xs text-muted-foreground mt-2">后台任务每天在指定时间执行一次：删除早于 N 天的 request_stats / channel_stats</p>
              </div>
            )}

            {(preferences.log_retention_mode ?? 'keep') === 'auto_delete' && (
              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">每天执行时间（按服务器时区；容器环境默认可能是 UTC）</label>
                <div className="flex items-center gap-2">
                  <input
                    type="time"
                    value={preferences.log_retention_run_at ?? '03:00'}
                    onChange={e => updatePreference('log_retention_run_at', e.target.value)}
                    className="w-40 bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                  />
                  <span className="text-xs text-muted-foreground">默认 03:00</span>
                </div>
                <p className="text-xs text-muted-foreground mt-2">
                  若不设置时区，系统会使用服务器本地时区（在容器中通常为 UTC）。如需指定时区（例如 Asia/Shanghai），可在配置中设置{' '}
                  <code className="px-1 rounded bg-muted">log_retention_timezone</code>
                </p>
              </div>
            )}
          </div>
        </section>

        {/* 数据库清理工具 */}
        <section className="bg-card border border-border rounded-xl overflow-hidden">
          <div className="p-4 border-b border-border bg-muted/30 flex items-center gap-2 font-medium text-foreground">
            <Database className="w-5 h-5 text-rose-500" /> 数据库清理工具
          </div>
          <div className="p-6 space-y-5">
            <div className="p-4 bg-rose-500/10 border border-rose-500/20 rounded-lg text-sm text-rose-700 dark:text-rose-300">
              <div className="font-medium mb-1">高风险操作提醒</div>
              <ul className="list-disc pl-4 space-y-1 text-xs">
                <li><code className="px-1 rounded bg-rose-500/20">clear_fields</code>：清空大字段，保留日志行（推荐）</li>
                <li><code className="px-1 rounded bg-rose-500/20">delete_rows</code>：直接删除日志行（不可恢复）</li>
                <li>建议先点击“预览匹配结果（Dry Run）”，确认范围后再执行</li>
              </ul>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">清理动作</label>
                <select
                  value={cleanupAction}
                  onChange={e => setCleanupAction(e.target.value as CleanupAction)}
                  className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                >
                  <option value="clear_fields">仅清空字段内容（保留日志）</option>
                  <option value="delete_rows">删除整条日志</option>
                </select>
              </div>

              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">时间筛选模式</label>
                <select
                  value={cleanupTimeMode}
                  onChange={e => setCleanupTimeMode(e.target.value as CleanupTimeMode)}
                  className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                >
                  <option value="older_than_hours">清理早于 N 小时的数据</option>
                  <option value="custom_range">按时间区间清理</option>
                  <option value="all">不按时间筛选（全量）</option>
                </select>
              </div>
            </div>

            {cleanupTimeMode === 'older_than_hours' && (
              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">早于多少小时</label>
                <input
                  type="number"
                  min={1}
                  value={cleanupOlderThanHours}
                  onChange={e => setCleanupOlderThanHours(parseInt(e.target.value || '0', 10))}
                  className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                />
              </div>
            )}

            {cleanupTimeMode === 'custom_range' && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">开始时间</label>
                  <input
                    type="datetime-local"
                    value={cleanupStartTime}
                    onChange={e => setCleanupStartTime(e.target.value)}
                    className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                  />
                </div>
                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">结束时间</label>
                  <input
                    type="datetime-local"
                    value={cleanupEndTime}
                    onChange={e => setCleanupEndTime(e.target.value)}
                    className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                  />
                </div>
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <input type="text" value={cleanupProvider} onChange={e => setCleanupProvider(e.target.value)} placeholder="按渠道过滤（provider/provider_id 模糊匹配）" className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
              <input type="text" value={cleanupModel} onChange={e => setCleanupModel(e.target.value)} placeholder="按模型过滤（模糊匹配）" className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
              <input type="text" value={cleanupApiKey} onChange={e => setCleanupApiKey(e.target.value)} placeholder="按 API Key 名称/分组/前缀过滤" className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
              <input type="text" value={cleanupStatusCodes} onChange={e => setCleanupStatusCodes(e.target.value)} placeholder="按状态码过滤（如 400,401,429）" className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground" />
              <select value={cleanupSuccessMode} onChange={e => setCleanupSuccessMode(e.target.value as CleanupSuccessMode)} className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground md:col-span-2">
                <option value="ALL">所有状态</option>
                <option value="SUCCESS">仅成功请求</option>
                <option value="FAILED">仅失败请求</option>
              </select>
            </div>

            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                checked={cleanupFlaggedOnly}
                onChange={e => setCleanupFlaggedOnly(e.target.checked)}
                className="rounded border-border"
              />
              仅清理已标记日志（is_flagged=true）
            </label>

            {cleanupAction === 'clear_fields' && (
              <div>
                <label className="text-sm font-medium text-foreground mb-2 block">选择要清空的字段</label>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {LOG_CLEANUP_FIELD_OPTIONS.map(item => (
                    <label key={item.key} className="flex items-center gap-2 text-sm text-foreground bg-muted/40 border border-border rounded-lg px-3 py-2">
                      <input
                        type="checkbox"
                        checked={cleanupFields.includes(item.key)}
                        onChange={() => toggleCleanupField(item.key)}
                        className="rounded border-border"
                      />
                      {item.label}
                    </label>
                  ))}
                </div>
              </div>
            )}

            <div className="flex flex-col md:flex-row gap-3 md:items-center">
              <button onClick={handleCleanupPreview} disabled={cleanupRunning} className="bg-secondary hover:bg-secondary/80 text-secondary-foreground px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50">
                {cleanupRunning ? '处理中...' : '预览匹配结果（Dry Run）'}
              </button>
              <input
                type="text"
                value={cleanupConfirmText}
                onChange={e => setCleanupConfirmText(e.target.value)}
                placeholder={`执行前请输入确认词：${requiredConfirmPhrase}`}
                className="flex-1 bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
              />
              <button onClick={handleCleanupExecute} disabled={cleanupRunning} className="bg-red-600 hover:bg-red-500 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50">
                执行清理
              </button>
            </div>

            {cleanupMessage && <div className="text-sm text-muted-foreground">{cleanupMessage}</div>}

            {cleanupResult && (
              <div className="border border-border rounded-lg p-4 bg-muted/40 space-y-2 text-sm">
                <div>匹配记录数：<span className="font-mono">{cleanupResult.matched_rows}</span></div>
                <div>实际影响数：<span className="font-mono">{cleanupResult.affected_rows}</span></div>
                {Object.keys(cleanupResult.non_null_counts || {}).length > 0 && (
                  <div>
                    <div className="text-muted-foreground mb-1">字段非空统计：</div>
                    <pre className="bg-background border border-border rounded-lg p-2 text-xs overflow-x-auto">{JSON.stringify(cleanupResult.non_null_counts, null, 2)}</pre>
                  </div>
                )}
              </div>
            )}
          </div>
        </section>

        {/* 第三方客户端配置 */}
        <section className="bg-card border border-border rounded-xl overflow-hidden">
          <div className="p-4 border-b border-border bg-muted/30 flex items-center justify-between">
            <div className="flex items-center gap-2 font-medium text-foreground">
              <Blocks className="w-5 h-5 text-pink-500" /> 第三方客户端 (Playground)
            </div>
            <button
              onClick={() => {
                const newClients = [...(preferences.external_clients || []), { name: '', icon: '🌟', link: '' }];
                updatePreference('external_clients', newClients);
              }}
              className="text-xs flex items-center gap-1 bg-primary hover:bg-primary/90 text-primary-foreground px-2.5 py-1.5 rounded-md transition-colors"
            >
              <Plus className="w-3.5 h-3.5" /> 添加客户端
            </button>
          </div>
          <div className="p-6 space-y-4">
            <p className="text-xs text-muted-foreground mb-4">这些客户端将显示在 Playground 的侧边栏中。链接中可使用 <code className="bg-muted px-1 py-0.5 rounded text-foreground">{"{key}"}</code> 和 <code className="bg-muted px-1 py-0.5 rounded text-foreground">{"{address}"}</code> 作为变量，系统会自动注入当前 API Key 和网关地址。</p>

            <div className="space-y-3">
              {(preferences.external_clients || []).map((client: ExternalClient, idx: number) => (
                <div key={idx} className="flex gap-3 items-start bg-muted/50 p-4 rounded-lg border border-border">
                  <input
                    type="text"
                    value={client.icon}
                    onChange={e => {
                      const newClients = [...(preferences.external_clients || [])];
                      newClients[idx].icon = e.target.value;
                      updatePreference('external_clients', newClients);
                    }}
                    placeholder="图标"
                    className="w-12 bg-background border border-border px-2 py-2 rounded-lg text-center text-lg focus:border-primary"
                  />
                  <div className="flex-1 space-y-3">
                    <input
                      type="text"
                      value={client.name}
                      onChange={e => {
                        const newClients = [...(preferences.external_clients || [])];
                        newClients[idx].name = e.target.value;
                        updatePreference('external_clients', newClients);
                      }}
                      placeholder="客户端名称 (例如: NextChat)"
                      className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground focus:border-primary"
                    />
                    <div className="relative">
                      <Link className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground/60" />
                      <input
                        type="url"
                        value={client.link}
                        onChange={e => {
                          const newClients = [...(preferences.external_clients || [])];
                          newClients[idx].link = e.target.value;
                          updatePreference('external_clients', newClients);
                        }}
                        placeholder='https://.../?settings={"key":"{key}","url":"{address}"}'
                        className="w-full bg-background border border-border pl-9 pr-3 py-2 rounded-lg text-sm font-mono text-foreground focus:border-primary"
                      />
                    </div>
                  </div>
                  <button
                    onClick={() => {
                      const newClients = (preferences.external_clients || []).filter((_: ExternalClient, i: number) => i !== idx);
                      updatePreference('external_clients', newClients);
                    }}
                    className="p-2 text-muted-foreground/60 hover:text-red-500 hover:bg-red-500/10 rounded-lg transition-colors self-center"
                  >
                    <Trash2 className="w-5 h-5" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        </section>

      </div>
    </div>
  );
}
