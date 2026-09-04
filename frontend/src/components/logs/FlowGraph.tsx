/* ==========================================================================
 * FlowGraph.tsx —— 执行流程泳道图（SVG，模块间交互关系视图）
 * --------------------------------------------------------------------------
 * 每个模块一条水平泳道，流程内每步事件是一个节点（按执行序均匀分布），
 * 节点间曼哈顿折线连线，跨泳道连线即「模块间交互」。
 * 交互：滚轮缩放（以鼠标为中心）+ 拖拽平移 + 点击节点查看详情。
 * 性能：全部变换走 transform（不触发布局重排），单流程事件数上限 120。
 * ========================================================================== */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { ExecutionFlow, FlowEvent } from '@/services/logApi';
import { LEVEL_COLORS, eventShortName, fmtDuration, moduleColor, moduleName } from './flowShared';

/** 泳道高度 */
const LANE_H = 92;
/** 节点水平步距 */
const STEP = 168;
/** 节点尺寸 */
const NODE_W = 138;
const NODE_H = 56;
/** 画布留白 */
const PAD_L = 118; // 左侧模块标签区
const PAD_R = 40;
const PAD_T = 18;
const PAD_B = 24;

interface FlowGraphProps {
  flow: ExecutionFlow;
  /** 选中节点索引（null 未选） */
  selectedIdx: number | null;
  onSelect: (idx: number | null) => void;
}

export const FlowGraph: React.FC<FlowGraphProps> = ({ flow, selectedIdx, onSelect }) => {
  const wrapRef = useRef<HTMLDivElement>(null);
  // 缩放平移（transform 实现，符合性能规范）
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const dragRef = useRef<{ x: number; y: number; panX: number; panY: number; moved: boolean } | null>(null);

  const lanes = flow.modules.length;
  const graphW = PAD_L + flow.events.length * STEP + PAD_R;
  const graphH = PAD_T + lanes * LANE_H + PAD_B;

  // 滚轮缩放（须 non-passive 才能 preventDefault；React onWheel 是 passive）
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const mx = e.clientX - rect.left; // 鼠标在容器内的位置
      const my = e.clientY - rect.top;
      setZoom((z) => {
        const next = Math.min(3, Math.max(0.3, z * (e.deltaY < 0 ? 1.12 : 0.89)));
        // 缩放时保持鼠标指向的内容点不动：pan' = mouse - (mouse - pan) * (next/z)
        setPan((p) => ({
          x: mx - (mx - p.x) * (next / z),
          y: my - (my - p.y) * (next / z),
        }));
        return next;
      });
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  // 拖拽平移（pointer events；移动超过阈值才算拖拽，避免吞掉节点点击）
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    dragRef.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y, moved: false };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.abs(dx) + Math.abs(dy) > 4) d.moved = true;
    if (d.moved) setPan({ x: d.panX + dx, y: d.panY + dy });
  };
  const onPointerUp = () => { dragRef.current = null; };

  const zoomBy = useCallback((factor: number) => {
    setZoom((z) => Math.min(3, Math.max(0.3, z * factor)));
  }, []);
  const resetView = useCallback(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, []);

  /** 节点中心坐标（索引均匀分布） */
  const nodePos = (i: number, module: string) => {
    const lane = flow.modules.indexOf(module);
    return {
      x: PAD_L + i * STEP + NODE_W / 2,
      y: PAD_T + lane * LANE_H + LANE_H / 2,
    };
  };

  /** 曼哈顿折线路径（节点右缘 → 中间竖线 → 下一节点左缘） */
  const edgePath = (from: { x: number; y: number }, to: { x: number; y: number }) => {
    const x1 = from.x + NODE_W / 2;
    const x2 = to.x - NODE_W / 2;
    const midX = (x1 + x2) / 2;
    return `M ${x1} ${from.y} H ${midX} ${from.y === to.y ? '' : `V ${to.y}`} H ${x2}`;
  };

  const nodeEl = (ev: FlowEvent, i: number) => {
    const { x, y } = nodePos(i, ev.module);
    const stroke = LEVEL_COLORS[ev.level] ?? LEVEL_COLORS.info;
    const selected = selectedIdx === i;
    return (
      <g
        key={i}
        style={{ cursor: 'pointer' }}
        onClick={(e) => {
          e.stopPropagation();
          onSelect(selected ? null : i);
        }}
      >
        <title>{`${fmtDuration(ev.duration_ms ?? 0)} · ${ev.friendly}`}</title>
        <rect
          x={x - NODE_W / 2}
          y={y - NODE_H / 2}
          width={NODE_W}
          height={NODE_H}
          rx={10}
          style={{
            fill: 'var(--color-card)',
            stroke,
            strokeWidth: selected ? 2.5 : 1.5,
            filter: selected ? `drop-shadow(0 0 6px ${stroke})` : undefined,
          }}
        />
        {/* 步骤序号 */}
        <text
          x={x - NODE_W / 2 + 14}
          y={y - NODE_H / 2 + 18}
          style={{ fill: stroke, fontSize: 11, fontWeight: 700 }}
        >
          {String(i + 1).padStart(2, '0')}
        </text>
        {/* 事件短名 */}
        <text
          x={x}
          y={y - 2}
          textAnchor="middle"
          style={{ fill: 'var(--color-text-primary)', fontSize: 12.5, fontWeight: 600 }}
        >
          {eventShortName(ev)}
        </text>
        {/* 耗时（仅有值时） */}
        {typeof ev.duration_ms === 'number' && ev.duration_ms > 0 && (
          <text
            x={x}
            y={y + 16}
            textAnchor="middle"
            style={{ fill: 'var(--color-text-tertiary)', fontSize: 10.5 }}
          >
            {fmtDuration(ev.duration_ms)}
          </text>
        )}
        {/* 错误角标 */}
        {ev.level === 'error' && (
          <circle cx={x + NODE_W / 2 - 4} cy={y - NODE_H / 2 + 4} r={7} style={{ fill: 'var(--color-error)' }} />
        )}
      </g>
    );
  };

  return (
    <div style={{ position: 'relative' }}>
      {/* 缩放控件 */}
      <div
        style={{
          position: 'absolute',
          top: 8,
          right: 8,
          zIndex: 2,
          display: 'flex',
          gap: 4,
          alignItems: 'center',
        }}
      >
        <button type="button" className="btn btn-ghost" style={{ padding: '2px 10px', fontSize: 'var(--font-size-xs)' }} onClick={() => zoomBy(1.25)} title="放大（滚轮也可）">+</button>
        <button type="button" className="btn btn-ghost" style={{ padding: '2px 10px', fontSize: 'var(--font-size-xs)' }} onClick={() => zoomBy(0.8)} title="缩小">−</button>
        <button type="button" className="btn btn-ghost" style={{ padding: '2px 10px', fontSize: 'var(--font-size-xs)' }} onClick={resetView} title="重置视图">{Math.round(zoom * 100)}%</button>
      </div>

      <div
        ref={wrapRef}
        style={{
          height: 460,
          overflow: 'hidden',
          cursor: dragRef.current?.moved ? 'grabbing' : 'grab',
          background: 'var(--color-surface-secondary, var(--color-bg))',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--color-border-light)',
          position: 'relative',
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
        onClick={() => onSelect(null)}
      >
        <svg
          width={graphW}
          height={graphH}
          style={{
            display: 'block',
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
            transformOrigin: '0 0',
          }}
        >
          {/* 泳道背景与模块标签 */}
          {flow.modules.map((m, li) => (
            <g key={m}>
              <rect
                x={PAD_L - 14}
                y={PAD_T + li * LANE_H + 4}
                width={Math.max(graphW - PAD_L + 14 - PAD_R, 0)}
                height={LANE_H - 8}
                rx={8}
                style={{
                  fill: li % 2 === 0 ? 'var(--color-primary-50)' : 'transparent',
                  stroke: 'var(--color-divider)',
                  strokeWidth: 1,
                }}
              />
              <text
                x={PAD_L - 26}
                y={PAD_T + li * LANE_H + LANE_H / 2 - 4}
                textAnchor="end"
                style={{ fill: moduleColor(m), fontSize: 12.5, fontWeight: 700 }}
              >
                {moduleName(m)}
              </text>
              <text
                x={PAD_L - 26}
                y={PAD_T + li * LANE_H + LANE_H / 2 + 12}
                textAnchor="end"
                style={{ fill: 'var(--color-text-tertiary)', fontSize: 10 }}
              >
                {m}
              </text>
            </g>
          ))}

          {/* 执行序连线（模块间交互） */}
          {flow.events.slice(0, -1).map((ev, i) => {
            const from = nodePos(i, ev.module);
            const to = nodePos(i + 1, flow.events[i + 1].module);
            const cross = ev.module !== flow.events[i + 1].module;
            return (
              <path
                key={`e-${i}`}
                d={edgePath(from, to)}
                fill="none"
                style={{
                  stroke: cross ? 'var(--color-accent)' : 'var(--color-text-tertiary)',
                  strokeWidth: cross ? 2 : 1.5,
                  strokeDasharray: cross ? 'none' : '4 3',
                  opacity: cross ? 0.9 : 0.6,
                }}
                markerEnd=""
              />
            );
          })}

          {/* 节点 */}
          {flow.events.map(nodeEl)}
        </svg>

        {/* 图例 */}
        <div
          style={{
            position: 'absolute',
            left: 10,
            bottom: 8,
            display: 'flex',
            gap: 12,
            fontSize: 'var(--font-size-xs)',
            color: 'var(--color-text-tertiary)',
            pointerEvents: 'none',
          }}
        >
          <span>实线 = 跨模块交互</span>
          <span>虚线 = 模块内步骤</span>
          <span>滚轮缩放 · 拖拽平移 · 点击节点看详情</span>
        </div>
      </div>
    </div>
  );
};

export default FlowGraph;
