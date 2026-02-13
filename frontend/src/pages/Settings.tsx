import { useState, useEffect } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import {
  Settings2, Save, RefreshCw, AlertCircle, Clock, Zap, Shield,
  Timer, Database, Server, Blocks, Plus, Trash2, Edit2, Link
} from 'lucide-react';

export default function Settings() {
  const { token } = useAuthStore();
  const [preferences, setPreferences] = useState<any>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

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
          const loadedPreferences = data.api_config?.preferences || data.preferences || {};

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

  const updatePreference = (key: string, value: any) => {
    setPreferences((prev: any) => ({ ...prev, [key]: value }));
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
        alert('保存失败');
      }
    } catch (err) {
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
                  value={preferences.cooldown_period ?? 300}
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
                  onChange={e => updatePreference('model_timeout', { ...preferences.model_timeout, default: parseInt(e.target.value) })}
                  className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
                />
              </div>
              <div>
                <label className="text-sm font-medium text-foreground mb-1.5 block">Keepalive 心跳间隔 (秒)</label>
                <input
                  type="number" min="0" max="300"
                  value={preferences.keepalive_interval?.default ?? 25}
                  onChange={e => updatePreference('keepalive_interval', { ...preferences.keepalive_interval, default: parseInt(e.target.value) })}
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
          <div className="p-6">
            <label className="text-sm font-medium text-foreground mb-1.5 block">日志原始数据保留时间 (小时)</label>
            <input
              type="number" min="0"
              value={preferences.log_raw_data_retention_hours ?? 24}
              onChange={e => updatePreference('log_raw_data_retention_hours', parseInt(e.target.value))}
              className="w-full bg-background border border-border px-3 py-2 rounded-lg text-sm text-foreground"
            />
            <p className="text-xs text-muted-foreground mt-2">设为 0 表示不保存请求/响应原始数据，减少存储占用</p>
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
              {(preferences.external_clients || []).map((client: any, idx: number) => (
                <div key={idx} className="flex gap-3 items-start bg-muted/50 p-4 rounded-lg border border-border">
                  <input
                    type="text"
                    value={client.icon}
                    onChange={e => {
                      const newClients = [...preferences.external_clients];
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
                        const newClients = [...preferences.external_clients];
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
                          const newClients = [...preferences.external_clients];
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
                      const newClients = preferences.external_clients.filter((_: any, i: number) => i !== idx);
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
