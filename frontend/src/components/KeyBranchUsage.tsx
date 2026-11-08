import { useEffect, useState } from 'react';
import { apiFetch } from '../lib/api';

interface Props { id: string; token: string; name: string }
interface UsageRow { request_count?: number; total_tokens?: number; recorded_cost?: number }

export function KeyBranchUsage({ id, token, name }: Props) {
  const [scope, setScope] = useState<'direct' | 'branch'>('direct');
  const [hours, setHours] = useState(24);
  const [rows, setRows] = useState<UsageRow[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    setRows([]);
    const query = new URLSearchParams({ api_key_id: id, key_scope: scope, hours: String(hours) });
    apiFetch(`/v1/stats/usage_analysis?${query}`, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then(async response => {
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || '读取用量失败');
        if (!controller.signal.aborted) setRows(result.data || []);
      })
      .catch(cause => { if (!controller.signal.aborted) setError(String(cause.message || cause)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [id, token, scope, hours]);
  const totals = rows.reduce((sum, row) => ({ requests: sum.requests + (row.request_count || 0), tokens: sum.tokens + (row.total_tokens || 0), cost: sum.cost + (row.recorded_cost || 0) }), { requests: 0, tokens: 0, cost: 0 });
  return <section className="rounded-lg border border-border p-3 text-sm space-y-2">
    <h3 className="font-medium">{name}：历史用量</h3>
    <div className="flex flex-wrap gap-2">
      <label>统计范围 <select value={scope} onChange={event => setScope(event.target.value as 'direct' | 'branch')} className="rounded border border-border bg-background p-1"><option value="direct">直接调用</option><option value="branch">包含全部下级</option></select></label>
      <label>时间 <select value={hours} onChange={event => setHours(Number(event.target.value))} className="rounded border border-border bg-background p-1"><option value={24}>24 小时</option><option value={168}>7 天</option><option value={720}>30 天</option></select></label>
    </div>
    {loading ? <p>正在读取…</p> : error ? <p role="alert" className="text-red-600">{error}</p> : <p>{totals.requests.toLocaleString()} 次请求 · {totals.tokens.toLocaleString()} Token · 记录费用 {totals.cost.toFixed(6)}</p>}
    <p className="text-xs text-muted-foreground">按请求发生时的归属统计，迁移不改写历史；分支汇总已包含直接调用，请勿重复相加。</p>
  </section>;
}
