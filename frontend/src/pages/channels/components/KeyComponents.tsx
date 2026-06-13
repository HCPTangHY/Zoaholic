import { ClipboardPaste, LogIn, Play, ToggleRight, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';

import type { ApiKeyObj, BalanceResult } from '../types';
import {
  buildRoundRectPath,
  buildRowQuota,
  buildRowQuotaSlotData,
  formatCountdown,
  formatRackKeyLabel,
  hasUiSlot,
  serializeSlotValue,
  withRackCompactBalanceFallback,
  getUiSlotScript,
  getUiSlotValue,
  uiSlotCache,
} from '../utils';
import { QuotaRings } from './QuotaComponents';

// 修改原因：Channels.tsx 拆分后，Key 行和机房卡片需要成为独立展示模块。
// 修改方式：沿用原组件实现，只把依赖改为从 types、utils 和 QuotaComponents 导入。
// 目的：让编辑抽屉能够复用完整 Key 交互，并保持 ui_slots 全局访问方式不变。
// ========== DeferredInput ==========
// 本地 state 暂存输入，blur/Enter 时才写回外部，避免 parse+trim 吞空格
export function DeferredInput({ value, onCommit, ...props }: Omit<React.InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'onBlur' | 'onKeyDown' | 'value'> & { value: string; onCommit: (v: string) => void }) {
  const [local, setLocal] = useState(value);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current !== document.activeElement) setLocal(value); }, [value]);
  return <input ref={ref} {...props} type="text" value={local} onChange={e => setLocal(e.target.value)} onBlur={() => onCommit(local)} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); onCommit(local); (e.target as HTMLInputElement).blur(); } }} />;
}

// 修改原因：Key 备注遮罩原先使用固定 30% 宽度，短备注会浪费输入空间，长备注又会显示不全。
// 修改方式：把备注覆盖层和 Key 输入层封装到独立组件中，用 ref 与 useLayoutEffect 测量真实渲染宽度，并直接写入 DOM mask 样式，避免通过 state 触发重绘闪烁。
// 目的：让 Key 输入内容的透明区域随备注文字宽度变化，同时保留右侧标签渐隐和无备注时的旧 60% 标签遮罩。
export function KeyLabelOverlay({ label, hasTag, isFocused, children }: { label?: string; hasTag: boolean; isFocused: boolean; children: React.ReactNode }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const keyInputMaskRef = useRef<HTMLDivElement | null>(null);
  const labelSpanRef = useRef<HTMLSpanElement | null>(null);

  const setMaskImage = useCallback((el: HTMLElement, mask: string) => {
    el.style.maskImage = mask;
    el.style.setProperty('-webkit-mask-image', mask);
  }, []);

  const clearMaskImage = useCallback((el: HTMLElement) => {
    el.style.maskImage = '';
    el.style.removeProperty('-webkit-mask-image');
  }, []);

  const applyMasks = useCallback(() => {
    const keyInputMaskEl = keyInputMaskRef.current;
    if (!keyInputMaskEl) return;

    if (isFocused) {
      clearMaskImage(keyInputMaskEl);
      if (labelSpanRef.current) clearMaskImage(labelSpanRef.current);
      return;
    }

    const labelSpanEl = labelSpanRef.current;
    const containerEl = containerRef.current;
    if (label && labelSpanEl && containerEl && containerEl.clientWidth > 0) {
      const labelWidth = labelSpanEl.scrollWidth;
      const containerWidth = containerEl.clientWidth;
      const labelPct = Math.min(95, (labelWidth / containerWidth) * 100 + 5);
      const keyMask = hasTag
        ? `linear-gradient(to right, transparent 0%, transparent ${labelPct - 5}%, black ${labelPct + 15}%, black ${Math.max(65, labelPct + 15)}%, transparent 100%)`
        : `linear-gradient(to right, transparent 0%, transparent ${labelPct - 5}%, black ${labelPct + 15}%, black 100%)`;
      setMaskImage(keyInputMaskEl, keyMask);
      // label 不加 mask — 尽量完整显示，超出容器时靠 overflow-hidden 自然截断
      clearMaskImage(labelSpanEl);
      return;
    }

    if (labelSpanEl) clearMaskImage(labelSpanEl);
    if (hasTag) {
      setMaskImage(keyInputMaskEl, 'linear-gradient(to right, black 0%, black 60%, transparent 100%)');
    } else {
      clearMaskImage(keyInputMaskEl);
    }
  }, [clearMaskImage, hasTag, isFocused, label, setMaskImage]);

  useLayoutEffect(() => {
    applyMasks();

    const containerEl = containerRef.current;
    if (!containerEl || typeof ResizeObserver === 'undefined') return;

    const resizeObserver = new ResizeObserver(() => applyMasks());
    resizeObserver.observe(containerEl);
    if (labelSpanRef.current) resizeObserver.observe(labelSpanRef.current);

    return () => resizeObserver.disconnect();
  }, [applyMasks]);

  const bindLabelSpan = useCallback((el: HTMLSpanElement | null) => {
    labelSpanRef.current = el;
    applyMasks();
  }, [applyMasks]);

  const bindKeyInputMask = useCallback((el: HTMLDivElement | null) => {
    keyInputMaskRef.current = el;
    applyMasks();
  }, [applyMasks]);

  return (
    <div ref={containerRef} className="flex-1 min-w-0 relative z-[2]">
      {label && !isFocused && (
        <div className="absolute inset-y-0 left-0 right-0 flex items-center pointer-events-none z-[3] select-none overflow-hidden">
          <span
            ref={bindLabelSpan}
            className="text-sm leading-5 font-mono font-semibold text-amber-600 dark:text-amber-400 whitespace-nowrap"
          >
            {label}
          </span>
        </div>
      )}

      <div ref={bindKeyInputMask}>{children}</div>
    </div>
  );
}

export const UiSlot = ({ engine, slot, data, context, className, element = 'span', fallbackText, enabledPlugins }: { engine: string; slot: string; data: any; context?: Record<string, any>; className?: string; element?: 'span' | 'div'; fallbackText?: string; enabledPlugins?: string[] }) => {
  const ref = useRef<HTMLElement | null>(null);
  const [loaded, setLoaded] = useState(false);
  const dataRef = useRef(data);
  const contextRef = useRef(context);
  dataRef.current = data;
  contextRef.current = context;
  const dataKey = useMemo(() => serializeSlotValue(data), [data]);
  const contextKey = useMemo(() => serializeSlotValue(context), [context]);
  // 修改原因：enabledPlugins 数组引用可能随表单更新重建，直接放入 effect 依赖会导致无意义重跑。
  // 修改方式：和 data/context 一样序列化为内容签名，只在插件列表内容变化时重新检查 requires_plugin。
  // 目的：让插件开关变化能正确影响 UiSlot，同时避免普通输入聚焦造成重复加载。
  const enabledPluginsKey = useMemo(() => serializeSlotValue(enabledPlugins || []), [enabledPlugins]);

  const bindRef = useCallback((node: HTMLElement | null) => {
    ref.current = node;
  }, []);

  useEffect(() => {
    if (!ref.current) return;
    const el = ref.current;
    const cacheKey = `${engine}:${slot}`;

    const run = async () => {
      try {
        // 修改原因：同一渠道的同一插槽会被多个 Key 行重复使用，重复 import 会浪费资源并增加闪烁概率。
        // 修改方式：按 `${engine}:${slot}` 缓存模块函数，命中时直接复用已加载的 render 函数。
        // 目的：保持列表渲染轻量，同时支持同一渠道注册多个彼此独立的插槽。
        if (fallbackText !== undefined) el.textContent = fallbackText;
        // 修改原因：后端可能返回 {script, requires_plugin}，且同一个 engine 的不同 provider 插件开关不同。
        // 修改方式：先按 enabledPlugins 提取可执行脚本，再决定是否使用缓存或动态 import。
        // 目的：避免未启用插件的 provider 误用已经缓存的同 engine slot 脚本。
        const slotValue = getUiSlotValue(engine, slot);
        const jsSrc = getUiSlotScript(engine, slot, enabledPlugins);
        if (!jsSrc) {
          if (!slotValue || typeof slotValue === 'string' || !slotValue.requires_plugin) uiSlotCache[cacheKey] = null;
          setLoaded(true);
          return;
        }
        if (cacheKey in uiSlotCache) {
          const fn = uiSlotCache[cacheKey];
          // 修改原因：新插槽脚本通过 ctx.context?.mode 区分完整行和机房卡片，但旧脚本仍直接读取 ctx.account 等扁平字段。
          // 修改方式：调用脚本时同时保留扁平展开字段，并额外传入原始 context 对象。
          // 目的：让 mode 分支生效，同时保持已有渠道和插件脚本兼容。
          if (fn) fn({ el, data: dataRef.current, ...(contextRef.current ?? {}), context: contextRef.current });
          setLoaded(true);
          return;
        }

        // 修改原因：插槽脚本来自渠道元数据，前端只负责按 slot 名加载，不应固定读取 quota_display。
        // 修改方式：从字符串 slot 或对象 slot.script 取内联 JS，再通过 Blob URL dynamic import 加载默认导出。
        // 目的：新增 key_border、key_background、balance_summary 等插槽时不再修改加载器。

        const blob = new Blob([jsSrc], { type: 'application/javascript' });
        const url = URL.createObjectURL(blob);
        try {
          const mod = await import(/* @vite-ignore */ url);
          const fn = mod.default || mod;
          uiSlotCache[cacheKey] = typeof fn === 'function' ? fn : null;
          // 修改原因：首次动态加载插槽脚本时也必须提供 nested context，否则新加载路径和缓存命中路径行为不一致。
          // 修改方式：与缓存命中分支一样传入 data、展开后的 context 字段，以及完整 context 对象。
          // 目的：保证 quota_display、key_background 和 key_border 都能稳定读取 ctx.context?.mode。
          if (uiSlotCache[cacheKey]) uiSlotCache[cacheKey]!({ el, data: dataRef.current, ...(contextRef.current ?? {}), context: contextRef.current });
        } finally {
          URL.revokeObjectURL(url);
        }
        setLoaded(true);
      } catch (e) {
        console.warn(`[UiSlot] Failed to load UI slot ${slot} for ${engine}:`, e);
        uiSlotCache[cacheKey] = null;
        // 修改原因：插槽脚本失败时继续显示旧 DOM 会误导用户，以为渠道仍在正常渲染。
        // 修改方式：有默认文本的插槽恢复默认文本，没有默认文本的插槽清空内容和 title。
        // 目的：让失败插槽安全静默降级，并把排查信息留在控制台。
        el.textContent = fallbackText ?? '';
        el.removeAttribute('title');
        setLoaded(true);
      }
    };

    run();
  }, [engine, slot, dataKey, contextKey, fallbackText, enabledPluginsKey]);

  if (element === 'div') {
    return <div ref={bindRef as React.RefCallback<HTMLDivElement>} data-loaded={loaded ? 'true' : 'false'} className={className} />;
  }
  return <span ref={bindRef as React.RefCallback<HTMLSpanElement>} data-loaded={loaded ? 'true' : 'false'} className={className} />;
};

// ── 冷却中 Key 行组件（SVG 边框进度） ──
export function CoolingKeyRow({ idx, keyObj, remainSec, totalDuration, focused, onFocus, onBlur, onRecover, onToggle, onTest, onDelete, onLabelChange, alwaysShowRecover }: {
  idx: number; keyObj: { key: string; disabled: boolean; label?: string }; remainSec: number; totalDuration: number;
  focused: boolean;
  onFocus: () => void; onBlur: () => void;
  onRecover: () => void; onToggle: () => void; onTest: () => void; onDelete: () => void; onLabelChange?: (label: string) => void;
  alwaysShowRecover?: boolean;
}) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [svgViewBox, setSvgViewBox] = useState('');
  const [pathD, setPathD] = useState('');

  // 计算进度百分比
  const progress = totalDuration > 0 ? Math.max(0, Math.min(100, (remainSec / totalDuration) * 100)) : 0;

  // 初始化 & resize 时更新 SVG path
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;
    const update = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      setSvgViewBox(`0 0 ${w} ${h}`);
      setPathD(buildRoundRectPath(1, 1, w - 2, h - 2, 7));
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // SVG stroke-dasharray / dashoffset
  const dasharray = progress > 0 ? `${progress} 100` : '0 100';
  const dashoffset = progress > 0 ? `${-(100 - progress)}` : '0';

  return (
    <div ref={wrapperRef} className="relative rounded-lg" style={{ isolation: 'isolate' }}>
      {/* SVG 边框进度 */}
      {!focused && pathD && (
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none z-[1]"
          viewBox={svgViewBox}
          style={{ overflow: 'visible' }}
        >
          <path
            d={pathD}
            pathLength={100}
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            className="text-red-500"
            style={{ strokeDasharray: dasharray, strokeDashoffset: dashoffset, transition: 'stroke-dasharray 1s linear, stroke-dashoffset 1s linear' }}
          />
        </svg>
      )}
      {/* 内容 */}
      <div className={`relative flex items-center gap-2 px-3 py-2 rounded-lg border-2 transition-colors ${focused ? 'border-blue-500 bg-muted/50' : 'border-border bg-background dark:bg-card'}`}>
        <span className="text-xs text-muted-foreground w-4 text-right relative z-[2]">{idx + 1}</span>
        {keyObj.label && !focused && (
          <span className="text-sm font-mono font-semibold text-amber-600 dark:text-amber-400 truncate max-w-[30%] flex-shrink-0 relative z-[2]">{keyObj.label}</span>
        )}
        <div className="flex-1 min-w-0 relative z-[2]">
          <input
            type="text" value={keyObj.key || ''} readOnly placeholder="sk-..."
            onFocus={onFocus} onBlur={e => { if (!wrapperRef.current?.contains(e.relatedTarget as Node)) onBlur(); }}
            className={`w-full bg-transparent border-none text-sm font-mono outline-none ${focused ? 'text-foreground' : 'text-red-400 dark:text-red-300 line-through decoration-red-500/40'}`}
          />
          {/* 倒计时叠加 */}
          {!focused && (
            <span className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 text-[11px] font-semibold font-mono text-red-500 bg-background/85 dark:bg-card/85 rounded px-2 py-0.5 pointer-events-none z-[3]">
              {formatCountdown(remainSec)}
            </span>
          )}
        </div>
        {(!focused || alwaysShowRecover) && (
          <button onClick={onRecover} className="text-[11px] px-2 py-0.5 rounded border border-emerald-500/50 bg-emerald-500/20 text-emerald-400 font-medium hover:bg-emerald-500/30 hover:border-emerald-400 cursor-pointer flex-shrink-0 relative z-[2] transition-colors">恢复</button>
        )}
        <div className="actions flex items-center gap-1 flex-shrink-0 relative z-[2]">
          <button onClick={onToggle} className="text-muted-foreground" title="禁用"><ToggleRight className="w-5 h-5" /></button>
          <button onClick={onTest} disabled={!keyObj.key.trim()} className="text-blue-600 dark:text-blue-400 disabled:opacity-50"><Play className="w-4 h-4" /></button>
          <button onClick={onDelete} className="text-red-500 hover:text-red-400 ml-1"><Trash2 className="w-4 h-4" /></button>
        </div>
        {/* Label 编辑：聚焦时在行底部展开 */}
        {focused && onLabelChange && (
          <div className="absolute left-0 right-0 -bottom-6 flex items-center gap-1 z-[5]">
            <span className="text-[10px] text-muted-foreground/50 pl-8">备注:</span>
            <input
              type="text"
              value={keyObj.label || ''}
              onChange={e => onLabelChange(e.target.value)}
              onFocus={onFocus}
              placeholder="点击添加备注"
              className="flex-1 bg-background/80 backdrop-blur-sm border border-border/50 rounded px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400 font-mono outline-none focus:border-amber-500/50 placeholder:text-muted-foreground/30"
            />
          </div>
        )}
      </div>
    </div>
  );
}



// 修改原因：Key 数量较多时，完整行模式会占用过多纵向空间，无法快速查看大量 Key 的状态。
// 修改方式：为机房模式集中定义固定尺寸卡片、350° 缺口圆环、tier 药丸和状态灯等纯展示 helper。
// 目的：在不改动现有完整行渲染代码的前提下，让 >= 10 个 Key 的渠道自动获得紧凑视图。

export function RackGrid({ children, onClick }: { children: React.ReactNode; onClick?: React.MouseEventHandler<HTMLDivElement> }) {
  // 修改原因：机房模式需要让卡片横向排列并自动换行，而不能继续使用完整行的纵向间距布局。
  // 修改方式：用 flex flex-wrap 和固定 gap 包裹 RackCard，外层仍放在原滚动容器内。
  // 目的：在侧边编辑抽屉中以紧凑网格展示大量 Key。
  // 修改原因：选中卡片展开成完整行后，用户需要点击机房网格空白处取消选中。
  // 修改方式：让 RackGrid 接收并透传 onClick，只在调用方判断 target 是否为网格本身。
  // 目的：避免点击卡片或完整行内部控件时误取消编辑状态。
  return <div className="flex flex-wrap gap-1.5 pb-1" onClick={onClick}>{children}</div>;
}

export function RackCoolingBorder({ remainSec, totalDuration }: { remainSec: number; totalDuration: number }) {
  // 在卡片圆角矩形边框上画红色冷却进度条 + 倒计时叠加层
  const ref = useRef<HTMLDivElement>(null);
  const [svgViewBox, setSvgViewBox] = useState('');
  const [pathD, setPathD] = useState('');
  const progress = totalDuration > 0 ? Math.max(0, Math.min(100, (remainSec / totalDuration) * 100)) : 0;
  const safeRemain = Math.max(0, Math.ceil(remainSec));

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      if (w > 0 && h > 0) {
        setSvgViewBox(`0 0 ${w} ${h}`);
        setPathD(buildRoundRectPath(1, 1, w - 2, h - 2, 7));
      }
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const dasharray = progress > 0 ? `${progress} 100` : '0 100';
  const dashoffset = progress > 0 ? `${-(100 - progress)}` : '0';

  return (
    <div ref={ref} className="absolute inset-0 z-[5] pointer-events-none">
      {pathD && (
        <svg className="absolute inset-0 w-full h-full" viewBox={svgViewBox} style={{ overflow: 'visible' }}>
          <path
            d={pathD}
            pathLength={100}
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            className="text-red-500"
            style={{ strokeDasharray: dasharray, strokeDashoffset: dashoffset, transition: 'stroke-dasharray 1s linear, stroke-dashoffset 1s linear' }}
          />
        </svg>
      )}
      <div className="absolute inset-x-0 bottom-[18px] flex items-center justify-center">
        <span className="text-[9px] font-bold font-mono text-red-500 bg-background/80 dark:bg-card/80 rounded px-1 py-0.5">
          {formatCountdown(safeRemain)}
        </span>
      </div>
    </div>
  );
}

export function RackCard({ idx, keyObj, providerName, engine, enabledPlugins, runtimeKeyStatus, localCountdowns, balanceResults, oauthAccounts, isOAuthEngine, onFocus, onImport, onLogin }: {
  idx: number;
  keyObj: ApiKeyObj;
  providerName: string;
  engine: string;
  // 修改原因：机房卡片按 engine 判断 slot 时拿不到 formData，必须由调用处把当前 provider 启用插件列表传入。
  // 修改方式：在 RackCard props 中增加 enabledPlugins，并传给 hasUiSlot 与 UiSlot。
  // 目的：让 oai_tier 这类 requires_plugin 的 quota_display 在紧凑卡片中按当前 provider 正确生效。
  enabledPlugins: string[];
  runtimeKeyStatus: Record<string, { auto_disabled?: { key: string; remaining_seconds: number; duration?: number; reason?: string }[]; cooling?: any[] }>;
  localCountdowns: Record<string, Record<string, { remaining: number; duration: number }>>;
  balanceResults: Record<string, BalanceResult>;
  oauthAccounts: Record<string, any>;
  isOAuthEngine: boolean;
  onFocus: () => void;
  onImport: () => void;
  onLogin: () => void;
}) {
  // 修改原因：机房卡片需要复用完整行的数据来源，但完整行渲染分支不能被改写。
  // 修改方式：在独立 RackCard 中重新计算运行时禁用、冷却、余额、OAuth quota 和插槽状态。
  // 目的：让紧凑视图与完整行保持相同状态语义和操作回调。
  const rtDisabled = runtimeKeyStatus[providerName]?.auto_disabled || [];
  const rtEntry = !keyObj.disabled ? rtDisabled.find((d: any) => d.key === keyObj.key) : undefined;
  const isPermanent = !!rtEntry && rtEntry.remaining_seconds < 0;
  const isCooling = !!rtEntry && !isPermanent && rtEntry.remaining_seconds > 0;
  const countdown = localCountdowns[providerName]?.[keyObj.key];
  const remainSec = countdown?.remaining ?? (rtEntry?.remaining_seconds || 0);
  // 修改原因：冷却圆环需要用剩余时间除以总冷却时长，不能再只依赖卡片边框表达冷却状态。
  // 修改方式：沿用完整行 CoolingKeyRow 的数据来源，优先取本地倒计时 duration，其次取运行时 duration，最后用 remainSec 兜底。
  // 目的：让机房卡片的冷却弧线和完整行倒计时保持同一套时间语义。
  const totalDuration = countdown?.duration ?? rtEntry?.duration ?? remainSec;
  const isGrayed = keyObj.disabled || isPermanent;
  const status = isGrayed ? 'disabled' : isCooling ? 'cooling' : 'active';
  const bal = balanceResults[keyObj.key];
  const oauthAccount = oauthAccounts[keyObj.key];
  // 修改原因：机房卡片不应再按 OAuth 与普通 Key 选择两套圆环数据来源，也不应再渲染通用 badge 标签。
  // 修改方式：用 buildRowQuota 统一产出 gauges，并为旧 amount fallback 单独压缩圆心显示文本。
  // 目的：卡片渲染只消费 QuotaRings；tier/plan 等标签交给 quota_display 插槽。
  const rowQuota = buildRowQuota(bal, oauthAccount, isOAuthEngine);
  const rackGauges = withRackCompactBalanceFallback(rowQuota.gauges, bal);
  const rowQuotaHasValues = rowQuota.gauges.length > 0;
  const slotData = buildRowQuotaSlotData(bal, oauthAccount, rowQuota);
  // 修改原因：部分渠道插槽只读取账号或 balance 上下文，即使标准 quota 数字暂时没有返回也应挂载。
  // 修改方式：按 slotData、OAuth 账号和统一 rowQuota 是否有内容综合判断，不再使用 OAuth 展示分支。
  // 目的：让 key_background、quota_display 在机房模式下不因标准双额度缺失而失效。
  const slotPayloadAvailable = Boolean(slotData || oauthAccount || rowQuotaHasValues);
  const slotContext = { account: oauthAccount, keyObj, balance: bal };
  const hasKeyBorderSlot = hasUiSlot(engine, 'key_border', enabledPlugins);
  const hasKeyBackgroundSlot = hasUiSlot(engine, 'key_background', enabledPlugins);
  const hasQuotaDisplaySlot = hasUiSlot(engine, 'quota_display', enabledPlugins);
  const isOAuthEmpty = isOAuthEngine && !keyObj.key.trim();
  const labelText = formatRackKeyLabel(keyObj);
  const title = `${idx + 1}. ${keyObj.label || keyObj.key || '空账号'}${isCooling ? ` · 冷却 ${formatCountdown(remainSec)}` : ''}`;
  // 修改原因：冷却状态已经由圆环倒计时表达，继续显示黄色状态灯会让小卡片颜色过于集中。
  // 修改方式：状态灯只保留启用和禁用两类静态状态，冷却时不再渲染右上角黄色灯。
  // 目的：让用户主要通过中心倒计时和圆环进度识别冷却状态。
  const statusClass = isGrayed
    ? 'bg-red-500 shadow-[0_0_10px_rgba(239,68,68,0.6)]'
    : 'bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.65)]';
  // 修改原因：冷却卡片的黄色硬边框会和倒计时圆环重复表达，造成小卡片视觉拥挤。
  // 修改方式：所有机房卡片统一使用普通边框，冷却进度只由圆环承担。
  // 目的：去掉旧的黄色硬编码冷却边框，保留状态表达但降低干扰。
  const cardBorder = 'border-border';
  // 修改原因：机房卡片原先复用 muted 半透明背景，浅色主题下与滚动区域层次不够清楚。
  // 修改方式：浅色主题使用 card 表面，深色主题继续使用 muted 半透明表面以维持原暗色观感。
  // 目的：让大量机房卡片在两种主题下都有明确边界和稳定背景。
  const cardSurfaceClass = 'bg-card/90 dark:bg-muted/50';
  // 修改原因：空 OAuth 卡片圆环上的导入和登录操作层原先只使用深色按钮和遮罩。
  // 修改方式：把遮罩、导入按钮和登录按钮抽成包含 light/dark 变体的类名常量。
  // 目的：让浅色主题下的空账号操作入口保持可读，同时不改变深色主题视觉。
  const emptyOAuthOverlayClass = 'absolute inset-0 z-[6] flex flex-col items-center justify-center gap-1 rounded-full bg-white/90 dark:bg-[#0f0f12]/85';
  const emptyOAuthImportButtonClass = 'flex items-center gap-1 rounded border border-slate-300 bg-white/95 px-1.5 py-0.5 text-[9px] text-slate-700 hover:bg-slate-100 dark:border-[#1e1e22] dark:bg-slate-800/90 dark:text-slate-100 dark:hover:bg-slate-700';
  const emptyOAuthLoginButtonClass = 'flex items-center gap-1 rounded border border-blue-500/40 bg-blue-500/15 px-1.5 py-0.5 text-[9px] text-blue-700 hover:bg-blue-500/20 dark:text-blue-200 dark:hover:bg-blue-500/25';

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onFocus();
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      title={title}
      onClick={onFocus}
      onKeyDown={handleKeyDown}
      className={`relative h-[92px] overflow-hidden rounded-lg border ${cardSurfaceClass} text-foreground transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-blue-500/50 ${cardBorder} ${isGrayed ? 'opacity-50' : 'hover:border-muted-foreground/30'}`}
      style={{ width: 'calc((100% - 5 * 6px) / 6)', isolation: 'isolate' }}
    >
      {/* 修改原因：key_border 和 key_background 插槽在完整行模式已有挂载点，机房卡片也必须保留同等扩展能力；渠道脚本还需要知道当前是小卡片布局。
          修改方式：把 key_background 放在卡片背景层，把 key_border 放在外层绝对覆盖层，并透传 quota/account/balance 上下文和 mode: 'rack'。
          目的：保证渠道自定义边框、背景和额度装饰在紧凑视图中不丢失，同时避免完整行样式进入机房卡片。 */}
      {hasKeyBackgroundSlot && slotPayloadAvailable && (
        <UiSlot engine={engine} slot="key_background" data={slotData} context={{ ...slotContext, mode: 'rack' }} element="div" className="absolute inset-0 z-0 rounded-xl pointer-events-none" enabledPlugins={enabledPlugins} />
      )}
      {hasKeyBorderSlot && slotPayloadAvailable && (
        <UiSlot engine={engine} slot="key_border" data={slotData} context={{ ...slotContext, mode: 'rack' }} element="div" className="absolute inset-0 z-[3] pointer-events-none" enabledPlugins={enabledPlugins} />
      )}
      {isCooling && <RackCoolingBorder remainSec={remainSec} totalDuration={totalDuration} />}
      <span className={`absolute right-1.5 top-1.5 z-[4] h-2 w-2 rounded-full ring-2 ring-card ${statusClass}`} title={status} />
      <div className="relative z-[2] flex h-full flex-col items-center px-1 pb-1.5 pt-2">
        <div className="relative flex h-12 w-12 items-center justify-center">
          {/* 修改原因：机房卡片不能再按 OAuth 与普通 Key 分支选择 RackOAuthRings 或 RackSingleRing。
              修改方式：统一把 rowQuota.gauges 交给 QuotaRings，由组件内部按数量决定空环、单环或双环。
              目的：新增 quota 类型时不需要继续修改卡片渲染分支。 */}
          <QuotaRings gauges={rackGauges} hideText={hasQuotaDisplaySlot && slotPayloadAvailable} />
          {/* 修改原因：冷却圆环中心必须显示倒计时，quota_display 再覆盖上去会遮挡剩余秒数；OAuth 渠道完整行标签在圆环中心会溢出。
              修改方式：quota_display 仍挂载在非冷却卡片的圆环中心，并透传 mode: 'rack'，外层容器增加 overflow-hidden 和 max-w-full。
              目的：同时保证普通额度插槽可用、冷却状态倒计时清晰可见，并限制机房卡片中心文字宽度。 */}
          {hasQuotaDisplaySlot && slotPayloadAvailable && (
            <div className="absolute inset-0 z-[5] flex items-center justify-center overflow-hidden max-w-full">
              <UiSlot engine={engine} slot="quota_display" data={slotData} context={{ ...slotContext, mode: 'rack' }} element="div" className="flex items-center justify-center text-[10px]" enabledPlugins={enabledPlugins} />
            </div>
          )}

          {isOAuthEmpty && (
            <div className={emptyOAuthOverlayClass}>
              <button
                type="button"
                onClick={(event) => { event.stopPropagation(); onImport(); }}
                className={emptyOAuthImportButtonClass}
                title="粘贴 Refresh Token"
              >
                <ClipboardPaste className="h-2.5 w-2.5" /> 导入
              </button>
              <button
                type="button"
                onClick={(event) => { event.stopPropagation(); onLogin(); }}
                className={emptyOAuthLoginButtonClass}
                title="浏览器登录"
              >
                <LogIn className="h-2.5 w-2.5" /> 登录
              </button>
            </div>
          )}
        </div>
        <div className="mt-auto w-full text-center">

          <div className={`truncate text-[10px] font-semibold font-mono ${isCooling ? 'text-red-400 dark:text-red-300 line-through decoration-red-500/40' : 'text-foreground'}`} title={keyObj.label || keyObj.key || labelText}>{labelText}</div>
        </div>
      </div>
      {/* 修改原因：卡片选中后的编辑入口已改为展开完整行，旧的底部小按钮弹层太小且不能编辑 Key。
          修改方式：删除 focused popover，让所有操作按钮只出现在展开后的完整行中。
          目的：避免同一张卡片同时出现两套操作入口。 */}
    </div>
  );
}
