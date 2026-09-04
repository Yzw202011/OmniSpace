/* ==========================================================================
 * FlowPanel.tsx —— 功能执行流程可视化面板（2026-08-22 / 08-23 追踪升级）
 * --------------------------------------------------------------------------
 * 系统日志模块的第三个视图：把各模块功能执行（模型加载/热切换/资源
 * 释放/训练等跨模块链路）聚合为「流程」直观展示。
 *   - 双数据源：「实时追踪」（flow_trace 精确 flow_id 节点链，默认；
 *     含卡住定位/资源快照/输入输出，2026-08-23 流程记录机制优化）
 *     与「事件聚合」（时间邻近性启发式，兼容无埋点的历史数据）
 *   - 事件聚合模式：左栏流程列表（状态/摘要/跨模块/耗时）+ 主区泳道
 *     流程图或甘特时间轴双视图；级别/模块/时间/关键词筛选
 *   - 实时：10s 自动刷新 + 手动刷新；节点/行点击查看执行结果与异常详情
 * ========================================================================== */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  GitBranch,
  RefreshCw,
  Search,
  XCircle,
  ZoomIn,
} from 'lucide-react';
import {
  queryFlows,
  listEventModules,
  type EventLevel,
  type ExecutionFlow,
} from '@/services/logApi';
import { useAppStore } from '@/stores/useAppStore';
import { FlowGraph } from './FlowGraph';
import { FlowTimeline } from './FlowTimeline';
import { FlowTraceView } from './FlowTraceView';
import {
  LEVEL_COLORS,
  STATUS_COLORS,
  fmtDuration,
  fmtTime,
  moduleColor,
  moduleName,
} from './flowShared';

/** 自动刷新间隔（毫秒） */
const AUTO_REFRESH_MS = 10_000;

/** 流程状态图标 */
function StatusIcon({ status }: { status: ExecutionFlow['status'] }) {
  const color = STATUS_COLORS[status];
  if (status === 'error') return <span style={{ color, display: 'flex' }}><XCircle size={15} /></span>;
  if (status === 'warning') return <span style={{ color, display: 'flex' }}><AlertTriangle size={15} /></span>;
  return <span style={{ color, display: 'flex' }}><CheckCircle2 size={15} /></span>;
}

/** 节点详情卡（执行结果 + 异常信息） */
const NodeDetail: React.FC<{ flow: ExecutionFlow; idx: number }> = ({ flow, idx }) => {
  const ev = flow.events[idx];
  const color = LEVEL_COLORS[ev.level] ?? LEVEL_COLORS.info;
  return (
    <div
      className="card hoverable"
      style={{
        padding: 'var(--space-3) var(--space-4)',
        marginTop: 'var(--space-2)',
        borderLeft: `3px solid ${color}`,
      }}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <span style={{ color, display: 'flex' }}><Activity size={14} /></span>
        <span style={{ fontWeight: 'var(--font-weight-semibold)', fontSize: 'var(--font-size-sm)' }}>
          第 {idx + 1} 步 · {ev.category}
        </span>
        <span className="badge" style={{ background: 'var(--color-primary-50)', color: moduleColor(ev.module), fontSize: 'var(--font-size-xs)' }}>
          {moduleName(ev.module)}
        </span>
        {typeof ev.duration_ms === 'number' && ev.duration_ms > 0 && (
          <span className="text-tertiary flex items-center gap-1" style={{ fontSize: 'var(--font-size-xs)' }}>
            <Clock size={11} /> {fmtDuration(ev.duration_ms)}
          </span>
        )}
        <span className="flex-1" />
        <span className="text-tertiary mono" style={{ fontSize: 'var(--font-size-xs)' }}>{ev.ts}</span>
      </div>
      <div className="text-sm mt-1.5" style={{ color: 'var(--color-text-primary)' }}>{ev.friendly}</div>
      {(ev.detail || ev.trace_id) && (
        <pre
          className="mono"
          style={{
            marginTop: 'var(--space-2)',
            padding: 'var(--space-2) var(--space-3)',
            background: 'var(--color-surface-secondary, var(--color-bg))',
            borderRadius: 'var(--radius-sm)',
            fontSize: 'var(--font-size-xs)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
            color: 'var(--color-text-secondary)',
          }}
        >
          {[
            `事件: ${ev.event}`,
            ev.detail ? `详情: ${ev.detail}` : null,
            ev.trace_id ? `追踪: ${ev.trace_id}` : null,
          ]
            .filter(Boolean)
            .join('\n')}
        </pre>
      )}
    </div>
  );
};

export const FlowPanel: React.FC = () => {
  // ── 数据源切换：实时追踪（精确 flow_id） / 事件聚合（历史兼容） ──
  const [source, setSource] = useState<'trace' | 'events'>('trace');

  // ── 数据状态 ──
  const [flows, setFlows] = useState<ExecutionFlow[]>([]);
  const [total, setTotal] = useState(0);
  const [modules, setModules] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string>('');
  const [loadError, setLoadError] = useState(false);

  // ── 筛选状态 ──
  const [level, setLevel] = useState<EventLevel | ''>('');
  const [module, setModule] = useState('');
  const [days, setDays] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [keywordInput, setKeywordInput] = useState('');

  // ── 视图状态 ──
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [view, setView] = useState<'graph' | 'timeline'>('graph');
  const [selectedFlowId, setSelectedFlowId] = useState('');
  const [selectedNodeIdx, setSelectedNodeIdx] = useState<number | null>(null);

  const queryRef = useRef({ level, module, days, keyword });
  queryRef.current = { level, module, days, keyword };

  /** 拉取流程列表 */
  const fetchFlows = useCallback(async () => {
    setLoading(true);
    const { showToast } = useAppStore.getState();
    try {
      const q = queryRef.current;
      const [res, mods] = await Promise.all([
        queryFlows({
          days: q.days,
          level: q.level || undefined,
          module: q.module || undefined,
          keyword: q.keyword || undefined,
          limit: 50,
        }),
        listEventModules(),
      ]);
      setFlows(res.flows);
      setTotal(res.total);
      setModules(mods.modules);
      setLoadError(false);
      setLastRefresh(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      // 选中保持：原选中流程还在则保留，否则选最新一条
      setSelectedFlowId((prevId) => {
        if (prevId && res.flows.some((f) => f.flow_id === prevId)) return prevId;
        setSelectedNodeIdx(null);
        return res.flows[0]?.flow_id ?? '';
      });
    } catch {
      setLoadError(true);
      showToast('执行流程查询失败：后端服务可能未启动', 'error');
    } finally {
      setLoading(false);
    }
  }, []);

  // 初次加载
  useEffect(() => { void fetchFlows(); }, [fetchFlows]);

  // 筛选变化 → 重新拉取
  useEffect(() => { void fetchFlows(); }, [level, module, days, keyword, fetchFlows]);

  // 自动刷新（10s）
  useEffect(() => {
    if (!autoRefresh) return;
    const t = window.setInterval(() => void fetchFlows(), AUTO_REFRESH_MS);
    return () => window.clearInterval(t);
  }, [autoRefresh, fetchFlows]);

  const selectedFlow = flows.find((f) => f.flow_id === selectedFlowId) ?? null;

  // 实时追踪模式（2026-08-23 默认）：独立视图，自带筛选/刷新
  if (source === 'trace') {
    return (
      <>
        <div className="flex items-center gap-2" style={{ marginTop: 'var(--space-3)' }}>
          <button
            type="button"
            className="btn btn-primary"
            style={{ padding: '4px 14px', fontSize: 'var(--font-size-xs)' }}
          >
            实时追踪
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            style={{ padding: '4px 14px', fontSize: 'var(--font-size-xs)' }}
            onClick={() => setSource('events')}
          >
            事件聚合（历史兼容）
          </button>
          <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
            每次功能执行唯一流程 ID · 节点链输入输出/耗时/资源快照 · 卡住定位
          </span>
        </div>
        <FlowTraceView />
      </>
    );
  }

  return (
    <>
      {/* 数据源切换 */}
      <div className="flex items-center gap-2" style={{ marginTop: 'var(--space-3)' }}>
        <button
          type="button"
          className="btn btn-ghost"
          style={{ padding: '4px 14px', fontSize: 'var(--font-size-xs)' }}
          onClick={() => setSource('trace')}
        >
          实时追踪
        </button>
        <button
          type="button"
          className="btn btn-primary"
          style={{ padding: '4px 14px', fontSize: 'var(--font-size-xs)' }}
        >
          事件聚合（历史兼容）
        </button>
        <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
          事件日志按时间邻近性聚合的流程链路（无埋点历史数据也可查看）
        </span>
      </div>

      {/* 筛选栏 */}
      <div className="card hoverable" style={{ padding: 'var(--space-3) var(--space-4)', marginTop: 'var(--space-3)' }}>
        <div className="flex items-center gap-2 flex-wrap">
          {(['', 'info', 'success', 'warning', 'error'] as const).map((lv) => (
            <button
              key={lv || 'all'}
              type="button"
              className={`btn ${level === lv ? 'btn-primary' : 'btn-ghost'}`}
              style={{ padding: '4px 12px', fontSize: 'var(--font-size-xs)' }}
              onClick={() => setLevel(lv)}
            >
              {lv === '' ? '全部状态' : lv === 'success' ? '成功' : lv === 'warning' ? '注意' : lv === 'error' ? '错误' : '信息'}
            </button>
          ))}
          <select
            className="input"
            style={{ width: 'auto', padding: '4px 8px', fontSize: 'var(--font-size-xs)' }}
            value={module}
            onChange={(e) => setModule(e.target.value)}
            aria-label="按模块筛选"
          >
            <option value="">全部模块</option>
            {modules.map((m) => (
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
              placeholder="搜索流程内容…"
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
            onClick={() => void fetchFlows()}
          >
            <RefreshCw size={13} aria-hidden="true" className={loading ? 'animate-spin' : ''} /> 刷新
          </button>
        </div>
        {lastRefresh && (
          <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', marginTop: 6 }}>
            {loadError ? '上次刷新失败，正在重试…' : `上次刷新 ${lastRefresh} · 每 10s 自动刷新`}
          </div>
        )}
      </div>

      {/* 主体：左流程列表 + 右视图 */}
      <div className="flex gap-3 mt-3" style={{ alignItems: 'stretch' }}>
        {/* 左：流程列表 */}
        <div className="card hoverable" style={{ width: 320, flexShrink: 0, padding: 0, display: 'flex', flexDirection: 'column', maxHeight: 620 }}>
          <div
            className="flex items-center gap-2"
            style={{ padding: 'var(--space-3) var(--space-4)', borderBottom: '1px solid var(--color-divider)' }}
          >
            <GitBranch size={14} aria-hidden="true" style={{ color: 'var(--color-primary)' }} />
            <span style={{ fontWeight: 'var(--font-weight-semibold)', fontSize: 'var(--font-size-sm)' }}>执行流程</span>
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
                {total === 0 ? '最近没有功能执行记录。切换模型、跑一次训练后，这里会出现流程' : '当前筛选下没有匹配的流程'}
              </div>
            )}
            {flows.map((f) => {
              const active = f.flow_id === selectedFlowId;
              return (
                <button
                  key={f.flow_id}
                  type="button"
                  onClick={() => {
                    setSelectedFlowId(f.flow_id);
                    setSelectedNodeIdx(null);
                  }}
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
                    <StatusIcon status={f.status} />
                    <span className="mono text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>{fmtTime(f.start_ts)}</span>
                    <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', marginLeft: 'auto' }}>
                      {f.event_count} 步
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
                    {f.summary}
                  </div>
                  <div className="flex items-center gap-1 mt-1" style={{ flexWrap: 'wrap' }}>
                    {f.modules.map((m) => (
                      <span key={m} className="badge" style={{ fontSize: 10, padding: '0 6px', color: moduleColor(m), background: 'var(--color-primary-50)' }}>
                        {moduleName(m)}
                      </span>
                    ))}
                    <span className="text-tertiary" style={{ fontSize: 10, marginLeft: 'auto' }}>
                      {f.span_ms > 0 ? fmtDuration(f.span_ms) : '瞬时'}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* 右：流程详情视图 */}
        <div className="card hoverable" style={{ flex: 1, minWidth: 0, padding: 'var(--space-3) var(--space-4)' }}>
          {selectedFlow ? (
            <>
              <div className="flex items-center gap-2 flex-wrap">
                <StatusIcon status={selectedFlow.status} />
                <span style={{ fontWeight: 'var(--font-weight-semibold)', fontSize: 'var(--font-size-sm)' }}>
                  {selectedFlow.summary}
                </span>
                <span className="text-tertiary mono" style={{ fontSize: 'var(--font-size-xs)' }}>
                  {fmtTime(selectedFlow.start_ts)} → {fmtTime(selectedFlow.end_ts)}
                </span>
                <span className="flex-1" />
                <button
                  type="button"
                  className={`btn ${view === 'graph' ? 'btn-primary' : 'btn-ghost'}`}
                  style={{ padding: '3px 12px', fontSize: 'var(--font-size-xs)' }}
                  onClick={() => { setView('graph'); setSelectedNodeIdx(null); }}
                >
                  <GitBranch size={12} /> 模块交互图
                </button>
                <button
                  type="button"
                  className={`btn ${view === 'timeline' ? 'btn-primary' : 'btn-ghost'}`}
                  style={{ padding: '3px 12px', fontSize: 'var(--font-size-xs)' }}
                  onClick={() => { setView('timeline'); setSelectedNodeIdx(null); }}
                >
                  <ZoomIn size={12} /> 时间轴
                </button>
              </div>
              <div style={{ marginTop: 'var(--space-3)' }}>
                {view === 'graph' ? (
                  <FlowGraph
                    key={selectedFlow.flow_id}
                    flow={selectedFlow}
                    selectedIdx={selectedNodeIdx}
                    onSelect={setSelectedNodeIdx}
                  />
                ) : (
                  <div style={{ maxHeight: 520, overflowY: 'auto', border: '1px solid var(--color-border-light)', borderRadius: 'var(--radius-md)' }}>
                    <FlowTimeline
                      flow={selectedFlow}
                      selectedIdx={selectedNodeIdx}
                      onSelect={setSelectedNodeIdx}
                    />
                  </div>
                )}
              </div>
              {selectedNodeIdx !== null && selectedFlow.events[selectedNodeIdx] && (
                <NodeDetail flow={selectedFlow} idx={selectedNodeIdx} />
              )}
            </>
          ) : (
            <div className="text-secondary text-sm" style={{ padding: 'var(--space-6)', textAlign: 'center' }}>
              {flows.length > 0 ? '选择左侧流程查看执行详情' : '暂无流程数据'}
            </div>
          )}
        </div>
      </div>
    </>
  );
};

export default FlowPanel;
