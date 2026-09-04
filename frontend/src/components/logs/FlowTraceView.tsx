/* ==========================================================================
 * FlowTraceView.tsx —— 执行流程追踪视图（2026-08-23 流程记录机制优化）
 * --------------------------------------------------------------------------
 * 精确 flow_id 数据源（flow_trace.db，区别于 FlowPanel 的事件启发式聚合）：
 *   - 左栏：流程列表（状态/大白话描述/模块/耗时/卡住秒数，新→旧）
 *   - 筛选：状态（含「卡住」）+ 模块 + 时间范围 + 关键词 + 状态计数徽章
 *   - 主区：流程头（触发/输入输出/资源对比）+ 卡住定位卡（stall_analysis：
 *     卡在哪个环节/该环节输入与最后输出/已执行时长/排查提示）
 *     + 节点链时间轴（每节点输入/输出/状态/耗时/CPU·内存·GPU 快照/异常）
 *   - 实时：10s 自动刷新（running 流程详情同步刷新，心跳进度可见）
 * ========================================================================== */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Clock,
  Cpu,
  Loader2,
  PauseCircle,
  Radar,
  RefreshCw,
  Search,
  ServerCog,
  XCircle,
} from 'lucide-react';
import {
  getFlowTrace,
  getFlowTraceStats,
  queryFlowTraces,
  type FlowTraceDetail,
  type FlowTraceListItem,
  type ResourceSnapshot,
  type TraceStatus,
} from '@/services/logApi';
import { useAppStore } from '@/stores/useAppStore';
import {
  fmtDuration,
  fmtTime,
  moduleColor,
  moduleName,
  TRACE_STATUS_COLORS,
  TRACE_STATUS_NAMES,
} from './flowShared';

/** 自动刷新间隔（毫秒） */
const AUTO_REFRESH_MS = 10_000;

/** 流程状态图标 */
function TraceStatusIcon({ status, spinning = false }: { status: TraceStatus | string; spinning?: boolean }) {
  const color = TRACE_STATUS_COLORS[status as TraceStatus] ?? 'var(--color-text-tertiary)';
  if (status === 'error') return <span style={{ color, display: 'flex' }}><XCircle size={15} /></span>;
  if (status === 'stalled') return <span style={{ color, display: 'flex' }}><AlertTriangle size={15} /></span>;
  if (status === 'orphan') return <span style={{ color, display: 'flex' }}><PauseCircle size={15} /></span>;
  if (status === 'running') {
    return (
      <span style={{ color, display: 'flex' }}>
        {spinning ? <Loader2 size={15} className="animate-spin" /> : <Radar size={15} />}
      </span>
    );
  }
  return <span style={{ color, display: 'flex' }}><CheckCircle2 size={15} /></span>;
}

/** 资源快照条（CPU/内存/GPU 一行内联展示） */
const ResourceBar: React.FC<{ res: ResourceSnapshot | null | undefined; compact?: boolean }> = ({ res, compact }) => {
  if (!res || (!res.cpu_percent && !res.ram_used_gb && !res.gpu)) return null;
  const gpu = res.gpu;
  return (
    <span
      className="flex items-center gap-2 flex-wrap text-tertiary"
      style={{ fontSize: compact ? 10 : 'var(--font-size-xs)' }}
    >
      <span className="flex items-center gap-0.5" title="CPU 使用率">
        <Cpu size={compact ? 9 : 11} /> {res.cpu_percent?.toFixed(0)}%
      </span>
      {res.ram_used_gb ? <span title="内存占用">内存 {res.ram_used_gb.toFixed(1)}G</span> : null}
      {gpu && gpu.vram_total_mb ? (
        <span title="GPU 显存占用 / 利用率 / 温度">
          GPU {(gpu.vram_used_mb / 1024).toFixed(1)}/{(gpu.vram_total_mb / 1024).toFixed(0)}G
          {' · '}{gpu.util_percent?.toFixed(0)}%{gpu.temp_celsius ? ` · ${gpu.temp_celsius.toFixed(0)}°C` : ''}
        </span>
      ) : null}
    </span>
  );
};

/** 卡住定位卡（stall_analysis：用户报告「卡住了」时的一眼定位） */
const StallAnalysisCard: React.FC<{ detail: FlowTraceDetail }> = ({ detail }) => {
  const sa = detail.stall_analysis;
  if (!sa) return null;
  return (
    <div
      className="card hoverable"
      style={{
        marginTop: 'var(--space-3)',
        padding: 'var(--space-3) var(--space-4)',
        borderLeft: '3px solid var(--color-warning)',
        background: 'var(--color-warning-50, var(--color-primary-50))',
      }}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <span style={{ color: 'var(--color-warning)', display: 'flex' }}><AlertTriangle size={15} /></span>
        <span style={{ fontWeight: 'var(--font-weight-semibold)', fontSize: 'var(--font-size-sm)', color: 'var(--color-warning)' }}>
          卡住定位 · 疑似卡在「{sa.where}」环节
        </span>
        {sa.node_running_ms ? (
          <span className="badge" style={{ background: 'var(--color-warning-50, transparent)', color: 'var(--color-warning)', fontSize: 'var(--font-size-xs)' }}>
            该环节已执行 {fmtDuration(sa.node_running_ms)}
          </span>
        ) : null}
      </div>
      <div className="mt-2 flex flex-col gap-1" style={{ fontSize: 'var(--font-size-xs)' }}>
        {sa.node_input && (
          <div className="flex gap-1.5">
            <span className="text-tertiary" style={{ flexShrink: 0 }}>环节输入：</span>
            <span className="mono" style={{ color: 'var(--color-text-secondary)', wordBreak: 'break-all' }}>{sa.node_input}</span>
          </div>
        )}
        {sa.node_last_output && (
          <div className="flex gap-1.5">
            <span className="text-tertiary" style={{ flexShrink: 0 }}>最后输出：</span>
            <span className="mono" style={{ color: 'var(--color-text-secondary)', wordBreak: 'break-all' }}>{sa.node_last_output}</span>
          </div>
        )}
        <div className="flex gap-1.5">
          <span className="text-tertiary" style={{ flexShrink: 0 }}>资源状态：</span>
          <ResourceBar res={detail.resource_end} />
        </div>
        <div style={{ color: 'var(--color-warning)', marginTop: 2 }}>{sa.hint}</div>
        {detail.error_code && (
          <div className="flex gap-1.5">
            <span className="text-tertiary" style={{ flexShrink: 0 }}>错误码：</span>
            <span className="mono" style={{ color: 'var(--color-error)' }}>{detail.error_code}</span>
          </div>
        )}
      </div>
    </div>
  );
};

/** 节点链时间轴（每节点：序号/名称/状态/耗时/输入输出/资源快照/异常） */
const NodeTimeline: React.FC<{ detail: FlowTraceDetail }> = ({ detail }) => (
  <div style={{ marginTop: 'var(--space-3)' }}>
    {detail.nodes.map((n, i) => {
      const color = TRACE_STATUS_COLORS[n.status as TraceStatus] ?? 'var(--color-text-tertiary)';
      const isLast = i === detail.nodes.length - 1;
      return (
        <div key={`${n.seq}-${n.node}`} style={{ display: 'flex', gap: 'var(--space-3)' }}>
          {/* 竖直连线 + 状态图标 */}
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', paddingTop: 2 }}>
            <TraceStatusIcon status={n.status} spinning={n.status === 'running' && isLast} />
            {!isLast && (
              <span
                style={{
                  flex: 1, width: 2, minHeight: 26, marginTop: 2,
                  background: 'var(--color-divider)', borderRadius: 1,
                }}
              />
            )}
          </div>
          {/* 节点内容 */}
          <div
            className="card hoverable"
            style={{
              flex: 1, minWidth: 0, marginBottom: isLast ? 0 : 'var(--space-2)',
              padding: 'var(--space-2) var(--space-3)',
              borderLeft: `3px solid ${color}`,
            }}
          >
            <div className="flex items-center gap-2 flex-wrap">
              <span className="mono text-tertiary" style={{ fontSize: 10 }}>#{n.seq}</span>
              <span style={{ fontWeight: 'var(--font-weight-semibold)', fontSize: 'var(--font-size-sm)' }}>
                {n.node}
              </span>
              {n.friendly && n.friendly !== n.node && (
                <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>{n.friendly}</span>
              )}
              <span className="badge" style={{ background: 'var(--color-primary-50)', color: moduleColor(n.module || detail.module), fontSize: 10 }}>
                {moduleName(n.module || detail.module)}
              </span>
              <span style={{ fontSize: 'var(--font-size-xs)', color }}>{TRACE_STATUS_NAMES[n.status as TraceStatus] ?? n.status}</span>
              {typeof n.duration_ms === 'number' && n.duration_ms !== null && (
                <span className="text-tertiary flex items-center gap-1" style={{ fontSize: 'var(--font-size-xs)' }}>
                  <Clock size={11} /> {fmtDuration(n.duration_ms)}
                </span>
              )}
              <span className="flex-1" />
              <span className="text-tertiary mono" style={{ fontSize: 'var(--font-size-xs)' }}>{fmtTime(n.started_at)}</span>
            </div>
            {(n.input_summary || n.output_summary) && (
              <div className="flex items-center gap-1 flex-wrap mt-1" style={{ fontSize: 'var(--font-size-xs)' }}>
                {n.input_summary && (
                  <span className="mono" style={{ color: 'var(--color-text-secondary)', wordBreak: 'break-all' }}>
                    <span className="text-tertiary">入 </span>{n.input_summary}
                  </span>
                )}
                {n.input_summary && n.output_summary && <ChevronRight size={11} className="text-tertiary" />}
                {n.output_summary && (
                  <span className="mono" style={{ color: 'var(--color-text-secondary)', wordBreak: 'break-all' }}>
                    <span className="text-tertiary">出 </span>{n.output_summary}
                  </span>
                )}
              </div>
            )}
            {n.error && (
              <div className="mono" style={{ marginTop: 4, fontSize: 'var(--font-size-xs)', color: 'var(--color-error)', wordBreak: 'break-all' }}>
                异常：{n.error}
              </div>
            )}
            <div className="mt-1"><ResourceBar res={n.resource} compact /></div>
          </div>
        </div>
      );
    })}
  </div>
);

export const FlowTraceView: React.FC = () => {
  // ── 数据状态 ──
  const [flows, setFlows] = useState<FlowTraceListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<{ by_status: Record<TraceStatus, number>; modules: string[] } | null>(null);
  const [detail, setDetail] = useState<FlowTraceDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastRefresh, setLastRefresh] = useState('');
  const [loadError, setLoadError] = useState(false);

  // ── 筛选状态 ──
  const [status, setStatus] = useState<TraceStatus | ''>('');
  const [module, setModule] = useState('');
  const [days, setDays] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [keywordInput, setKeywordInput] = useState('');

  // ── 视图状态 ──
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [selectedFlowId, setSelectedFlowId] = useState('');

  const queryRef = useRef({ status, module, days, keyword });
  queryRef.current = { status, module, days, keyword };
  const selectedIdRef = useRef(selectedFlowId);
  selectedIdRef.current = selectedFlowId;

  /** 拉取流程列表 + 统计（选中流程为 running 时同步刷新详情） */
  const fetchTraces = useCallback(async () => {
    setLoading(true);
    const { showToast } = useAppStore.getState();
    try {
      const q = queryRef.current;
      const [res, st] = await Promise.all([
        queryFlowTraces({
          days: q.days,
          status: q.status || undefined,
          module: q.module || undefined,
          keyword: q.keyword || undefined,
          limit: 50,
        }),
        getFlowTraceStats(),
      ]);
      setFlows(res.flows);
      setTotal(res.total);
      setStats({ by_status: st.by_status, modules: st.modules });
      setLoadError(false);
      setLastRefresh(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      // 选中保持：原选中还在则保留，否则选最新一条
      const prevId = selectedIdRef.current;
      const keep = prevId && res.flows.some((f) => f.flow_id === prevId) ? prevId : (res.flows[0]?.flow_id ?? '');
      setSelectedFlowId(keep);
      // running 流程详情随列表刷新（看实时心跳）；终态流程只在切换时拉取
      const keepFlow = res.flows.find((f) => f.flow_id === keep);
      if (keep && (!keepFlow || keepFlow.status === 'running' || keepFlow.status === 'stalled' || keep !== prevId)) {
        try {
          setDetail(await getFlowTrace(keep));
        } catch { /* 详情拉取失败保留旧详情 */ }
      }
    } catch {
      setLoadError(true);
      showToast('流程追踪查询失败：后端服务可能未启动', 'error');
    } finally {
      setLoading(false);
    }
  }, []);

  /** 切换选中流程 → 拉全节点明细 */
  useEffect(() => {
    if (!selectedFlowId) { setDetail(null); return; }
    let cancelled = false;
    getFlowTrace(selectedFlowId)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch(() => { if (!cancelled) setDetail(null); });
    return () => { cancelled = true; };
  }, [selectedFlowId]);

  // 初次加载
  useEffect(() => { void fetchTraces(); }, [fetchTraces]);

  // 筛选变化 → 重新拉取
  useEffect(() => { void fetchTraces(); }, [status, module, days, keyword, fetchTraces]);

  // 自动刷新（10s）
  useEffect(() => {
    if (!autoRefresh) return;
    const t = window.setInterval(() => void fetchTraces(), AUTO_REFRESH_MS);
    return () => window.clearInterval(t);
  }, [autoRefresh, fetchTraces]);

  const byStatus = stats?.by_status;

  return (
    <>
      {/* 筛选栏 */}
      <div className="card hoverable" style={{ padding: 'var(--space-3) var(--space-4)', marginTop: 'var(--space-3)' }}>
        <div className="flex items-center gap-2 flex-wrap">
          {(['', 'running', 'stalled', 'success', 'error', 'cancelled', 'orphan'] as const).map((st) => {
            const active = status === st;
            const count = st && byStatus ? (byStatus[st] ?? 0) : null;
            const color = st ? TRACE_STATUS_COLORS[st] : 'var(--color-primary)';
            return (
              <button
                key={st || 'all'}
                type="button"
                className={`btn ${active ? 'btn-primary' : 'btn-ghost'}`}
                style={{ padding: '4px 12px', fontSize: 'var(--font-size-xs)', gap: 4 }}
                onClick={() => setStatus(st)}
              >
                {st ? TRACE_STATUS_NAMES[st] : '全部状态'}
                {count !== null && count > 0 && (
                  <span
                    className="badge"
                    style={{ background: active ? 'rgba(255,255,255,.25)' : 'var(--color-primary-50)', color: active ? '#fff' : color, fontSize: 10, padding: '0 6px' }}
                  >
                    {count}
                  </span>
                )}
              </button>
            );
          })}
          <select
            className="input"
            style={{ width: 'auto', padding: '4px 8px', fontSize: 'var(--font-size-xs)' }}
            value={module}
            onChange={(e) => setModule(e.target.value)}
            aria-label="按模块筛选"
          >
            <option value="">全部模块</option>
            {(stats?.modules ?? []).map((m) => (
              <option key={m} value={m}>{moduleName(m)}</option>
            ))}
          </select>
          <select
            className="input"
            style={{ width: 'auto', padding: '4px 8px', fontSize: 'var(--font-size-xs)' }}
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            aria-label="按时间范围筛选"
          >
            <option value={1}>最近 1 天</option>
            <option value={7}>最近 7 天</option>
            <option value={30}>最近 30 天</option>
          </select>
          <span className="flex items-center gap-1 flex-1" style={{ minWidth: 180 }}>
            <Search size={13} aria-hidden="true" className="text-tertiary" />
            <input
              className="input"
              style={{ padding: '4px 8px', fontSize: 'var(--font-size-xs)' }}
              placeholder="搜索功能描述/输入输出/错误…"
              value={keywordInput}
              onChange={(e) => setKeywordInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') setKeyword(keywordInput.trim()); }}
            />
          </span>
          <span className="flex-1" />
          <label className="text-tertiary flex items-center gap-1.5" style={{ fontSize: 'var(--font-size-xs)', cursor: 'pointer' }}>
            <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
            自动刷新
          </label>
          <button
            type="button"
            className="btn btn-ghost"
            style={{ padding: '4px 12px', fontSize: 'var(--font-size-xs)' }}
            disabled={loading}
            onClick={() => void fetchTraces()}
          >
            <RefreshCw size={13} aria-hidden="true" className={loading ? 'animate-spin' : ''} /> 刷新
          </button>
        </div>
        {lastRefresh && (
          <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', marginTop: 6 }}>
            {loadError ? '上次刷新失败，正在重试…' : `上次刷新 ${lastRefresh} · 每 10s 自动刷新 · 共 ${total} 条流程`}
          </div>
        )}
      </div>

      {/* 主体：左流程列表 + 右详情 */}
      <div className="flex gap-3 mt-3" style={{ alignItems: 'stretch' }}>
        {/* 左：流程列表 */}
        <div className="card hoverable" style={{ width: 320, flexShrink: 0, padding: 0, display: 'flex', flexDirection: 'column', maxHeight: 620 }}>
          <div
            className="flex items-center gap-2"
            style={{ padding: 'var(--space-3) var(--space-4)', borderBottom: '1px solid var(--color-divider)' }}
          >
            <Radar size={14} aria-hidden="true" style={{ color: 'var(--color-primary)' }} />
            <span style={{ fontWeight: 'var(--font-weight-semibold)', fontSize: 'var(--font-size-sm)' }}>流程追踪</span>
            <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', marginLeft: 'auto' }}>
              {flows.length}/{total}
            </span>
          </div>
          <div style={{ overflowY: 'auto', flex: 1 }}>
            {loading && flows.length === 0 && (
              <div className="text-secondary text-sm" style={{ padding: 'var(--space-6)', textAlign: 'center' }}>加载中…</div>
            )}
            {!loading && flows.length === 0 && (
              <div className="text-secondary text-sm" style={{ padding: 'var(--space-6)', textAlign: 'center' }}>
                {total === 0 ? '最近没有功能执行记录。发起对话、生成图片后，这里会出现完整流程' : '当前筛选下没有匹配的流程'}
              </div>
            )}
            {flows.map((f) => {
              const active = f.flow_id === selectedFlowId;
              const color = TRACE_STATUS_COLORS[f.status];
              return (
                <button
                  key={f.flow_id}
                  type="button"
                  onClick={() => setSelectedFlowId(f.flow_id)}
                  style={{
                    display: 'block',
                    width: '100%',
                    textAlign: 'left',
                    padding: 'var(--space-2) var(--space-3)',
                    background: active ? 'var(--color-primary-100)' : 'transparent',
                    borderLeft: `3px solid ${active ? 'var(--color-primary)' : 'transparent'}`,
                    border: 'none',
                    borderBottom: '1px solid var(--color-divider)',
                    cursor: 'pointer',
                    transition: 'background var(--transition-fast)',
                  }}
                >
                  <div className="flex items-center gap-2">
                    <TraceStatusIcon status={f.status} />
                    <span className="mono text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>{fmtTime(f.started_at)}</span>
                    <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', marginLeft: 'auto' }}>
                      {f.node_count} 节点
                    </span>
                  </div>
                  <div
                    style={{
                      fontSize: 'var(--font-size-xs)',
                      color: 'var(--color-text-secondary)',
                      marginTop: 3,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {f.friendly}
                  </div>
                  <div className="flex items-center gap-1 mt-1" style={{ flexWrap: 'wrap' }}>
                    <span className="badge" style={{ fontSize: 10, padding: '0 6px', color: moduleColor(f.module), background: 'var(--color-primary-50)' }}>
                      {moduleName(f.module)} · {f.feature}
                    </span>
                    <span style={{ fontSize: 10, marginLeft: 'auto', color }}>
                      {f.status === 'stalled'
                        ? `卡住 ${Math.round(f.stalled_seconds)}s`
                        : f.duration_ms !== null && f.duration_ms !== undefined
                          ? fmtDuration(f.duration_ms)
                          : '进行中'}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* 右：流程详情 */}
        <div className="card hoverable" style={{ flex: 1, minWidth: 0, padding: 'var(--space-3) var(--space-4)', maxHeight: 620, overflowY: 'auto' }}>
          {detail ? (
            <>
              {/* 流程头 */}
              <div className="flex items-center gap-2 flex-wrap">
                <TraceStatusIcon status={detail.status} spinning={detail.status === 'running'} />
                <span style={{ fontWeight: 'var(--font-weight-semibold)', fontSize: 'var(--font-size-sm)' }}>
                  {detail.friendly}
                </span>
                <span className="badge" style={{ background: 'var(--color-primary-50)', color: TRACE_STATUS_COLORS[detail.status], fontSize: 'var(--font-size-xs)' }}>
                  {TRACE_STATUS_NAMES[detail.status]}
                </span>
                {detail.duration_ms !== null && detail.duration_ms !== undefined && (
                  <span className="text-tertiary flex items-center gap-1" style={{ fontSize: 'var(--font-size-xs)' }}>
                    <Clock size={11} /> {fmtDuration(detail.duration_ms)}
                  </span>
                )}
                <span className="flex-1" />
                <span className="text-tertiary mono" style={{ fontSize: 10 }}>{detail.flow_id}</span>
              </div>
              {/* 触发/输入输出/资源对比 */}
              <div className="flex flex-col gap-1 mt-2" style={{ fontSize: 'var(--font-size-xs)' }}>
                <div className="flex gap-1.5 flex-wrap">
                  <span className="text-tertiary">触发：</span>
                  <span style={{ color: 'var(--color-text-secondary)' }}>{detail.trigger || '—'}</span>
                  {detail.input_summary && <><span className="text-tertiary" style={{ marginLeft: 8 }}>输入：</span><span className="mono" style={{ color: 'var(--color-text-secondary)' }}>{detail.input_summary}</span></>}
                  {detail.output_summary && <><span className="text-tertiary" style={{ marginLeft: 8 }}>输出：</span><span className="mono" style={{ color: 'var(--color-text-secondary)' }}>{detail.output_summary}</span></>}
                </div>
                {detail.error_code && (
                  <div className="flex gap-1.5">
                    <span className="text-tertiary">错误码：</span>
                    <span className="mono" style={{ color: 'var(--color-error)' }}>{detail.error_code}</span>
                    {detail.error_detail && <span className="mono text-tertiary" style={{ wordBreak: 'break-all' }}>{detail.error_detail}</span>}
                  </div>
                )}
                <div className="flex gap-1.5 items-center flex-wrap">
                  <span className="text-tertiary flex items-center gap-0.5"><ServerCog size={11} /> 资源：</span>
                  <ResourceBar res={detail.resource_start} />
                  <ChevronRight size={11} className="text-tertiary" />
                  <ResourceBar res={detail.resource_end} />
                </div>
              </div>
              {/* 卡住定位卡 */}
              <StallAnalysisCard detail={detail} />
              {/* 节点链 */}
              <NodeTimeline detail={detail} />
            </>
          ) : (
            <div className="text-secondary text-sm" style={{ padding: 'var(--space-6)', textAlign: 'center' }}>
              {flows.length > 0 ? '选择左侧流程查看节点链执行详情' : '暂无流程数据'}
            </div>
          )}
        </div>
      </div>
    </>
  );
};

export default FlowTraceView;
