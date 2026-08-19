/* ==========================================================================
 * KnowledgeGraphView.tsx —— 知识图谱视图（TASK-055 / v2.3.1 新增）
 * --------------------------------------------------------------------------
 * 数据：GET /v1/knowledge/graph（kid 可选）
 * 交互：节点拖拽 / 滚轮缩放 / 背景平移 / 点击节点查看详情
 * 实现说明：离线环境不引入 react-flow/cytoscape（package.json 无依赖），
 *           用原生 SVG + 环形布局自实现同等交互，零新增依赖。
 * ========================================================================== */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { X } from 'lucide-react';
import type { KnowledgeGraph, KnowledgeGraphNode } from '@/services/learningApi';

const WIDTH = 760;
const HEIGHT = 420;
const NODE_R = 18;

interface NodePos { x: number; y: number }

interface SelectedInfo {
  node: KnowledgeGraphNode;
  relations: { direction: 'out' | 'in'; relation: string; other: string }[];
}

/** 环形布局：度数最高的节点居中，其余按连接关系分环分布 */
function computeLayout(graph: KnowledgeGraph): Map<string, NodePos> {
  const pos = new Map<string, NodePos>();
  const nodes = graph.nodes;
  if (nodes.length === 0) return pos;

  const degree = new Map<string, number>();
  const neighbors = new Map<string, string[]>();
  for (const e of graph.edges) {
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
    neighbors.set(e.source, [...(neighbors.get(e.source) ?? []), e.target]);
    neighbors.set(e.target, [...(neighbors.get(e.target) ?? []), e.source]);
  }

  const cx = WIDTH / 2;
  const cy = HEIGHT / 2;
  const sorted = [...nodes].sort(
    (a, b) => (degree.get(b.name) ?? 0) - (degree.get(a.name) ?? 0));
  const center = sorted[0];
  pos.set(center.name, { x: cx, y: cy });

  // BFS 分环
  const visited = new Set<string>([center.name]);
  let ring: string[] = [center.name];
  let ringIdx = 0;
  while (ring.length > 0 && visited.size < nodes.length) {
    const next: string[] = [];
    for (const name of ring) {
      for (const nb of neighbors.get(name) ?? []) {
        if (!visited.has(nb)) {
          visited.add(nb);
          next.push(nb);
        }
      }
    }
    ringIdx += 1;
    const radius = Math.min(ringIdx * 110, Math.min(WIDTH, HEIGHT) / 2 - 40);
    next.forEach((name, i) => {
      const angle = (2 * Math.PI * i) / next.length + ringIdx * 0.6;
      pos.set(name, { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) });
    });
    ring = next;
  }
  // 孤立节点放最外环
  const rest = nodes.filter((n) => !pos.has(n.name));
  const rOut = Math.min(WIDTH, HEIGHT) / 2 - 24;
  rest.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(rest.length, 1);
    pos.set(n.name, { x: cx + rOut * Math.cos(angle), y: cy + rOut * Math.sin(angle) });
  });
  return pos;
}

export interface KnowledgeGraphViewProps {
  graph: KnowledgeGraph | null;
  loading: boolean;
  onRefresh: () => void;
}

export const KnowledgeGraphView: React.FC<KnowledgeGraphViewProps> = ({
  graph, loading, onRefresh,
}) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const [positions, setPositions] = useState<Map<string, NodePos>>(new Map());
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState<NodePos>({ x: 0, y: 0 });
  const [selected, setSelected] = useState<SelectedInfo | null>(null);
  const dragRef = useRef<
    | { kind: 'node'; name: string; dx: number; dy: number }
    | { kind: 'pan'; sx: number; sy: number; ox: number; oy: number }
    | null
  >(null);

  // 图数据变化 → 重算布局
  useEffect(() => {
    setPositions(graph ? computeLayout(graph) : new Map());
    setSelected(null);
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }, [graph]);

  const nodeByName = useMemo(() => {
    const m = new Map<string, KnowledgeGraphNode>();
    for (const n of graph?.nodes ?? []) m.set(n.name, n);
    return m;
  }, [graph]);

  const toSvg = useCallback((clientX: number, clientY: number): NodePos => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    // 归一化到 viewBox 坐标系
    const px = ((clientX - rect.left) / rect.width) * WIDTH;
    const py = ((clientY - rect.top) / rect.height) * HEIGHT;
    return { x: (px - offset.x) / scale, y: (py - offset.y) / scale };
  }, [offset, scale]);

  const onNodePointerDown = (e: React.PointerEvent, name: string) => {
    e.stopPropagation();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    const p = toSvg(e.clientX, e.clientY);
    const cur = positions.get(name) ?? { x: 0, y: 0 };
    dragRef.current = { kind: 'node', name, dx: cur.x - p.x, dy: cur.y - p.y };
  };

  const onBgPointerDown = (e: React.PointerEvent) => {
    dragRef.current = {
      kind: 'pan', sx: e.clientX, sy: e.clientY, ox: offset.x, oy: offset.y,
    };
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (drag.kind === 'node') {
      const p = toSvg(e.clientX, e.clientY);
      setPositions((prev) => {
        const next = new Map(prev);
        next.set(drag.name, { x: p.x + drag.dx, y: p.y + drag.dy });
        return next;
      });
    } else {
      const rect = svgRef.current?.getBoundingClientRect();
      const kx = rect ? WIDTH / rect.width : 1;
      const ky = rect ? HEIGHT / rect.height : 1;
      setOffset({
        x: drag.ox + (e.clientX - drag.sx) * kx,
        y: drag.oy + (e.clientY - drag.sy) * ky,
      });
    }
  };

  const onPointerUp = () => { dragRef.current = null; };

  const onWheel = (e: React.WheelEvent) => {
    const next = Math.min(3, Math.max(0.3, scale * (e.deltaY < 0 ? 1.12 : 0.89)));
    setScale(next);
  };

  const onNodeClick = (name: string) => {
    const node = nodeByName.get(name);
    if (!node || !graph) return;
    const relations: SelectedInfo['relations'] = [];
    for (const e of graph.edges) {
      if (e.source === name) relations.push({ direction: 'out', relation: e.relation, other: e.target });
      else if (e.target === name) relations.push({ direction: 'in', relation: e.relation, other: e.source });
    }
    setSelected({ node, relations });
  };

  const nodes = graph?.nodes ?? [];
  const edges = graph?.edges ?? [];

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
          {graph
            ? `节点 ${nodes.length} · 关系 ${edges.length}（全库实体 ${graph.total_entities ?? 0} / 边 ${graph.total_edges ?? 0}）`
            : '图谱未加载'}
          {' '}· 滚轮缩放 / 拖拽节点 / 点击查看详情
        </span>
        <button className="btn btn-ghost btn-sm" onClick={onRefresh} disabled={loading}>
          {loading ? '加载中…' : '⟳ 刷新'}
        </button>
      </div>

      {nodes.length === 0 ? (
        <div className="text-secondary text-sm" style={{ padding: 'var(--space-4) 0' }}>
          {loading ? '图谱加载中…' : '暂无图谱数据（学习产生知识后自动构建实体关系）。'}
        </div>
      ) : (
        <div className="flex gap-3" style={{ alignItems: 'flex-start' }}>
          <svg
            ref={svgRef}
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            style={{
              width: '100%', maxWidth: WIDTH, height: 'auto',
              border: '1px solid var(--color-border-light)',
              borderRadius: 'var(--radius-lg)',
              background: 'var(--color-bg-secondary, rgba(255,255,255,0.02))',
              cursor: dragRef.current?.kind === 'pan' ? 'grabbing' : 'grab',
              touchAction: 'none',
            }}
            onPointerDown={onBgPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerLeave={onPointerUp}
            onWheel={onWheel}
          >
            <g transform={`translate(${offset.x},${offset.y}) scale(${scale})`}>
              {/* 边 */}
              {edges.map((e) => {
                const s = positions.get(e.source);
                const t = positions.get(e.target);
                if (!s || !t) return null;
                const mx = (s.x + t.x) / 2;
                const my = (s.y + t.y) / 2;
                return (
                  <g key={e.id}>
                    <line
                      x1={s.x} y1={s.y} x2={t.x} y2={t.y}
                      stroke="var(--color-border-light)"
                      strokeWidth={Math.min(1 + (e.weight ?? 1) * 0.4, 3)}
                      strokeOpacity={0.7}
                    />
                    <text
                      x={mx} y={my - 4} textAnchor="middle"
                      style={{ fontSize: 10, fill: 'var(--color-text-tertiary)', pointerEvents: 'none' }}
                    >
                      {e.relation}
                    </text>
                  </g>
                );
              })}
              {/* 节点 */}
              {nodes.map((n) => {
                const p = positions.get(n.name);
                if (!p) return null;
                const isSel = selected?.node.name === n.name;
                const r = NODE_R + Math.min((n.ref_count ?? 1) * 1.5, 10);
                return (
                  <g
                    key={n.id}
                    transform={`translate(${p.x},${p.y})`}
                    style={{ cursor: 'pointer' }}
                    onPointerDown={(e) => onNodePointerDown(e, n.name)}
                    onClick={() => onNodeClick(n.name)}
                  >
                    <circle
                      r={r}
                      fill={isSel ? 'var(--color-primary, #FF6B9D)' : 'var(--color-card, #16213E)'}
                      stroke={isSel ? 'var(--color-primary, #FF6B9D)' : 'var(--color-border-light)'}
                      strokeWidth={isSel ? 2.5 : 1.5}
                    />
                    <text
                      textAnchor="middle" dy={r + 14}
                      style={{
                        fontSize: 11,
                        fill: 'var(--color-text-secondary)',
                        pointerEvents: 'none',
                      }}
                    >
                      {n.name.length > 10 ? `${n.name.slice(0, 10)}…` : n.name}
                    </text>
                  </g>
                );
              })}
            </g>
          </svg>

          {/* 节点详情 */}
          {selected && (
            <aside
              className="card"
              style={{ width: 240, flexShrink: 0, padding: 'var(--space-3)' }}
              aria-label="节点详情"
            >
              <div className="flex items-center justify-between mb-2">
                <strong className="text-sm">{selected.node.name}</strong>
                <button className="btn btn-ghost btn-sm" onClick={() => setSelected(null)} aria-label="关闭节点详情"><X size={14} aria-hidden="true" /></button>
              </div>
              <div className="text-tertiary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>
                被引用 {selected.node.ref_count ?? 1} 次 · 关系 {selected.relations.length} 条
              </div>
              <div className="flex flex-col gap-1" style={{ maxHeight: 300, overflowY: 'auto' }}>
                {selected.relations.map((r, i) => (
                  <div
                    key={i}
                    className="text-xs"
                    style={{
                      padding: 'var(--space-1) var(--space-2)',
                      border: '1px solid var(--color-border-light)',
                      borderRadius: 'var(--radius-md)',
                    }}
                  >
                    {r.direction === 'out'
                      ? `${selected.node.name} —${r.relation}→ ${r.other}`
                      : `${r.other} —${r.relation}→ ${selected.node.name}`}
                  </div>
                ))}
                {selected.relations.length === 0 && (
                  <span className="text-tertiary text-xs">暂无关联关系</span>
                )}
              </div>
            </aside>
          )}
        </div>
      )}
    </div>
  );
};

export default KnowledgeGraphView;
