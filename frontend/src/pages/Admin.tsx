import { useState, useEffect } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import {
  Key, Plus, RefreshCw, Copy, Trash2, Edit, Save, X, Search,
  Folder, Clock, CheckCircle2, AlertCircle, AlertTriangle,
  Wand2, Wallet, Brain, Download, Check, CopyCheck
} from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';

// ========== Types ==========
interface ApiKeyData {
  api: string;
  name?: string;
  role?: string;
  groups?: string[];
  group?: string;
  model?: string[];
  preferences?: {
    credits?: number;
    created_at?: string;
    [key: string]: any;
  };
}

interface ApiKeyState {
  enabled: boolean;
  credits: number | null;
  total_cost: number;
  created_at: string;
}

export default function Admin() {
  const { token } = useAuthStore();
  const [keys, setKeys] = useState<ApiKeyData[]>([]);
  const [keyStates, setKeyStates] = useState<Record<string, ApiKeyState>>({});
  const [loading, setLoading] = useState(true);

  // Edit Sheet
  const [isSheetOpen, setIsSheetOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  // Form State
  const [formApi, setFormApi] = useState('');
  const [formName, setFormName] = useState('');
  const [formRole, setFormRole] = useState('');
  const [formGroups, setFormGroups] = useState<string[]>(['default']);
  const [formModels, setFormModels] = useState<string[]>([]);
  const [formCredits, setFormCredits] = useState('');

  // Input states
  const [groupInput, setGroupInput] = useState('');
  const [modelInput, setModelInput] = useState('');

  // Credits Dialog
  const [isCreditsOpen, setIsCreditsOpen] = useState(false);
  const [creditsAmount, setCreditsAmount] = useState('');
  const [creditsTargetKey, setCreditsTargetKey] = useState('');

  // Fetch Models Dialog
  const [isFetchModelsOpen, setIsFetchModelsOpen] = useState(false);
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [modelSearchQuery, setModelSearchQuery] = useState('');
  const [fetchingModels, setFetchingModels] = useState(false);

  // ========== Data Loading ==========
  const fetchData = async () => {
    if (!token) return;
    setLoading(true);
    try {
      const headers = { Authorization: `Bearer ${token}` };
      const [configRes, statesRes] = await Promise.all([
        apiFetch('/v1/api_config', { headers }),
        apiFetch('/v1/api_keys_states', { headers })
      ]);

      if (configRes.ok) {
        const config = await configRes.json();
        setKeys(config.api_config?.api_keys || config.api_keys || []);
      }
      if (statesRes.ok) {
        const states = await statesRes.json();
        setKeyStates(states.api_keys_states || {});
      }
    } catch (err) {
      console.error('Failed to load API keys:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, []);

  // ========== Sheet Handlers ==========
  const openSheet = (index: number | null = null, copyFrom: ApiKeyData | null = null) => {
    setEditingIndex(index);
    setGroupInput('');
    setModelInput('');

    let source: ApiKeyData | null = null;
    if (copyFrom) {
      source = JSON.parse(JSON.stringify(copyFrom));
      source!.api = '';
      source!.name = `${source!.name || 'Key'}_Copy`;
    } else if (index !== null) {
      source = keys[index];
    }

    if (source) {
      setFormApi(source.api || '');
      setFormName(source.name || source.preferences?.name || '');
      setFormRole(source.role || '');

      // Parse groups
      let groups: string[] = ['default'];
      if (Array.isArray(source.groups) && source.groups.length > 0) {
        groups = source.groups;
      } else if (typeof source.group === 'string' && source.group.trim()) {
        groups = [source.group.trim()];
      } else if (source.preferences?.group) {
        groups = [source.preferences.group];
      }
      setFormGroups(groups);

      setFormModels(Array.isArray(source.model) ? [...source.model] : []);
      setFormCredits(source.preferences?.credits !== undefined ? String(source.preferences.credits) : '');
    } else {
      setFormApi('');
      setFormName('');
      setFormRole('');
      setFormGroups(['default']);
      setFormModels([]);
      setFormCredits('');
    }

    setIsSheetOpen(true);
  };

  // ========== Generate Key ==========
  const generateKey = async () => {
    try {
      const res = await apiFetch('/v1/generate-api-key', {
        headers: { Authorization: `Bearer ${token}` }
      });
      const data = await res.json();
      if (data.api_key) {
        setFormApi(data.api_key);
      }
    } catch (err) {
      alert('生成密钥失败');
    }
  };

  // ========== Groups ==========
  const addGroup = () => {
    const val = groupInput.trim();
    if (val && !formGroups.includes(val)) {
      setFormGroups([...formGroups, val]);
    }
    setGroupInput('');
  };

  const removeGroup = (g: string) => {
    const newGroups = formGroups.filter(x => x !== g);
    setFormGroups(newGroups.length ? newGroups : ['default']);
  };

  // ========== Models ==========
  const addModelsFromInput = () => {
    const parts = modelInput.split(/[,\s]+/).map(s => s.trim()).filter(Boolean);
    if (parts.length > 0) {
      const newModels = [...new Set([...formModels, ...parts])];
      setFormModels(newModels);
    }
    setModelInput('');
  };

  const removeModel = (m: string) => {
    setFormModels(formModels.filter(x => x !== m));
  };

  const clearAllModels = () => {
    if (formModels.length === 0) return;
    if (confirm('确定要清空所有模型规则吗？')) {
      setFormModels([]);
    }
  };

  // ========== Fetch Models by Groups ==========
  const openFetchModelsDialog = async () => {
    const groups = formGroups.length > 0 ? formGroups : ['default'];
    setFetchingModels(true);
    setModelSearchQuery('');

    try {
      const res = await apiFetch('/v1/channels/models_by_groups', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ groups })
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`获取模型失败: ${err.detail || res.status}`);
        return;
      }

      const data = await res.json();
      const models = (data.models || []).map((m: any) => m.id || m).filter(Boolean);

      if (models.length === 0) {
        alert('当前分组下没有可用模型');
        return;
      }

      setFetchedModels(models);
      // Pre-select existing models
      const existing = new Set(formModels);
      setSelectedModels(new Set(models.filter((m: string) => existing.has(m))));
      setIsFetchModelsOpen(true);
    } catch (err) {
      alert('获取模型失败');
    } finally {
      setFetchingModels(false);
    }
  };

  const toggleModelSelect = (model: string) => {
    const newSet = new Set(selectedModels);
    if (newSet.has(model)) {
      newSet.delete(model);
    } else {
      newSet.add(model);
    }
    setSelectedModels(newSet);
  };

  const selectAllVisible = () => {
    const filtered = fetchedModels.filter(m =>
      !modelSearchQuery || m.toLowerCase().includes(modelSearchQuery.toLowerCase())
    );
    setSelectedModels(new Set(filtered));
  };

  const deselectAllVisible = () => {
    const filtered = new Set(fetchedModels.filter(m =>
      !modelSearchQuery || m.toLowerCase().includes(modelSearchQuery.toLowerCase())
    ));
    const newSet = new Set(selectedModels);
    filtered.forEach(m => newSet.delete(m));
    setSelectedModels(newSet);
  };

  const confirmFetchModels = () => {
    const existingSet = new Set(formModels);
    selectedModels.forEach(m => existingSet.add(m));
    setFormModels(Array.from(existingSet));
    setIsFetchModelsOpen(false);
  };

  const filteredFetchedModels = fetchedModels.filter(m =>
    !modelSearchQuery || m.toLowerCase().includes(modelSearchQuery.toLowerCase())
  );

  // ========== Save ==========
  const handleSave = async () => {
    if (!formApi.trim()) {
      alert('API Key 不能为空');
      return;
    }

    const target: any = { api: formApi.trim() };

    if (formName.trim()) target.name = formName.trim();
    if (formRole.trim()) target.role = formRole.trim();
    target.groups = formGroups.length > 0 ? formGroups : ['default'];
    if (formModels.length > 0) target.model = formModels;

    // Preferences
    const prefs: any = {};
    if (formCredits.trim()) {
      const num = Number(formCredits);
      if (!isNaN(num)) prefs.credits = num;
    }
    if (Object.keys(prefs).length > 0) target.preferences = prefs;

    const newKeys = [...keys];
    if (editingIndex !== null) {
      // Preserve existing preferences except credits
      const existing = keys[editingIndex];
      if (existing.preferences) {
        target.preferences = { ...existing.preferences, ...prefs };
      }
      newKeys[editingIndex] = target;
    } else {
      newKeys.push(target);
    }

    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ api_keys: newKeys })
      });
      if (res.ok) {
        setKeys(newKeys);
        setIsSheetOpen(false);
        fetchData();
      } else {
        alert('保存失败');
      }
    } catch (err) {
      alert('网络错误');
    }
  };

  // ========== Delete ==========
  const handleDelete = async (index: number) => {
    const keyObj = keys[index];
    const name = keyObj.name || keyObj.api?.slice(0, 12) + '...';
    if (!confirm(`确定要删除 API Key "${name}" 吗？`)) return;

    const newKeys = keys.filter((_, i) => i !== index);
    try {
      const res = await apiFetch('/v1/api_config/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ api_keys: newKeys })
      });
      if (res.ok) {
        setKeys(newKeys);
        fetchData();
      } else {
        alert('删除失败');
      }
    } catch (err) {
      alert('网络错误');
    }
  };

  // ========== Add Credits ==========
  const openCreditsDialog = (key: string) => {
    setCreditsTargetKey(key);
    setCreditsAmount('');
    setIsCreditsOpen(true);
  };

  const handleAddCredits = async () => {
    const amount = parseFloat(creditsAmount);
    if (isNaN(amount) || amount <= 0) {
      alert('请输入大于 0 的有效数字');
      return;
    }

    try {
      const res = await apiFetch(`/v1/add_credits?paid_key=${encodeURIComponent(creditsTargetKey)}&amount=${amount}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` }
      });
      if (res.ok) {
        setIsCreditsOpen(false);
        fetchData();
      } else {
        const data = await res.json().catch(() => ({}));
        alert(`充值失败: ${data.detail || res.status}`);
      }
    } catch (err) {
      alert('网络错误');
    }
  };

  // ========== Helpers ==========
  const getStatusInfo = (keyStr: string) => {
    const state = keyStates[keyStr];
    if (!state) return { icon: <AlertCircle className="w-3.5 h-3.5" />, label: '未知', cls: 'bg-muted text-muted-foreground' };
    if (state.enabled) return { icon: <CheckCircle2 className="w-3.5 h-3.5" />, label: '启用中', cls: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-500 border border-emerald-500/20' };
    return { icon: <AlertTriangle className="w-3.5 h-3.5" />, label: '已停用', cls: 'bg-red-500/10 text-red-600 dark:text-red-500 border border-red-500/20' };
  };

  const getCreditsInfo = (keyStr: string) => {
    const state = keyStates[keyStr];
    if (!state) return { text: '—', usage: '' };
    const credits = state.credits;
    const cost = state.total_cost || 0;
    if (credits === null || credits === undefined || credits < 0) {
      return { text: '不限额度', usage: `已用 ${cost.toFixed(2)}` };
    }
    const balance = Math.max(0, credits - cost);
    return { text: `${credits.toFixed(2)}`, usage: `已用 ${cost.toFixed(2)}，剩余 ${balance.toFixed(2)}` };
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  return (
    <div className="space-y-6 animate-in fade-in duration-500 font-sans">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-foreground">API 密钥管理</h1>
          <p className="text-muted-foreground mt-1">管理调用 Zoaholic 网关的下游 API Key、额度与权限</p>
        </div>
        <div className="flex gap-2">
          <button onClick={fetchData} className="p-2 text-muted-foreground hover:text-foreground bg-card border border-border rounded-lg">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button onClick={() => openSheet()} className="bg-primary hover:bg-primary/90 text-primary-foreground px-4 py-2 rounded-lg flex items-center gap-2 font-medium">
            <Plus className="w-4 h-4" /> 新增 API Key
          </button>
        </div>
      </div>

      {/* Table */}
      <div className="bg-card border border-border rounded-xl overflow-hidden">
        {loading && keys.length === 0 ? (
          <div className="p-12 flex flex-col items-center justify-center text-muted-foreground">
            <RefreshCw className="w-8 h-8 animate-spin mb-3" />
            <p>加载密钥数据...</p>
          </div>
        ) : keys.length === 0 ? (
          <div className="p-16 flex flex-col items-center justify-center text-muted-foreground">
            <Key className="w-12 h-12 mb-3 opacity-50" />
            <h3 className="text-lg font-medium text-foreground">暂无 API 密钥</h3>
            <p className="text-sm mt-1 mb-4">创建您的第一个密钥以允许客户端接入</p>
            <button onClick={() => openSheet()} className="text-primary hover:underline text-sm font-medium">+ 新增 API Key</button>
          </div>
        ) : (
          <table className="w-full text-left border-collapse">
            <thead className="bg-muted border-b border-border text-muted-foreground text-sm font-medium">
              <tr>
                <th className="px-6 py-4">名称 / Key</th>
                <th className="px-6 py-4">角色</th>
                <th className="px-6 py-4 text-center">额度 / 使用</th>
                <th className="px-6 py-4">模型规则</th>
                <th className="px-6 py-4 text-center">状态</th>
                <th className="px-6 py-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border text-sm">
              {keys.map((keyObj, idx) => {
                const status = getStatusInfo(keyObj.api);
                const credits = getCreditsInfo(keyObj.api);
                const state = keyStates[keyObj.api];
                const name = keyObj.name || keyObj.preferences?.name || '未命名密钥';
                const groups = keyObj.groups || (keyObj.group ? [keyObj.group] : ['default']);
                const models = keyObj.model || [];
                const modelText = models.length === 0 ? '默认: all' :
                  (models.length === 1 && models[0] === 'all') ? '全部模型 (all)' :
                    models.length > 3 ? `${models.slice(0, 3).join(', ')} 等 ${models.length} 条` : models.join(', ');

                return (
                  <tr key={idx} className="hover:bg-muted/50 transition-colors group">
                    <td className="px-6 py-4">
                      <div className="font-medium text-foreground">{name}</div>
                      <div className="text-xs text-muted-foreground font-mono mt-1 flex items-center gap-1.5">
                        <Key className="w-3 h-3" />
                        {keyObj.api.slice(0, 7)}...{keyObj.api.slice(-4)}
                        <button onClick={() => copyToClipboard(keyObj.api)} className="text-muted-foreground/60 hover:text-foreground">
                          <Copy className="w-3 h-3" />
                        </button>
                      </div>
                      {state?.created_at && (
                        <div className="text-xs text-muted-foreground/60 mt-1">创建: {state.created_at}</div>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      <span className={`px-2 py-1 rounded text-xs font-medium ${keyObj.role === 'admin' ? 'bg-purple-500/10 text-purple-600 dark:text-purple-400 border border-purple-500/20' : 'bg-muted text-muted-foreground'}`}>
                        {keyObj.role || 'user'}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-center">
                      <div className="text-foreground">{credits.text}</div>
                      <div className="text-xs text-muted-foreground mt-0.5">{credits.usage}</div>
                    </td>
                    <td className="px-6 py-4 max-w-[200px]">
                      <div className="text-xs text-muted-foreground truncate mb-1" title={models.join(', ')}>{modelText}</div>
                      <div className="flex flex-wrap gap-1">
                        {groups.map(g => (
                          <span key={g} className="flex items-center gap-1 text-[11px] bg-muted text-foreground px-1.5 py-0.5 rounded">
                            <Folder className="w-3 h-3" />{g}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="px-6 py-4 text-center">
                      <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium ${status.cls}`}>
                        {status.icon} {status.label}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        <button onClick={() => openCreditsDialog(keyObj.api)} className="p-1.5 text-emerald-600 dark:text-emerald-500 hover:bg-emerald-500/10 rounded-md" title="充值额度">
                          <Wallet className="w-4 h-4" />
                        </button>
                        <button onClick={() => openSheet(null, keyObj)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md" title="复制配置">
                          <Copy className="w-4 h-4" />
                        </button>
                        <button onClick={() => openSheet(idx)} className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md" title="编辑">
                          <Edit className="w-4 h-4" />
                        </button>
                        <button onClick={() => handleDelete(idx)} className="p-1.5 text-red-600 dark:text-red-500 hover:bg-red-500/10 rounded-md" title="删除">
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* ========== Edit Side Sheet ========== */}
      <Dialog.Root open={isSheetOpen} onOpenChange={setIsSheetOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40" />
          <Dialog.Content className="fixed right-0 top-0 h-full w-[560px] bg-background border-l border-border shadow-2xl z-50 flex flex-col animate-in slide-in-from-right duration-300">
            <div className="p-5 border-b border-border flex justify-between items-center bg-muted/30">
              <Dialog.Title className="text-lg font-bold text-foreground flex items-center gap-2">
                <Key className="w-5 h-5 text-primary" />
                {editingIndex !== null ? '编辑 API Key' : '新增 API Key'}
              </Dialog.Title>
              <Dialog.Close className="text-muted-foreground hover:text-foreground"><X className="w-5 h-5" /></Dialog.Close>
            </div>

            <div className="flex-1 overflow-y-auto p-5 space-y-6">
              {/* Basic Info Section */}
              <section className="space-y-4">
                <div className="text-sm font-semibold text-foreground border-b border-border pb-2 flex items-center gap-2">
                  <Key className="w-4 h-4 text-primary" /> 基础信息
                </div>

                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">Key 名称</label>
                  <input
                    type="text" value={formName} onChange={e => setFormName(e.target.value)}
                    placeholder="例如 生产环境Key、测试用Key"
                    className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm text-foreground"
                  />
                  <p className="text-xs text-muted-foreground mt-1">为此 API Key 设置一个友好的显示名称</p>
                </div>

                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">API Key</label>
                  <div className="flex gap-2">
                    <input
                      type="text" value={formApi} onChange={e => setFormApi(e.target.value)}
                      placeholder="sk-xxx..."
                      className="flex-1 bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono text-foreground"
                    />
                    <button onClick={generateKey} className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg flex items-center gap-1.5 text-sm">
                      <Wand2 className="w-4 h-4" /> 生成
                    </button>
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">建议使用以 sk- 开头的随机字符串</p>
                </div>

                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">角色 (role)</label>
                  <input
                    type="text" value={formRole} onChange={e => setFormRole(e.target.value)}
                    placeholder="例如 admin, paid 或 user"
                    className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm text-foreground"
                  />
                  <p className="text-xs text-muted-foreground mt-1">包含 'admin' 的 Key 将被视为管理 Key</p>
                </div>

                {/* Groups */}
                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">分组</label>
                  <div className="flex flex-wrap gap-2 mb-2 p-2 bg-muted/50 border border-border rounded-lg min-h-[40px]">
                    {formGroups.map(g => (
                      <span key={g} className="bg-background border border-border text-foreground px-2 py-1 rounded text-xs flex items-center gap-1">
                        <Folder className="w-3 h-3" /> {g}
                        <button onClick={() => removeGroup(g)} className="ml-1 text-muted-foreground hover:text-red-500"><X className="w-3 h-3" /></button>
                      </span>
                    ))}
                  </div>
                  <div className="flex gap-2">
                    <input
                      type="text" value={groupInput} onChange={e => setGroupInput(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addGroup(); } }}
                      placeholder="输入分组名..."
                      className="flex-1 bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm text-foreground"
                    />
                    <button onClick={addGroup} className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg text-sm">添加</button>
                  </div>
                </div>
              </section>

              {/* Quota Section */}
              <section className="space-y-4">
                <div className="text-sm font-semibold text-foreground border-b border-border pb-2 flex items-center gap-2">
                  <Wallet className="w-4 h-4 text-emerald-500" /> 额度与模型
                </div>

                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">额度 (credits)</label>
                  <input
                    type="number" value={formCredits} onChange={e => setFormCredits(e.target.value)}
                    placeholder="留空或负数表示不限制"
                    className="w-full bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm text-foreground"
                  />
                  <p className="text-xs text-muted-foreground mt-1">与统计模块配合: credits - total_cost = 剩余余额</p>
                </div>
              </section>

              {/* Models Section */}
              <section className="space-y-4">
                <div className="text-sm font-semibold text-foreground border-b border-border pb-2 flex items-center gap-2">
                  <Brain className="w-4 h-4 text-purple-500" /> 模型配置
                </div>

                {/* Actions */}
                <div className="flex items-center gap-2">
                  <button
                    onClick={openFetchModelsDialog}
                    disabled={fetchingModels}
                    className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg text-sm flex items-center gap-1.5 disabled:opacity-50"
                  >
                    <Download className={`w-4 h-4 ${fetchingModels ? 'animate-spin' : ''}`} /> 获取模型
                  </button>
                  <button onClick={clearAllModels} className="bg-red-500/10 text-red-600 dark:text-red-500 px-3 py-2 rounded-lg text-sm">清空全部</button>
                </div>

                {/* Model Chips */}
                <div className="bg-muted/50 border border-border rounded-lg p-3 min-h-[100px] max-h-[200px] overflow-y-auto">
                  {formModels.length === 0 ? (
                    <div className="text-center text-muted-foreground text-sm py-4">暂无模型规则，点击「获取模型」或手动添加。留空表示默认 all。</div>
                  ) : (
                    <div className="flex flex-wrap gap-2">
                      {formModels.map((m, idx) => (
                        <span
                          key={idx}
                          className="bg-background border border-border text-foreground text-xs font-mono px-2 py-1 rounded flex items-center gap-1 cursor-pointer hover:bg-muted"
                          onClick={() => copyToClipboard(m)}
                          title="点击复制"
                        >
                          {m}
                          <button onClick={(e) => { e.stopPropagation(); removeModel(m); }} className="text-muted-foreground hover:text-red-500 ml-1">
                            <X className="w-3 h-3" />
                          </button>
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                {/* Manual Input */}
                <div>
                  <label className="text-sm font-medium text-foreground mb-1.5 block">手动输入模型规则</label>
                  <div className="flex gap-2">
                    <input
                      type="text" value={modelInput} onChange={e => setModelInput(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addModelsFromInput(); } }}
                      placeholder="例如 all, gpt-4o 用空格/逗号分隔"
                      className="flex-1 bg-background border border-border focus:border-primary px-3 py-2 rounded-lg text-sm font-mono text-foreground"
                    />
                    <button onClick={addModelsFromInput} className="bg-muted hover:bg-muted/80 text-foreground px-3 py-2 rounded-lg text-sm">添加</button>
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">多个用逗号或空格分隔，按回车快速添加</p>
                </div>
              </section>
            </div>

            {/* Footer */}
            <div className="p-4 bg-muted/30 border-t border-border flex justify-end gap-3">
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              <button onClick={handleSave} className="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary hover:bg-primary/90 rounded-lg flex items-center gap-1.5">
                <Save className="w-4 h-4" /> 保存
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* ========== Fetch Models Dialog ========== */}
      <Dialog.Root open={isFetchModelsOpen} onOpenChange={setIsFetchModelsOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] max-h-[80vh] bg-background border border-border rounded-xl shadow-2xl z-50 flex flex-col">
            <div className="p-5 border-b border-border">
              <Dialog.Title className="text-lg font-bold text-foreground">选择模型</Dialog.Title>
              <p className="text-sm text-muted-foreground mt-1">当前分组: {formGroups.join(', ')}</p>
            </div>

            <div className="p-4 border-b border-border">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <input
                  type="text" value={modelSearchQuery} onChange={e => setModelSearchQuery(e.target.value)}
                  placeholder="搜索模型名称..."
                  className="w-full bg-muted border border-border pl-10 pr-4 py-2.5 rounded-full text-sm text-foreground"
                />
              </div>
            </div>

            <div className="p-4 border-b border-border flex items-center justify-between">
              <span className="text-sm text-muted-foreground">
                显示 {filteredFetchedModels.length} / {fetchedModels.length} 个模型，已选 {selectedModels.size} 个
              </span>
              <div className="flex gap-2">
                <button onClick={selectAllVisible} className="text-sm text-primary hover:underline">全选</button>
                <button onClick={deselectAllVisible} className="text-sm text-muted-foreground hover:text-foreground">全不选</button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto max-h-[360px]">
              {filteredFetchedModels.map(model => {
                const isSelected = selectedModels.has(model);
                const isExisting = formModels.includes(model);
                return (
                  <div
                    key={model}
                    onClick={() => toggleModelSelect(model)}
                    className="px-4 py-2.5 flex items-center hover:bg-muted cursor-pointer border-b border-border last:border-b-0"
                  >
                    <div className={`w-5 h-5 rounded border-2 flex items-center justify-center mr-3 transition-colors ${isSelected ? 'bg-primary border-primary' : 'border-muted-foreground/50'}`}>
                      {isSelected && <Check className="w-3 h-3 text-primary-foreground" />}
                    </div>
                    <span className="flex-1 font-mono text-sm text-foreground truncate">{model}</span>
                    {isExisting && <span className="text-xs bg-primary/20 text-primary px-2 py-0.5 rounded">已添加</span>}
                  </div>
                );
              })}
            </div>

            <div className="p-4 border-t border-border flex justify-end gap-3">
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              <button onClick={confirmFetchModels} className="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary hover:bg-primary/90 rounded-lg">
                确认选择 ({selectedModels.size})
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* ========== Add Credits Dialog ========== */}
      <Dialog.Root open={isCreditsOpen} onOpenChange={setIsCreditsOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[400px] bg-background border border-border rounded-xl shadow-2xl z-50 p-6">
            <Dialog.Title className="text-lg font-bold text-foreground flex items-center gap-2 mb-4">
              <Wallet className="w-5 h-5 text-emerald-500" /> 为 API Key 添加额度
            </Dialog.Title>

            <div className="text-sm text-muted-foreground break-all bg-muted p-3 rounded-lg font-mono border border-border mb-4">
              目标: {creditsTargetKey.slice(0, 15)}...
            </div>

            <div className="mb-6">
              <label className="text-sm font-medium text-foreground mb-1.5 block">增加额度</label>
              <input
                type="number" value={creditsAmount} onChange={e => setCreditsAmount(e.target.value)}
                placeholder="例如 100"
                autoFocus
                className="w-full bg-background border border-border focus:border-emerald-500 px-3 py-2.5 rounded-lg text-sm text-foreground"
              />
              <p className="text-xs text-muted-foreground mt-2">单位与统计模块中的 credits 相同，必须为正数</p>
            </div>

            <div className="flex justify-end gap-3">
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              <button onClick={handleAddCredits} className="px-4 py-2 text-sm font-medium text-white bg-emerald-600 hover:bg-emerald-500 rounded-lg">确认添加</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
