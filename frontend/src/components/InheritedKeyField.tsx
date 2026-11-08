import type { ReactNode } from 'react';

interface Props {
  label: string;
  inherited: boolean;
  hasParent: boolean;
  parentName: string;
  value: unknown;
  onOverride: () => void;
  onInherit: () => void;
  children: ReactNode;
}

const displayValue = (value: unknown): string => {
  if (value === undefined || value === null) return '未设置';
  if (typeof value === 'boolean') return value ? '开启' : '关闭';
  if (Array.isArray(value)) return value.map(displayValue).join('、') || '无';
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}：${displayValue(item)}`).join('；') || '无';
  return String(value);
};

function InheritedValue({ value, label }: { value: unknown; label: string }) {
  if (value === undefined || value === null) return <span className="text-muted-foreground">上级未设置</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="text-muted-foreground">{label === '模型权限' || label === '分组' ? '无授权项' : '未配置项目'}</span>;
    return <div className="flex flex-wrap gap-2">{value.map((item, index) => <span key={index} className="max-w-full break-words rounded-md border border-border bg-background px-2 py-1 text-xs">{displayValue(item)}</span>)}</div>;
  }
  if (typeof value === 'object') return <dl className="space-y-2">{Object.entries(value).map(([key, item]) => <div key={key} className="flex flex-wrap justify-between gap-2"><dt className="text-muted-foreground">{key}</dt><dd className="break-words">{displayValue(item)}</dd></div>)}</dl>;
  return <span>{displayValue(value)}</span>;
}

export function InheritedKeyField({ label, inherited, hasParent, parentName, value, onOverride, onInherit, children }: Props) {
  return <div className="space-y-3">
    {hasParent && <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
      <span className="text-muted-foreground">{label} · {inherited ? `继承自「${parentName}」` : '独立配置'}</span>
      <button type="button" onClick={inherited ? onOverride : onInherit} className="rounded-md border border-border px-2 py-1 text-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
        {inherited ? '改为独立配置' : '恢复继承'}
      </button>
    </div>}
    {inherited ? <div aria-label={`${label}继承值，只读`} className="rounded-lg border border-border bg-muted/30 p-3 text-sm leading-relaxed"><InheritedValue value={value} label={label} /></div> : children}
  </div>;
}
