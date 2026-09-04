/* ==========================================================================
 * FlowTimeline.tsx —— 执行流程时间轴（甘特图，完整执行路径视图）
 * --------------------------------------------------------------------------
 * 每步事件一行：左侧步骤信息（时间/状态图标/事件/模块），右侧按真实
 * 时间定位的耗时条（log_event 的 ts 为完成时刻，条形画 [ts-耗时, ts]；
 * 无耗时的事件画时刻圆点）。点击行查看节点详情。
 * ========================================================================== */

import React from 'react';
import { AlertTriangle, CheckCircle2, Info, XCircle, Clock } from 'lucide-react';
import type { ExecutionFlow, FlowEvent } from '@/services/logApi';
import { LEVEL_COLORS, eventShortName, fmtClock, fmtDuration, moduleColor, moduleName } from './flowShared';

/** 左侧信息列宽 */
const LEFT_W = 250;
/** 行高 */
const ROW_H = 36;

const LEVEL_ICONS: Record<string, React.ComponentType<{ size?: number }>> = {
  success: CheckCircle2,
  info: Info,
  warning: AlertTriangle,
  error: XCircle,
};

interface FlowTimelineProps {
  flow: ExecutionFlow;
  selectedIdx: number | null;
  onSelect: (idx: number | null) => void;
}

/** 事件 ts（完成时刻）→ 相对流程起点的毫秒偏移 */
function tsOffsetMs(flow: ExecutionFlow, ts: string): number {
  const t0 = new Date(flow.start_ts.replace(' ', 'T')).getTime();
  const t = new Date(ts.replace(' ', 'T')).getTime();
  if (Number.isNaN(t0) || Number.isNaN(t)) return 0;
  return Math.max(0, t - t0);
}

export const FlowTimeline: React.FC<FlowTimelineProps> = ({ flow, selectedIdx, onSelect }) => {
  const span = Math.max(1, flow.span_ms || 1);
  // 时间刻度：起点 / 25% / 50% / 75% / 终点
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((r) => ({
    ratio: r,
    label: fmtClock(
      new Date(
        new Date(flow.start_ts.replace(' ', 'T')).getTime() + span * r,
      ).toISOString().replace('Z', '').slice(11, 19),
    ),
  }));

  const rowEl = (ev: FlowEvent, i: number) => {
    const Icon = LEVEL_ICONS[ev.level] ?? Info;
    const color = LEVEL_COLORS[ev.level] ?? LEVEL_COLORS.info;
    const selected = selectedIdx === i;
    // 完成时刻偏移 + 耗时宽度（占整条流程时间轴的比例）
    const endOff = tsOffsetMs(flow, ev.ts);
    const dur = Math.max(0, ev.duration_ms ?? 0);
    const leftPct = (endOff / span) * 100;
    // 无耗时 → 圆点；有耗时 → 条形（最窄 0.8% 保证可见）
    const widthPct = dur > 0 ? Math.max(0.8, (dur / span) * 100) : 0;

    return (
      <div
        key={i}
        role="button"
        tabIndex={0}
        onClick={() => onSelect(selected ? null : i)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onSelect(selected ? null : i); }}
        style={{
          display: 'flex',
          alignItems: 'center',
          height: ROW_H,
          borderBottom: '1px solid var(--color-divider)',
          cursor: 'pointer',
          background: selected ? 'var(--color-primary-50)' : 'transparent',
          transition: 'background var(--transition-fast)',
        }}
      >
        {/* 左侧步骤信息 */}
        <div style={{ width: LEFT_W, flexShrink: 0, display: 'flex', alignItems: 'center', gap: 8, paddingLeft: 12 }}>
          <span style={{ color, display: 'flex', flexShrink: 0 }}><Icon size={14} /></span>
          <span className="mono text-tertiary" style={{ fontSize: 'var(--font-size-xs)', flexShrink: 0, width: 58 }}>
            {fmtClock(ev.ts)}
          </span>
          <span style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600, color: 'var(--color-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {String(i + 1).padStart(2, '0')} {eventShortName(ev)}
          </span>
        </div>
        {/* 右侧时间轴区（相对定位放条/点） */}
        <div style={{ flex: 1, position: 'relative', height: '100%', paddingRight: 16 }}>
          {dur > 0 ? (
            <div
              title={`${ev.friendly}（耗时 ${fmtDuration(dur)}）`}
              style={{
                position: 'absolute',
                top: '50%',
                left: `${Math.min(99.2, leftPct - widthPct)}%`,
                width: `${widthPct}%`,
                height: 14,
                transform: 'translateY(-50%)',
                borderRadius: 4,
                background: color,
                opacity: selected ? 1 : 0.78,
                boxShadow: selected ? `0 0 8px ${color}` : 'none',
                transition: 'opacity var(--transition-fast)',
              }}
            />
          ) : (
            <div
              title={ev.friendly}
              style={{
                position: 'absolute',
                top: '50%',
                left: `${Math.min(99.4, leftPct)}%`,
                width: 9,
                height: 9,
                transform: 'translate(-50%, -50%)',
                borderRadius: '50%',
                background: color,
              }}
            />
          )}
          {/* 模块徽标跟随条尾 */}
          <span
            className="badge"
            style={{
              position: 'absolute',
              top: '50%',
              left: `${Math.min(96, leftPct)}%`,
              transform: 'translate(6px, -50%)',
              fontSize: 10,
              padding: '1px 7px',
              background: 'var(--color-primary-50)',
              color: moduleColor(ev.module),
              borderColor: 'transparent',
              whiteSpace: 'nowrap',
            }}
          >
            {moduleName(ev.module)}
          </span>
        </div>
      </div>
    );
  };

  return (
    <div>
      {/* 顶部刻度尺 */}
      <div style={{ display: 'flex', height: 30, borderBottom: '1px solid var(--color-border-light)' }}>
        <div style={{ width: LEFT_W, flexShrink: 0, display: 'flex', alignItems: 'center', gap: 6, paddingLeft: 12, fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
          <Clock size={12} /> 时间轴（共 {fmtDuration(span)}）
        </div>
        <div style={{ flex: 1, position: 'relative', paddingRight: 16 }}>
          {ticks.map((t) => (
            <span
              key={t.ratio}
              className="mono"
              style={{
                position: 'absolute',
                left: `${t.ratio * 100}%`,
                transform: t.ratio === 1 ? 'translateX(-100%)' : t.ratio === 0 ? 'none' : 'translateX(-50%)',
                top: 6,
                fontSize: 10,
                color: 'var(--color-text-tertiary)',
              }}
            >
              {t.label}
            </span>
          ))}
        </div>
      </div>
      {/* 事件行 */}
      <div>{flow.events.map(rowEl)}</div>
      <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', padding: '8px 12px' }}>
        条形 = 步骤耗时（按完成时刻回溯）；圆点 = 无耗时的瞬时事件；点击行查看详情
      </div>
    </div>
  );
};

export default FlowTimeline;
