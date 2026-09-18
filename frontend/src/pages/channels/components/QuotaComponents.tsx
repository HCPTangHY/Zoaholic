import { useEffect, useRef, useState } from 'react';

import type { OAuthQuota, QuotaGauge } from '../types';
import {
  RACK_ARC_LENGTH,
  RACK_GAP_LENGTH,
  RACK_RING_PATH_LENGTH,
  buildBottomHalfPath,
  buildTopHalfPath,
  clampRackPercent,
  getQuotaGaugeStrokeColor,
  getQuotaRingText,
  getQuotaRingTextClass,
  getRackUsageGradientColor,
} from '../utils';

// 修改原因：Channels.tsx 拆分后，额度展示组件需要独立承载原有 SVG 渲染逻辑。
// 修改方式：保持原 JSX 和计算逻辑不变，只补齐跨文件 import 并导出组件。
// 目的：让 Key 行、机房卡片和兼容调用点继续使用同一套额度视觉。
export function QuotaBorderOverlay({ quotaInner, quotaOuter, label }: {
  quotaInner?: number | null; quotaOuter?: number | null; label?: string | null;
}) {
  const selfRef = useRef<HTMLDivElement>(null);
  const [svgViewBox, setSvgViewBox] = useState('');
  const [topPath, setTopPath] = useState('');
  const [bottomPath, setBottomPath] = useState('');

  useEffect(() => {
    const el = selfRef.current;
    if (!el) return;
    const update = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      if (w > 0 && h > 0) {
        setSvgViewBox(`0 0 ${w} ${h}`);
        setTopPath(buildTopHalfPath(1, 1, w - 2, h - 2, 7));
        setBottomPath(buildBottomHalfPath(1, 1, w - 2, h - 2, 7));
      }
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const qInner = quotaInner ?? 0;
  const qOuter = quotaOuter ?? 0;
  return (
    <div ref={selfRef} className="absolute inset-0 pointer-events-none z-[1]" style={{ overflow: 'visible' }}>
      {svgViewBox && (
        <svg className="absolute inset-0 w-full h-full" viewBox={svgViewBox} style={{ overflow: 'visible' }}>
          <title>{`${label ? label + ' · ' : ''}inner: ${quotaInner ?? '?'}% \u00b7 outer: ${quotaOuter ?? '?'}%`}</title>
          {quotaInner != null && topPath && (
            <path d={topPath} pathLength={100} fill="none" stroke="#3b82f6" strokeWidth={2} strokeLinecap="round"
              style={{ strokeDasharray: `${qInner} 100`, strokeDashoffset: 0, transition: 'stroke-dasharray 0.5s ease' }} />
          )}
          {quotaOuter != null && bottomPath && (
            <path d={bottomPath} pathLength={100} fill="none" stroke="#8b5cf6" strokeWidth={2} strokeLinecap="round"
              style={{ strokeDasharray: `${qOuter} 100`, strokeDashoffset: 0, transition: 'stroke-dasharray 0.5s ease' }} />
          )}
        </svg>
      )}
    </div>
  );
}

// 兼容 QuotaArcs 调用点 — 用最小百分比的文字 tag
export const QuotaArcs = ({ quotaInner, quotaOuter }: { quotaInner?: number; quotaOuter?: number }) => {
  if (quotaInner == null && quotaOuter == null) return null;
  const pct = Math.min(quotaInner ?? 100, quotaOuter ?? 100);
  const color = pct > 50 ? 'bg-emerald-500/15 text-emerald-500' : pct > 20 ? 'bg-amber-500/15 text-amber-600' : 'bg-red-500/15 text-red-500';
  return (
    <span
      className={`flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] cursor-default ${color}`}
      title={`inner: ${quotaInner ?? '?'}% · outer: ${quotaOuter ?? '?'}%`}
    >
      {Math.round(pct)}%
    </span>
  );
};

export function RackRingCircle({ radius, strokeWidth, percent, color, trackOpacity = 0.72 }: {
  radius: number;
  strokeWidth: number;
  percent: number | null;
  color: string;
  trackOpacity?: number;
}) {
  // 修改原因：单环和 OAuth 双环都需要相同的 350° 缺口圆环，重复写 SVG 容易让缺口角度不一致。
  // 修改方式：统一用 circle、pathLength 和 stroke-dasharray 绘制轨道与填充，并整体旋转 5° 让缺口居中在右侧。
  // 目的：确保普通 Key 和 OAuth Key 的机房圆环视觉一致。
  // 修改原因：圆环轨道原先使用过深的浅色主题颜色，导致浅色主题下几乎看不见。
  // 修改方式：轨道 circle 改用 Tailwind stroke 主题类，浅色主题使用 slate-300，深色主题保留原 #1a1a2e。
  // 目的：让同一个 SVG 轨道在浅色和深色主题中都有稳定对比度。
  const fillLength = percent == null ? 0 : (percent / 100) * RACK_ARC_LENGTH;
  return (
    <g style={{ transform: 'rotate(5deg)', transformOrigin: '32px 32px' }}>
      <circle
        cx="32"
        cy="32"
        r={radius}
        pathLength={RACK_RING_PATH_LENGTH}
        fill="none"
        className="stroke-slate-300 dark:stroke-[#1a1a2e]"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeOpacity={trackOpacity}
        strokeDasharray={`${RACK_ARC_LENGTH} ${RACK_GAP_LENGTH}`}
      />
      {percent != null && fillLength > 0 && (
        <circle
          cx="32"
          cy="32"
          r={radius}
          pathLength={RACK_RING_PATH_LENGTH}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={`${fillLength} ${RACK_RING_PATH_LENGTH - fillLength}`}
          className="transition-all duration-500"
        />
      )}
    </g>
  );
}

export function RackSingleRing({ percent, textClassName, label }: { percent: number | null; textClassName: string; label?: string | null }) {
  const pct = clampRackPercent(percent);
  const stroke = pct == null ? '#334155' : getRackUsageGradientColor(pct);
  // 有金额标签时显示金额，否则显示百分比
  const displayText = label || (pct == null ? '—' : `${Math.round(pct)}%`);
  const textSize = label && label.length > 5 ? 'text-[8px]' : 'text-[11px]';
  return (
    <div className="relative flex h-12 w-12 items-center justify-center">
      <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
        <RackRingCircle radius={25} strokeWidth={6} percent={pct} color={stroke} trackOpacity={pct == null ? 0.45 : 0.8} />
      </svg>
      <span className={`absolute inset-0 flex items-center justify-center ${textSize} font-bold font-mono ${textClassName}`}>
        {displayText}
      </span>
    </div>
  );
}

export function RackOAuthRings({ quota, hideText }: { quota: OAuthQuota | null; hideText?: boolean }) {
  const quotaInner = clampRackPercent(quota?.quota_inner);
  const quotaOuter = clampRackPercent(quota?.quota_outer);
  return (
    <div className="relative flex h-12 w-12 items-center justify-center">
      <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
        <RackRingCircle radius={26} strokeWidth={5} percent={quotaInner} color="#60a5fa" trackOpacity={quotaInner == null ? 0.42 : 0.74} />
        <RackRingCircle radius={18} strokeWidth={5} percent={quotaOuter} color="#a78bfa" trackOpacity={quotaOuter == null ? 0.35 : 0.68} />
      </svg>
      {!hideText && (
        <span className="absolute inset-0 flex items-center justify-center text-[11px] font-bold font-mono text-sky-700 dark:text-sky-100">
          {quotaInner == null ? '—' : `${Math.round(quotaInner)}%`}
        </span>
      )}
    </div>
  );
}

export function QuotaRings({ gauges, hideText, centerLabel }: { gauges: QuotaGauge[]; hideText?: boolean; centerLabel?: string | null }) {
  // 修改原因：RackSingleRing 与 RackOAuthRings 的调用点原先按 OAuth 与普通 Key 分支选择，后续 quota 类型会继续增加。
  // 修改方式：按 gauges 数量决定空态、单环或双环，三项以上沿用前两个 gauge，并在内部复用原有圆环样式参数。
  // 目的：让机房卡片和完整 Key 行都走同一个圆环渲染路径。
  const visibleGauges = Array.isArray(gauges) ? gauges.filter(Boolean).slice(0, 2) : [];

  if (visibleGauges.length === 0) {
    return (
      <div className="relative flex h-12 w-12 items-center justify-center" title="暂无额度数据">
        <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
          <RackRingCircle radius={25} strokeWidth={6} percent={null} color="#334155" trackOpacity={0.45} />
        </svg>
        {!hideText && <span className="absolute inset-0 flex items-center justify-center text-[11px] font-bold font-mono text-muted-foreground">—</span>}
      </div>
    );
  }

  if (visibleGauges.length === 1) {
    const gauge = visibleGauges[0];
    const mode = gauge.display_mode || (gauge.percent != null ? 'percent' : 'quota');
    const displayText = getQuotaRingText(gauge, gauge.percent ?? null);
    const textSize = displayText.length > 5 ? 'text-[8px]' : 'text-[11px]';

    if (mode === 'quota') {
      // quota 模式：available 对比 100 画弧线 + 金额文字
      const pct = clampRackPercent(gauge.percent);
      const stroke = getQuotaGaugeStrokeColor(gauge, pct, '');
      return (
        <div className="relative flex h-12 w-12 items-center justify-center" title={`余额: ${displayText}`}>
          <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
            <RackRingCircle radius={25} strokeWidth={6} percent={pct} color={stroke} trackOpacity={pct == null ? 0.45 : 0.8} />
          </svg>
          {!hideText && <span className={`absolute inset-0 flex items-center justify-center ${textSize} font-bold font-mono ${getQuotaRingTextClass(gauge, pct)}`}>{displayText}</span>}
        </div>
      );
    }

    // percent / amount 模式：正常弧线
    const pct = clampRackPercent(gauge.percent);
    const stroke = getQuotaGaugeStrokeColor(gauge, pct, '');
    return (
      <div className="relative flex h-12 w-12 items-center justify-center" title={`${gauge.label}: ${pct == null ? '未知' : `${pct.toFixed(1)}%`}`}>
        <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
          <RackRingCircle radius={25} strokeWidth={6} percent={pct} color={stroke} trackOpacity={pct == null ? 0.45 : 0.8} />
        </svg>
        {!hideText && (
          <span className={`absolute inset-0 flex items-center justify-center ${textSize} font-bold font-mono ${getQuotaRingTextClass(gauge, pct)}`}>
            {displayText}
          </span>
        )}
      </div>
    );
  }

  const quotaInner = clampRackPercent(visibleGauges[0]?.percent);
  const quotaOuter = clampRackPercent(visibleGauges[1]?.percent);
  const innerColor = getQuotaGaugeStrokeColor(visibleGauges[0], quotaInner, '#60a5fa');
  const outerColor = getQuotaGaugeStrokeColor(visibleGauges[1], quotaOuter, '#a78bfa');
  // 修改原因：plan 类型双环的圆心百分比在机房卡片上信息密度低，而层级标签没有其他常显位置。
  // 修改方式：centerLabel 存在时圆心改显层级文本，双窗口百分比仍保留在 title 悬浮提示和完整行 badge 中。
  // 目的：机房卡片一眼识别套餐层级，文字颜色继续跟随首个窗口的余量分档。
  const centerText = centerLabel || getQuotaRingText(visibleGauges[0], quotaInner);
  const centerSize = centerText.length > 5 ? 'text-[8px]' : 'text-[11px]';
  return (
    <div className="relative flex h-12 w-12 items-center justify-center" title={`${visibleGauges[0].label}: ${quotaInner ?? '?'}% · ${visibleGauges[1].label}: ${quotaOuter ?? '?'}%`}>
      <svg className="h-12 w-12" viewBox="0 0 64 64" aria-hidden="true">
        <RackRingCircle radius={26} strokeWidth={5} percent={quotaInner} color={innerColor} trackOpacity={quotaInner == null ? 0.42 : 0.74} />
        <RackRingCircle radius={18} strokeWidth={5} percent={quotaOuter} color={outerColor} trackOpacity={quotaOuter == null ? 0.35 : 0.68} />
      </svg>
      {!hideText && (
        <span className={`absolute inset-0 flex items-center justify-center ${centerSize} font-bold font-mono ${getQuotaRingTextClass(visibleGauges[0], quotaInner)}`}>
          {centerText}
        </span>
      )}
    </div>
  );
}
