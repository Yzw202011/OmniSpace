/* ==========================================================================
 * OmniSpace AI —— 系统日志 API（2026-08-21 日志可视化裁定）
 * --------------------------------------------------------------------------
 * 对齐后端 backend/api/logs.py（/api/v1 前缀由 api.ts 拼接）：
 * - GET  /logs/events   重要事件分页查询（大白话时间线，新→旧）
 * - GET  /logs/stats    统计（级别分布/模块分布/24h 趋势/保留策略）
 * - GET  /logs/modules  已产生事件的模块列表
 * - GET  /logs/flows/trace        执行流程追踪列表（精确 flow_id 链路）
 * - GET  /logs/flows/trace/{id}   单流程全明细（节点链 + 卡住定位）
 * - GET  /logs/flows/trace-stats  追踪面板筛选项与状态计数
 * - GET  /logs/files    原始日志文件清单
 * - GET  /logs/raw      原始日志尾部（技术诊断）
 * - POST /logs/cleanup  手动触发 30 天过期清理
 * ========================================================================== */

import { get, post } from './api';
import type { QueryParams } from './api';

/** 事件级别（后端 event_log._LEVELS） */
export type EventLevel = 'info' | 'success' | 'warning' | 'error';

/** 单条重要事件（对齐后端 log_event 写入结构） */
export interface LogEvent {
  /** ISO8601 本地时间 */
  ts: string;
  level: EventLevel;
  /** 模块标识：system/dialog/vllm/models/training/... */
  module: string;
  /** 事件类型：model_loaded/hot_switch/... */
  event: string;
  /** 大白话描述（用户可读） */
  friendly: string;
  /** 技术详情（可选，开发者展开看） */
  detail?: string;
  /** 耗时毫秒（可选） */
  duration_ms?: number;
  /** 流程追踪 ID（可选；同一次跨模块功能执行的关联标识，后端 log_event 携带） */
  trace_id?: string;
}

/** 事件查询结果 */
export interface EventQueryResult {
  items: LogEvent[];
  total: number;
  has_more: boolean;
}

/* ==========================================================================
 * 执行流程可视化（2026-08-22）：事件流聚合为「一次功能执行的完整链路」
 * ========================================================================== */

/** 流程内单步事件（事件 + 后端附加的功能类型） */
export interface FlowEvent extends LogEvent {
  /** 功能类型（模型加载/模型切换/资源释放/...，后端 _event_category） */
  category: string;
  /** 流程追踪 ID（可选，新埋点才有；历史数据由启发式聚合） */
  trace_id?: string;
}

/** 流程状态：含 error → error；含 warning → warning；否则 ok */
export type FlowStatus = 'ok' | 'warning' | 'error';

/** 一条功能执行流程（跨模块交互的完整链路，events 旧→新执行序） */
export interface ExecutionFlow {
  flow_id: string;
  start_ts: string;
  end_ts: string;
  /** 首尾事件时间差（毫秒） */
  span_ms: number;
  /** 涉及模块（首次出现顺序） */
  modules: string[];
  /** 涉及功能类型 */
  categories: string[];
  status: FlowStatus;
  event_count: number;
  /** 各步骤耗时之和（毫秒；无耗时的事件不计） */
  total_duration_ms: number;
  /** 人话摘要（如「模型加载 · 3 模块 · 5 步 · 全部成功」） */
  summary: string;
  events: FlowEvent[];
}

/** 流程查询结果 */
export interface FlowQueryResult {
  flows: ExecutionFlow[];
  total: number;
}

/** 流程查询参数 */
export interface FlowQueryParams {
  days?: number;
  level?: EventLevel;
  module?: string;
  keyword?: string;
  limit?: number;
}

/* ==========================================================================
 * 执行流程追踪（2026-08-23 流程记录机制优化）
 * --------------------------------------------------------------------------
 * 数据源 flow_trace.db：每次功能执行唯一 flow_id，节点链含
 * 输入/输出摘要/状态/耗时/资源快照；stalled=卡住（无心跳超阈值）。
 * ========================================================================== */

/** 流程状态（含动态推导：stalled 卡住 / orphan 进程重启遗留） */
export type TraceStatus = 'running' | 'success' | 'error' | 'cancelled' | 'stalled' | 'orphan';

/** 资源快照（节点/流程粒度，CPU/内存/GPU） */
export interface ResourceSnapshot {
  cpu_percent: number;
  ram_percent: number;
  ram_used_gb: number;
  gpu?: {
    vram_used_mb: number;
    vram_total_mb: number;
    util_percent: number;
    temp_celsius: number;
  };
}

/** 流程节点（执行环节：模型加载/生成/落盘…） */
export interface FlowTraceNode {
  seq: number;
  node: string;
  module: string;
  status: string;
  started_at: string;
  duration_ms: number | null;
  input_summary: string;
  output_summary: string;
  detail: string;
  friendly: string;
  error: string;
  resource: ResourceSnapshot;
}

/** 流程列表项（含卡住秒数与最后活跃节点） */
export interface FlowTraceListItem {
  flow_id: string;
  module: string;
  feature: string;
  status: TraceStatus;
  /** 大白话功能描述（如「AI 对话：你好…」） */
  friendly: string;
  trigger: string;
  input_summary: string;
  output_summary: string;
  error_code: string;
  error_detail: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  node_count: number;
  resource_start: ResourceSnapshot;
  resource_end: ResourceSnapshot;
  /** running 且无心跳超阈值时为卡住秒数（非卡住为 0） */
  stalled_seconds: number;
  last_node: {
    seq: number;
    node: string;
    status: string;
    started_at: string;
    duration_ms: number | null;
    input_summary: string;
    output_summary: string;
    resource: ResourceSnapshot;
  } | null;
}

/** 单流程全明细（节点链 + 卡住定位分析） */
export interface FlowTraceDetail extends FlowTraceListItem {
  nodes: FlowTraceNode[];
  /** 卡住定位（仅 running/stalled 流程）：卡在哪/输入/最后输出/提示 */
  stall_analysis?: {
    where: string;
    node_input: string;
    node_last_output: string;
    node_status: string;
    node_running_ms: number | null;
    hint: string;
  };
}

/** 追踪查询结果 */
export interface FlowTraceQueryResult {
  flows: FlowTraceListItem[];
  total: number;
}

/** 追踪统计（筛选项 + 状态计数，含卡住数量） */
export interface FlowTraceStats {
  total: number;
  by_status: Record<TraceStatus, number>;
  modules: string[];
  features: string[];
  stall_threshold_s: number;
}

/** 追踪查询参数 */
export interface TraceQueryParams {
  module?: string;
  feature?: string;
  status?: TraceStatus;
  keyword?: string;
  /** 回溯天数（换算为后端 start Unix 时间戳） */
  days?: number;
  limit?: number;
  offset?: number;
}

/** 事件查询参数 */
export interface EventQueryParams {
  limit?: number;
  offset?: number;
  days?: number;
  level?: EventLevel;
  module?: string;
  keyword?: string;
}

/** 事件统计（对齐后端 event_stats） */
export interface EventStats {
  total: number;
  by_level: Record<EventLevel, number>;
  by_module: Array<{ module: string; count: number }>;
  hourly_24h: Array<{ hour: string; count: number }>;
  retention_days: number;
}

/** 原始日志文件信息 */
export interface RawLogFile {
  name: string;
  size_bytes: number;
  size_mb: number;
  modified: string;
}

/** 原始日志尾部内容 */
export interface RawLogTail {
  name: string;
  exists: boolean;
  total_lines: number;
  lines: string[];
}

/** 手动清理结果 */
export interface CleanupResult {
  deleted_count: number;
  deleted_files: string[];
  freed_mb: number;
  retention_days: number;
}

/** 查询重要事件（大白话时间线，新→旧） */
export function queryEvents(params: EventQueryParams = {}) {
  const query: QueryParams = {
    limit: params.limit ?? 200,
    offset: params.offset ?? 0,
    days: params.days ?? 7,
  };
  if (params.level) query.level = params.level;
  if (params.module) query.module = params.module;
  if (params.keyword) query.keyword = params.keyword;
  return get<EventQueryResult>('/logs/events', query);
}

/** 查询功能执行流程聚合（流程可视化面板，新→旧） */
export function queryFlows(params: FlowQueryParams = {}) {
  const query: QueryParams = {
    days: params.days ?? 1,
    limit: params.limit ?? 50,
  };
  if (params.level) query.level = params.level;
  if (params.module) query.module = params.module;
  if (params.keyword) query.keyword = params.keyword;
  return get<FlowQueryResult>('/logs/flows', query);
}

/** 查询执行流程追踪列表（精确 flow_id 链路，新→旧） */
export function queryFlowTraces(params: TraceQueryParams = {}) {
  const query: QueryParams = {
    limit: params.limit ?? 50,
    offset: params.offset ?? 0,
  };
  if (params.module) query.module = params.module;
  if (params.feature) query.feature = params.feature;
  if (params.status) query.status = params.status;
  if (params.keyword) query.keyword = params.keyword;
  if (params.days) {
    query.start = Math.floor((Date.now() - params.days * 86_400_000) / 1000);
  }
  return get<FlowTraceQueryResult>('/logs/flows/trace', query);
}

/** 单流程全明细：完整节点链 + 卡住定位分析（stall_analysis） */
export function getFlowTrace(flowId: string) {
  return get<FlowTraceDetail>(`/logs/flows/trace/${flowId}`);
}

/** 追踪面板筛选项与状态计数（模块清单 + 卡住数量） */
export function getFlowTraceStats() {
  return get<FlowTraceStats>('/logs/flows/trace-stats');
}

/** 查询事件统计（级别/模块分布 + 24h 趋势） */
export function getEventStats(days = 7) {
  return get<EventStats>('/logs/stats', { days });
}

/** 已产生事件的模块列表（筛选项动态生成） */
export function listEventModules() {
  return get<{ modules: string[] }>('/logs/modules');
}

/** 原始日志文件清单 */
export function listLogFiles() {
  return get<{ files: RawLogFile[]; retention_days: number }>('/logs/files');
}

/** 原始日志尾部（技术诊断；name 限白名单 backend.log/error.log/vllm-server.log） */
export function getRawLogTail(name: string, lines = 200) {
  return get<RawLogTail>('/logs/raw', { name, lines });
}

/** 手动触发 30 天过期清理 */
export function triggerLogCleanup() {
  return post<CleanupResult>('/logs/cleanup', {});
}

export default {
  queryEvents,
  queryFlows,
  queryFlowTraces,
  getFlowTrace,
  getFlowTraceStats,
  getEventStats,
  listEventModules,
  listLogFiles,
  getRawLogTail,
  triggerLogCleanup,
};
