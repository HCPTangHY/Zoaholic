import { useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { AlertCircle, CheckCircle2, ChevronRight, Copy, Loader2, Power, X } from 'lucide-react';
import { apiFetch } from '../../../lib/api';
import { fmtErr } from '../../../components/Toast';
import { buildProviderApiPath } from '../utils';
import { COOLING_FIELDS, executeProviderBatch, type BatchProvider, type ProviderBatchAction } from '../lib/batchProviders';

const buttonClass = 'inline-flex items-center justify-center gap-2 rounded-lg border border-border bg-background px-3 py-2 text-sm font-medium transition-all duration-150 hover:bg-muted active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50';
const primaryClass = `${buttonClass} border-transparent bg-primary text-primary-foreground hover:bg-primary/90`;
const selectClass = 'mt-2 block w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-50';

type Props = {
  providers: BatchProvider[];
  selectedNames: string[];
  token: string | null;
  refresh: () => Promise<void>;
  busy: boolean;
  setBusy: (busy: boolean) => void;
  clearSelection: () => void;
};
type Report = Awaited<ReturnType<typeof executeProviderBatch>>;

function TargetList({ names, label = '目标渠道' }: { names: string[]; label?: string }) {
  return <details className="rounded-xl border border-border p-3">
    <summary className="cursor-pointer text-sm font-medium">{label} · {names.length} 个</summary>
    <ul className="mt-3 max-h-40 space-y-2 overflow-y-auto text-sm text-muted-foreground">{names.map(name => <li key={name} className="break-all">{name}</li>)}</ul>
  </details>;
}

const coolingLabels: Record<typeof COOLING_FIELDS[number], string> = {
  cooldown_period: '渠道冷却', api_key_cooldown_period: 'Key 冷却', key_rules: 'Key Rules',
  auto_disable_key: '自动停用', status_code_overrides: '状态码规则',
};
function describeSetting(value: unknown): string {
  if (value === undefined) return '未配置，将移除目标旧设置';
  if (value === null) return '清空';
  if (typeof value === 'boolean') return value ? '开启' : '关闭';
  if (Array.isArray(value)) return `${value.length} 条规则`;
  if (typeof value === 'object') return `${Object.keys(value).length} 项设置`;
  return String(value);
}

export function ProviderBatchActions({ providers, selectedNames, token, refresh, busy, setBusy, clearSelection }: Props) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<'enable' | 'disable' | 'copy'>('enable');
  const [sourceName, setSourceName] = useState('');
  const [cooling, setCooling] = useState(false);
  const [models, setModels] = useState<'none' | 'merge' | 'replace'>('none');
  const [targets, setTargets] = useState<string[]>([]);
  const [preview, setPreview] = useState<{ names: string[]; action: ProviderBatchAction } | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [refreshError, setRefreshError] = useState(false);
  const activeOperation = useRef(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const headers = { Authorization: `Bearer ${token}` };

  const read = async (name: string): Promise<BatchProvider> => {
    const response = await apiFetch(buildProviderApiPath(name), { headers });
    if (!response.ok) throw new Error(fmtErr(await response.json().catch(() => ({})), response.status));
    const data = await response.json();
    if (!data.provider || data.provider.provider !== name) throw new Error('渠道数据无效，请刷新后重试');
    return data.provider;
  };
  const start = (nextKind: typeof kind, trigger: HTMLButtonElement) => {
    if (busy || !selectedNames.length) return;
    triggerRef.current = trigger;
    const names = [...new Set(selectedNames)];
    setTargets(names); setKind(nextKind); setReport(null); setError(''); setRefreshError(false);
    setSourceName(''); setCooling(false); setModels('none');
    setPreview(nextKind === 'copy' ? null : { names, action: { kind: 'enabled', enabled: nextKind === 'enable' } });
    setOpen(true);
  };
  const prepare = async () => {
    if (activeOperation.current) return;
    activeOperation.current = true; setBusy(true); setError('');
    try {
      const source = await read(sourceName);
      const names = targets.filter(name => name !== sourceName);
      if (!names.length) throw new Error('请选择源渠道之外的目标渠道');
      setPreview({ names, action: { kind: 'copy', source, cooling, models } });
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { activeOperation.current = false; setBusy(false); }
  };
  const apply = async () => {
    if (!preview || activeOperation.current) return;
    activeOperation.current = true; setBusy(true); setError('');
    try {
      const result = await executeProviderBatch(preview.names, preview.action, read, async (name, provider) => {
        const response = await apiFetch(buildProviderApiPath(name), {
          method: 'PUT', headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(provider),
        });
        if (!response.ok) throw new Error(fmtErr(await response.json().catch(() => ({})), response.status));
      });
      setReport(result); setPreview(null); clearSelection();
      try { await refresh(); } catch { setRefreshError(true); }
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { activeOperation.current = false; setBusy(false); }
  };
  const title = report ? '操作结果' : kind === 'copy' ? '复制配置' : kind === 'enable' ? '启用渠道' : '停用渠道';

  return <>
    <div className={selectedNames.length ? 'flex flex-wrap items-center gap-2' : 'hidden'}>
      <button type="button" disabled={busy} onClick={event => start('enable', event.currentTarget)} className={buttonClass}><Power className="h-4 w-4" aria-hidden="true" />启用</button>
      <button type="button" disabled={busy} onClick={event => start('disable', event.currentTarget)} className={buttonClass}>停用</button>
      <button type="button" disabled={busy} onClick={event => start('copy', event.currentTarget)} className={buttonClass}><Copy className="h-4 w-4" aria-hidden="true" />复制配置</button>
    </div>
    <Dialog.Root open={open} onOpenChange={value => { if (!activeOperation.current) setOpen(value); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[60] bg-black/50 backdrop-blur-sm" />
        <Dialog.Content onEscapeKeyDown={event => { if (activeOperation.current) event.preventDefault(); }} onPointerDownOutside={event => { if (activeOperation.current) event.preventDefault(); }} onCloseAutoFocus={event => {
          event.preventDefault();
          if (triggerRef.current?.isConnected && triggerRef.current.getClientRects().length) triggerRef.current.focus();
          else document.getElementById('provider-selection-toggle')?.focus();
        }} className="fixed left-1/2 top-1/2 z-[70] flex max-h-[90dvh] w-[calc(100%_-_2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-border bg-background shadow-2xl">
          <header className="border-b border-border p-4 sm:p-6">
            <div className="flex items-center justify-between gap-3"><Dialog.Title className="text-lg font-semibold tracking-tight">{title}</Dialog.Title><Dialog.Close aria-label="关闭对话框" disabled={busy} className={`${buttonClass} p-2`}><X className="h-4 w-4" /></Dialog.Close></div>
            <Dialog.Description className="mt-2 text-sm leading-relaxed text-muted-foreground">{report ? '已完成的修改立即生效，未成功的渠道保持原状或需刷新核对。' : kind === 'copy' ? '将选定配置应用到目标渠道，连接地址、凭据和分组保持不变。' : `即将${kind === 'enable' ? '启用' : '停用'} ${targets.length} 个渠道。${kind === 'disable' ? '其子渠道也将无法调用。' : ''}`}</Dialog.Description>
            {kind === 'copy' && !report && <div className="mt-4 flex items-center gap-2 text-xs text-muted-foreground"><span className={!preview ? 'font-medium text-foreground' : ''}>1 选择配置</span><ChevronRight className="h-3 w-3" /><span className={preview ? 'font-medium text-foreground' : ''}>2 确认应用</span></div>}
          </header>
          <div className="min-h-0 space-y-4 overflow-y-auto p-4 sm:p-6" aria-busy={busy}>
            {error && <p role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{error}</p>}
            {report ? <>
              <div role="status" className="flex items-center gap-3 rounded-xl bg-muted/50 p-4">{report.failed.length ? <AlertCircle className="h-5 w-5 shrink-0 text-muted-foreground" /> : <CheckCircle2 className="h-5 w-5 shrink-0 text-muted-foreground" />}<p className="text-sm">成功 <strong>{report.succeeded.length}</strong> 个，失败 <strong>{report.failed.length}</strong> 个</p></div>
              {report.failed.length > 0 && <div className="space-y-3"><h3 className="text-sm font-medium">未成功的渠道</h3>{report.failed.map(item => <div key={item.name} className="rounded-lg border border-border p-3"><p className="break-all text-sm font-medium">{item.name}</p><p className="mt-1 break-words text-xs leading-relaxed text-muted-foreground">{item.error}</p></div>)}</div>}
              {report.succeeded.length > 0 && <TargetList names={report.succeeded} label="成功渠道" />}
              {refreshError && <p role="alert" className="text-sm text-muted-foreground">列表刷新失败，请手动刷新确认最终状态，不要重复提交已成功的操作。</p>}
            </> : preview ? <>
              {preview.action.kind === 'copy' && <>
                <div className="rounded-xl bg-muted/40 p-4"><p className="text-xs text-muted-foreground">源渠道</p><p className="mt-1 break-all text-sm font-medium">{preview.action.source.provider}</p></div>
                <div className="space-y-2 text-sm">{preview.action.cooling && <p>替换冷却与 Key Rules 配置</p>}{preview.action.models !== 'none' && <p>{preview.action.models === 'merge' ? '合并模型列表，重复模型保留目标映射' : '替换整个模型列表，源为空时清空'}</p>}</div>
                <details className="rounded-xl border border-border p-3"><summary className="cursor-pointer text-sm font-medium">配置摘要</summary><dl className="mt-3 space-y-3 text-xs">
                  {preview.action.cooling && COOLING_FIELDS.map(field => <div key={field} className="flex flex-wrap justify-between gap-2"><dt className="text-muted-foreground">{coolingLabels[field]}</dt><dd className="break-words">{describeSetting(preview.action.kind === 'copy' ? preview.action.source.preferences?.[field] : undefined)}</dd></div>)}
                  {preview.action.models !== 'none' && <div><dt className="text-muted-foreground">模型 · {preview.action.source.model?.length ?? 0} 项</dt><dd className="mt-2 max-h-32 space-y-1 overflow-y-auto">{(preview.action.source.model ?? []).map((model, index) => <p key={index} className="break-all">{typeof model === 'string' ? model : Object.entries(model).map(([upstream, alias]) => `${upstream} → ${alias}`).join('、')}</p>)}</dd></div>}
                </dl></details>
              </>}
              <TargetList names={preview.names} />
              <p className="text-xs leading-relaxed text-muted-foreground">逐个保存，不支持整批回滚。请勿同时编辑这些渠道。{preview.action.kind === 'copy' && preview.action.cooling ? '源中未配置的冷却兼容设置会从目标移除。' : ''}</p>
            </> : <>
              <label className="block text-sm font-medium">源渠道<select disabled={busy} value={sourceName} onChange={event => setSourceName(event.target.value)} className={selectClass}><option value="">请选择渠道</option>{providers.map(provider => <option key={provider.provider} value={provider.provider}>{provider.provider}</option>)}</select></label>
              <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-border p-4"><input type="checkbox" disabled={busy} checked={cooling} onChange={event => setCooling(event.target.checked)} className="mt-1 h-4 w-4 shrink-0 accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary" /><span><span className="text-sm font-medium">冷却与 Key Rules</span><span className="mt-1 block text-xs leading-relaxed text-muted-foreground">包含冷却时间、自动停用、重试及状态码规则。</span></span></label>
              <label className="block text-sm font-medium">模型列表<select disabled={busy} value={models} onChange={event => setModels(event.target.value as typeof models)} className={selectClass}><option value="none">不修改</option><option value="merge">合并模型</option><option value="replace">替换模型</option></select></label>
              <p className="text-xs leading-relaxed text-muted-foreground">已选 {targets.length} 个渠道。源渠道不会作为复制目标。</p>
            </>}
          </div>
          <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-border bg-muted/20 p-4 sm:px-6">
            {busy && <span role="status" className="mr-auto flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />{preview ? '正在保存，请稍候' : '正在读取配置'}</span>}
            {report ? <Dialog.Close className={primaryClass} disabled={busy}>完成</Dialog.Close> : <>
              {preview && kind === 'copy' ? <button type="button" disabled={busy} onClick={() => { setPreview(null); setError(''); }} className={buttonClass}>上一步</button> : <Dialog.Close disabled={busy} className={buttonClass}>取消</Dialog.Close>}
              {preview ? <button type="button" disabled={busy} onClick={apply} className={primaryClass}>{kind === 'copy' ? '确认应用' : kind === 'enable' ? '确认启用' : '确认停用'}</button> : <button type="button" disabled={busy || !sourceName || (!cooling && models === 'none')} onClick={prepare} className={primaryClass}>下一步</button>}
            </>}
          </footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  </>;
}
