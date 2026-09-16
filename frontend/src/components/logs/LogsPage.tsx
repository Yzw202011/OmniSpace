/* ==========================================================================
 * LogsPage.tsx —— 系统日志面板（/logs，2026-08-21 日志可视化裁定）
 * --------------------------------------------------------------------------
 * 全项目统一日志的可视化入口，三视图：
 *   - 事件时间线：大白话事件（模型加载/热切换/模块资源释放/训练/异常），
 *     供普通用户理解"系统正在发生什么"；级别/模块/关键词筛选 + 24h 趋势
 *   - 执行流程：功能执行流程可视化面板（2026-08-22）——流程图展示模块间
 *     交互、时间轴追踪完整执行路径（FlowPanel）
 *   - 技术日志：原始日志尾部（backend.log / error.log / vllm-server.log），
 *     供开发者诊断
 * 保留策略：所有日志仅保存 30 天，超期自动清除（也可手动触发清理）。
 * 数据源：GET /logs/*（src/api/logs.py → services/event_log.py）。
 * ========================================================================== */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ScrollText,
  RefreshCw,
  CheckCircle2,
  Info,
  AlertTriangle,
  XCircle,
  Search,
  Trash2,
  Terminal,
  ListTodo,
  ChevronDown,
  ChevronUp,
  Clock,
  GitBranch,
  Download,
  Activity,
} from 'lucide-react';
import { FlowPanel } from './FlowPanel';
import {
  ErrorSummaryCard,
  ExportDiagnosticsDialog,
  ResourceChart,
} from './LogSupportWidgets';
import {
  queryEvents,
  getEventStats,
  listEventModules,
  getRawLogTail,
  triggerLogCleanup,
  type EventLevel,
  type LogEvent,
  type EventStats,
} from '@/services/logApi';
import { useAppStore } from '@/stores/useAppStore';

/** 级别展示配置（图标 + 中文 + 样式类） */
const LEVEL_META: Record<EventLevel, { icon: React.ComponentType<{ size?: number }>; label: string; cls: string }> = {
  success: { icon: CheckCircle2, label: '成功', cls: 'badge success' },
  info: { icon: Info, label: '信息', cls: 'badge info' },
  warning: { icon: AlertTriangle, label: '注意', cls: 'badge warning' },
  error: { icon: XCircle, label: '错误', cls: 'badge danger' },
};

/** 模块中文名（大白话映射；未知模块原样展示） */
const MODULE_NAMES: Record<string, string> = {
  system: '系统',
  dialog: 'AI 对话',
  vllm: '推理引擎',
  models: '模型管理',
  training: '知识训练',
  paint: 'AI 绘画',
  learn: '知识学习',
  // 自愈批2：API 事件日志自动兜底新增模块标签的中文名（与后端
  // event_log_auto._MODULE_ZH 镜像；缺失时页面回退显示原始标签）
  manga: '漫剧',
  knowledge: '知识库',
  browser: '浏览器',
  voice: '语音',
  style: '风格',
  vision: '视觉工具',
  hardware: '硬件',
  license: '激活',
  novel: '小说',
  cloud: '云端',
  frontend: '前端',
};

/** 自动刷新间隔（毫秒） */
const AUTO_REFRESH_MS = 15_000;

/** 格式化时间为 MM-dd HH:mm:ss */
function fmtTime(ts: string): string {
  const t = ts.replace('T', ' ');
  return t.length > 19 ? t.slice(5, 19) : t.slice(5);
}

/** 单条事件卡片（friendly 主文案 + 可展开技术详情） */
const EventRow: React.FC<{ ev: LogEvent }> = ({ ev }) => {
  const [open, setOpen] = useState(false);
  const meta = LEVEL_META[ev.level] ?? LEVEL_META.info;
  const Icon = meta.icon;
  const levelColor =
    ev.level === 'error' ? 'var(--color-danger)'
    : ev.level === 'warning' ? 'var(--color-warning)'
    : ev.level === 'success' ? 'var(--color-success)'
    : 'var(--color-primary)';

  return (
    <div
      className="card hoverable"
      style={{ padding: 'var(--space-3) var(--space-4)', marginBottom: 'var(--space-2)' }}
    >
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          style={{
            color: levelColor,
            flexShrink: 0,
            marginTop: 2,
            display: 'flex',
          }}
        >
          <Icon size={16} />
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={meta.cls}>{meta.label}</span>
            <span className="badge" style={{ background: 'var(--color-primary-50)' }}>
              {MODULE_NAMES[ev.module] ?? ev.module}
            </span>
            {typeof ev.duration_ms === 'number' && (
              <span className="text-tertiary flex items-center gap-1" style={{ fontSize: 'var(--font-size-xs)' }}>
                <Clock size={11} aria-hidden="true" />
                {(ev.duration_ms / 1000).toFixed(1)}s
              </span>
            )}
          </div>
          <div className="text-sm mt-1.5" style={{ lineHeight: 'var(--line-height-relaxed)' }}>
            {ev.friendly}
          </div>
          {(ev.detail || ev.event) && (
            <button
              type="button"
              className="text-tertiary flex items-center gap-1 mt-1.5"
              style={{
                fontSize: 'var(--font-size-xs)',
                cursor: 'pointer',
                background: 'none',
                border: 'none',
                padding: 0,
              }}
              onClick={() => setOpen((v) => !v)}
            >
              {open ? <ChevronUp size={12} aria-hidden="true" /> : <ChevronDown size={12} aria-hidden="true" />}
              技术详情
            </button>
          )}
          {open && (
            <pre
              className="mono"
              style={{
                marginTop: 'var(--space-2)',
                padding: 'var(--space-2) var(--space-3)',
                background: 'var(--color-bg-secondary, var(--color-primary-50))',
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
                `时间: ${ev.ts}`,
              ]
                .filter(Boolean)
                .join('\n')}
            </pre>
          )}
        </div>
        <span className="text-tertiary mono" style={{ fontSize: 'var(--font-size-xs)', flexShrink: 0 }}>
          {fmtTime(ev.ts)}
        </span>
      </div>
    </div>
  );
};

export const LogsPage: React.FC = () => {
  // ── 事件时间线状态 ──
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<EventStats | null>(null);
  const [modules, setModules] = useState<string[]>([]);
  const [level, setLevel] = useState<EventLevel | ''>('');
  const [module, setModule] = useState('');
  const [days, setDays] = useState(7);
  const [keyword, setKeyword] = useState('');
  const [keywordInput, setKeywordInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [tab, setTab] = useState<'events' | 'flows' | 'raw' | 'resource'>('events');
  // 导出诊断包弹窗（2026-09-01 方案 C）
  const [exportOpen, setExportOpen] = useState(false);

  // ── 技术日志状态 ──
  const [rawName, setRawName] = useState('backend.log');
  const [rawLines, setRawLines] = useState<string[]>([]);
  const [rawLoading, setRawLoading] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  // 原始日志过滤（方案 C：关键词 ± 上下文 / 级别）
  const [rawKeywordInput, setRawKeywordInput] = useState('');
  const [rawKeyword, setRawKeyword] = useState('');
  const [rawLevel, setRawLevel] = useState('');

  const queryRef = useRef({ level, module, days, keyword });

  /** 拉取事件列表 + 统计 */
  const fetchEvents = useCallback(async () => {
    setLoading(true);
    const { showToast } = useAppStore.getState();
    try {
      const q = queryRef.current;
      const [res, st, mods] = await Promise.all([
        queryEvents({
          limit: 200,
          days: q.days,
          level: q.level || undefined,
          module: q.module || undefined,
          keyword: q.keyword || undefined,
        }),
        getEventStats(q.days),
        listEventModules(),
      ]);
      setEvents(res.items);
      setTotal(res.total);
      setStats(st);
      setModules(mods.modules);
    } catch {
      showToast('日志查询失败：后端服务可能未启动', 'error');
    } finally {
      setLoading(false);
    }
  }, []);

  /** 拉取原始日志尾部（含过滤参数：关键词/级别） */
  const fetchRaw = useCallback(
    async (name: string, filters?: { keyword?: string; level?: string }) => {
      setRawLoading(true);
      const { showToast } = useAppStore.getState();
      try {
        const res = await getRawLogTail(name, 300, filters);
        setRawLines(res.lines);
        if (!res.exists) {
          showToast(`日志文件 ${name} 还不存在（可能后端刚启动）`, 'info');
        }
      } catch {
        showToast('原始日志读取失败', 'error');
      } finally {
        setRawLoading(false);
      }
    },
    [],
  );

  // 初次加载 + 轮询刷新（cleanup 清除定时器）
  useEffect(() => {
    void fetchEvents();
    void fetchRaw(rawName);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    queryRef.current = { level, module, days, keyword };
    void fetchEvents();
  }, [level, module, days, keyword, fetchEvents]);

  // 自动刷新（15s；切到技术日志 tab 时暂停）
  useEffect(() => {
    if (!autoRefresh || tab !== 'events') return;
    const t = window.setInterval(() => void fetchEvents(), AUTO_REFRESH_MS);
    return () => window.clearInterval(t);
  }, [autoRefresh, tab, fetchEvents]);

  // 切换技术日志文件 / 过滤条件变化时拉取
  useEffect(() => {
    if (tab === 'raw') {
      void fetchRaw(rawName, {
        keyword: rawKeyword || undefined,
        level: rawLevel || undefined,
      });
    }
  }, [rawName, rawKeyword, rawLevel, tab, fetchRaw]);

  /** 手动清理 30 天过期日志 */
  const handleCleanup = async () => {
    setCleanupBusy(true);
    const { showToast } = useAppStore.getState();
    try {
      const r = await triggerLogCleanup();
      showToast(
        `清理完成：删除 ${r.deleted_count} 个过期文件，释放 ${r.freed_mb.toFixed(1)}MB 磁盘`,
        'success',
      );
      void fetchEvents();
    } catch {
      showToast('清理失败：后端服务可能未启动', 'error');
    } finally {
      setCleanupBusy(false);
    }
  };

  /** 搜索提交（Enter / 按钮） */
  const submitKeyword = () => setKeyword(keywordInput.trim());

  const maxHourly = Math.max(1, ...(stats?.hourly_24h ?? []).map((h) => h.count));

  return (
    <div className="page logs-page">
      <h1 className="page-title">
        <ScrollText size={20} aria-hidden="true" /> 系统日志
      </h1>
      <p className="page-subtitle">
        全项目运行日志：大白话事件时间线 + 技术日志诊断（仅保留 30 天，超期自动清除）
      </p>

      {/* 视图切换 + 操作 */}
      <div className="flex items-center gap-2 mt-4 flex-wrap">
        <button
          type="button"
          className={`btn ${tab === 'events' ? 'btn-primary' : 'btn-ghost'}`}
          style={{ padding: '6px 14px', fontSize: 'var(--font-size-sm)' }}
          onClick={() => setTab('events')}
        >
          <ListTodo size={14} aria-hidden="true" /> 事件时间线
        </button>
        <button
          type="button"
          className={`btn ${tab === 'flows' ? 'btn-primary' : 'btn-ghost'}`}
          style={{ padding: '6px 14px', fontSize: 'var(--font-size-sm)' }}
          onClick={() => setTab('flows')}
        >
          <GitBranch size={14} aria-hidden="true" /> 执行流程
        </button>
        <button
          type="button"
          className={`btn ${tab === 'raw' ? 'btn-primary' : 'btn-ghost'}`}
          style={{ padding: '6px 14px', fontSize: 'var(--font-size-sm)' }}
          onClick={() => setTab('raw')}
        >
          <Terminal size={14} aria-hidden="true" /> 技术日志
        </button>
        <button
          type="button"
          className={`btn ${tab === 'resource' ? 'btn-primary' : 'btn-ghost'}`}
          style={{ padding: '6px 14px', fontSize: 'var(--font-size-sm)' }}
          onClick={() => setTab('resource')}
        >
          <Activity size={14} aria-hidden="true" /> 资源曲线
        </button>
        <span className="flex-1" />
        <button
          type="button"
          className="btn btn-primary"
          style={{ padding: '6px 14px', fontSize: 'var(--font-size-sm)' }}
          onClick={() => setExportOpen(true)}
          title="导出 zip 诊断包：事件 + 失败流程 + 原始日志 + 硬件快照（可脱敏）"
        >
          <Download size={14} aria-hidden="true" /> 导出诊断包
        </button>
        <label className="text-tertiary flex items-center gap-1.5" style={{ fontSize: 'var(--font-size-xs)', cursor: 'pointer' }}>
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
          自动刷新
        </label>
        <button
          type="button"
          className="btn btn-ghost"
          style={{ padding: '6px 14px', fontSize: 'var(--font-size-sm)' }}
          disabled={loading || rawLoading || tab === 'flows' || tab === 'resource'}
          title={tab === 'flows' ? '执行流程面板有独立刷新（10s 自动）' : tab === 'resource' ? '资源曲线自动刷新（30s）' : '刷新'}
          onClick={() =>
            tab === 'events'
              ? void fetchEvents()
              : void fetchRaw(rawName, {
                  keyword: rawKeyword || undefined,
                  level: rawLevel || undefined,
                })
          }
        >
          <RefreshCw size={14} aria-hidden="true" className={loading || rawLoading ? 'animate-spin' : ''} /> 刷新
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          style={{ padding: '6px 14px', fontSize: 'var(--font-size-sm)' }}
          disabled={cleanupBusy}
          onClick={() => void handleCleanup()}
          title="删除超过 30 天的旧日志文件"
        >
          <Trash2 size={14} aria-hidden="true" /> 清理过期日志
        </button>
      </div>

      {tab === 'events' ? (
        <>
          {/* 最近异常聚合（方案 C：错误面板，一眼看出哪类错最多） */}
          <ErrorSummaryCard />
          {/* 统计卡 */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-4">
            {(
              [
                { label: '总事件', value: stats?.total ?? 0, color: 'var(--color-primary)', icon: ListTodo },
                { label: '成功', value: stats?.by_level.success ?? 0, color: 'var(--color-success)', icon: CheckCircle2 },
                { label: '注意', value: stats?.by_level.warning ?? 0, color: 'var(--color-warning)', icon: AlertTriangle },
                { label: '错误', value: stats?.by_level.error ?? 0, color: 'var(--color-danger)', icon: XCircle },
              ] as const
            ).map((c) => (
              <div key={c.label} className="card hoverable" style={{ padding: 'var(--space-3) var(--space-4)' }}>
                <div className="flex items-center gap-2 text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
                  <c.icon size={13} aria-hidden="true" style={{ color: c.color }} />
                  {c.label}
                </div>
                <div className="mt-1" style={{ fontSize: 22, fontWeight: 'var(--font-weight-semibold)', color: c.color }}>
                  {c.value}
                </div>
              </div>
            ))}
          </div>

          {/* 24h 趋势（迷你柱状图） + 模块分布 */}
          <div className="card hoverable mt-3" style={{ padding: 'var(--space-4)' }}>
            <h3 className="card-title">
              <Clock size={16} aria-hidden="true" /> 最近 24 小时事件趋势
            </h3>
            <div className="flex items-end gap-1 mt-3" style={{ height: 96 }}>
              {(stats?.hourly_24h ?? []).map((h) => (
                <div
                  key={h.hour}
                  title={`${h.hour} · ${h.count} 条`}
                  style={{
                    flex: 1,
                    minWidth: 6,
                    height: '100%',
                    background: 'var(--color-primary)',
                    opacity: h.count > 0 ? 0.85 : 0.25,
                    borderRadius: 2,
                    transform: `scaleY(${Math.max(4, (h.count / maxHourly) * 100) / 100})`,
                    transformOrigin: 'bottom',
                    transition: 'transform var(--transition-fast)',
                  }}
                />
              ))}
            </div>
            {stats && stats.hourly_24h.length > 0 && (
              <div
                className="flex justify-between text-tertiary mono mt-1"
                style={{ fontSize: 'var(--font-size-xs)' }}
              >
                <span>{stats.hourly_24h[0].hour}</span>
                <span>{stats.hourly_24h[stats.hourly_24h.length - 1].hour}</span>
              </div>
            )}
            {stats && stats.by_module.length > 0 && (
              <div className="flex items-center gap-2 flex-wrap mt-3">
                <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
                  模块分布：
                </span>
                {stats.by_module.map((m) => (
                  <span key={m.module} className="badge" style={{ background: 'var(--color-primary-50)', fontSize: 'var(--font-size-xs)' }}>
                    {MODULE_NAMES[m.module] ?? m.module} {m.count}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* 筛选栏 */}
          <div className="card hoverable mt-3" style={{ padding: 'var(--space-3) var(--space-4)' }}>
            <div className="flex items-center gap-2 flex-wrap">
              {(['', 'info', 'success', 'warning', 'error'] as const).map((lv) => (
                <button
                  key={lv || 'all'}
                  type="button"
                  className={`btn ${level === lv ? 'btn-primary' : 'btn-ghost'}`}
                  style={{ padding: '4px 12px', fontSize: 'var(--font-size-xs)' }}
                  onClick={() => setLevel(lv)}
                >
                  {lv ? LEVEL_META[lv].label : '全部级别'}
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
                  <option key={m} value={m}>
                    {MODULE_NAMES[m] ?? m}
                  </option>
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
                  placeholder="搜索日志内容…"
                  value={keywordInput}
                  onChange={(e) => setKeywordInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') submitKeyword();
                  }}
                />
              </span>
            </div>
          </div>

          {/* 事件列表（限高+竖向滚动条：200 条卡片全铺开曾把页面撑到
              近 2 万像素，统计/筛选区被推离视野——2026-09-12 用户反馈） */}
          <div
            className="mt-3"
            style={{
              maxHeight: '68vh',
              overflowY: 'auto',
              paddingRight: 4,
              scrollbarGutter: 'stable',
            }}
          >
            <div className="text-tertiary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>
              共 {total} 条事件（新 → 旧，最多展示 200 条）
            </div>
            {loading && events.length === 0 && (
              <div className="card" style={{ padding: 'var(--space-6)', textAlign: 'center' }}>
                <div className="text-secondary text-sm">加载中…</div>
              </div>
            )}
            {!loading && events.length === 0 && (
              <div className="card" style={{ padding: 'var(--space-6)', textAlign: 'center' }}>
                <div className="text-secondary text-sm">
                  {total === 0
                    ? '还没有日志事件。用一用 AI 对话、切换模型或跑一次训练，这里就会出现记录'
                    : '当前筛选条件下没有匹配的事件，试试放宽筛选'}
                </div>
              </div>
            )}
            {events.map((ev, i) => (
              <EventRow key={`${ev.ts}-${ev.event}-${i}`} ev={ev} />
            ))}
          </div>
        </>
      ) : tab === 'flows' ? (
        /* ── 执行流程可视化面板（流程图 + 时间轴 + 筛选 + 自动刷新） ── */
        <FlowPanel />
      ) : tab === 'resource' ? (
        /* ── 资源占用曲线（方案 C：显存/内存 30s 采样趋势） ── */
        <ResourceChart />
      ) : (
        /* ── 技术日志视图（方案 C：白名单 6 文件 + 关键词/级别过滤） ── */
        <div className="card hoverable mt-4" style={{ padding: 'var(--space-4)' }}>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="card-title">
              <Terminal size={16} aria-hidden="true" /> 原始日志
            </h3>
            <span className="flex-1" />
            {['backend.log', 'error.log', 'vllm-server.log', 'boot.log', 'comfyui_boot.log', 'launcher.log'].map((name) => (
              <button
                key={name}
                type="button"
                className={`btn ${rawName === name ? 'btn-primary' : 'btn-ghost'}`}
                style={{ padding: '4px 12px', fontSize: 'var(--font-size-xs)' }}
                onClick={() => setRawName(name)}
              >
                {name}
              </button>
            ))}
          </div>
          {/* 过滤行：关键词（Enter 提交）+ 级别 */}
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <div className="flex items-center gap-1.5 flex-1" style={{ minWidth: 220 }}>
              <Search size={13} className="text-tertiary" aria-hidden="true" />
              <input
                className="input"
                style={{ flex: 1, padding: '4px 10px', fontSize: 'var(--font-size-xs)' }}
                placeholder="关键词过滤（Enter 应用，命中行±3 行上下文）"
                value={rawKeywordInput}
                onChange={(e) => setRawKeywordInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') setRawKeyword(rawKeywordInput.trim());
                }}
              />
            </div>
            {['', 'ERROR', 'WARNING', 'INFO'].map((lv) => (
              <button
                key={lv || 'all'}
                type="button"
                className={`btn ${rawLevel === lv ? 'btn-primary' : 'btn-ghost'}`}
                style={{ padding: '3px 10px', fontSize: 'var(--font-size-xs)' }}
                onClick={() => setRawLevel(lv)}
              >
                {lv || '全部级别'}
              </button>
            ))}
          </div>
          <pre
            className="mono"
            style={{
              marginTop: 'var(--space-3)',
              padding: 'var(--space-3)',
              background: 'var(--color-bg-secondary, var(--color-primary-50))',
              borderRadius: 'var(--radius-sm)',
              fontSize: 'var(--font-size-xs)',
              lineHeight: 'var(--line-height-relaxed)',
              maxHeight: 560,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              color: 'var(--color-text-secondary)',
            }}
          >
            {rawLoading
              ? '加载中…'
              : rawLines.length > 0
                ? rawLines.join('\n')
                : rawKeyword || rawLevel
                  ? '（无命中行——换个关键词或级别试试）'
                  : '（暂无内容）'}
          </pre>
          <div className="text-tertiary mt-2" style={{ fontSize: 'var(--font-size-xs)' }}>
            技术日志面向开发者：含后端/启动链/引擎日志，同样遵循 30 天自动清除。
          </div>
        </div>
      )}
      {/* 导出诊断包弹窗（方案 C） */}
      {exportOpen && <ExportDiagnosticsDialog onClose={() => setExportOpen(false)} />}
    </div>
  );
};

export default LogsPage;
// 本项目仅供学习使用，商业授权请+Q 3559331368
