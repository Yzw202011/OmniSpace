// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * flowShared.ts —— 执行流程可视化共享常量与工具
 * --------------------------------------------------------------------------
 * 模块名/模块配色/级别色（全部走 CSS 变量，双主题自适应）+ 时间与耗时格式化。
 * 数据源对齐 logApi.ts 的 FlowEvent / ExecutionFlow（后端 /logs/flows）。
 * ========================================================================== */

import type { EventLevel, FlowEvent, FlowStatus, TraceStatus } from '@/services/logApi';

/** 模块中文名（与 LogsPage 一致；未知模块原样展示） */
export const MODULE_NAMES: Record<string, string> = {
  system: '系统',
  dialog: 'AI 对话',
  vllm: '推理引擎',
  models: '模型管理',
  training: '知识训练',
  paint: 'AI 绘画',
  learn: '知识学习',
  video: '漫剧视频',
  storyboard: '漫剧分镜',
  director: '导演台（已下线）',
};

/** 模块泳道色（CSS 变量，双主题下均有定义） */
export const MODULE_COLORS: Record<string, string> = {
  dialog: 'var(--color-primary)',
  vllm: 'var(--color-accent)',
  models: 'var(--color-highlight)',
  system: 'var(--color-text-secondary)',
  training: 'var(--color-success)',
  paint: 'var(--color-primary-300)',
  learn: 'var(--color-accent-300)',
};

/** 未登记模块的兜底色 */
export const MODULE_COLOR_FALLBACK = 'var(--color-text-tertiary)';

/** 级别 → 颜色（SVG/内联样式用） */
export const LEVEL_COLORS: Record<EventLevel, string> = {
  success: 'var(--color-success)',
  info: 'var(--color-info)',
  warning: 'var(--color-warning)',
  error: 'var(--color-error)',
};

/** 流程状态 → 颜色 */
export const STATUS_COLORS: Record<FlowStatus, string> = {
  ok: 'var(--color-success)',
  warning: 'var(--color-warning)',
  error: 'var(--color-error)',
};

/** 追踪流程状态 → 中文名 */
export const TRACE_STATUS_NAMES: Record<TraceStatus, string> = {
  running: '运行中',
  success: '成功',
  error: '失败',
  cancelled: '已取消',
  stalled: '卡住',
  orphan: '中断',
};

/** 追踪流程状态 → 颜色（stalled 卡住=警示橙；orphan 进程重启遗留=灰紫） */
export const TRACE_STATUS_COLORS: Record<TraceStatus, string> = {
  running: 'var(--color-info)',
  success: 'var(--color-success)',
  error: 'var(--color-error)',
  cancelled: 'var(--color-text-secondary)',
  stalled: 'var(--color-warning)',
  orphan: 'var(--color-text-tertiary)',
};

export function moduleColor(m: string): string {
  return MODULE_COLORS[m] ?? MODULE_COLOR_FALLBACK;
}

export function moduleName(m: string): string {
  return MODULE_NAMES[m] ?? m;
}

/** ISO 时间 → "MM-dd HH:mm:ss" */
export function fmtTime(ts: string): string {
  const t = ts.replace('T', ' ');
  return t.length > 19 ? t.slice(5, 19) : t.slice(5);
}

/** ISO 时间 → "HH:mm:ss"（同日内更紧凑的展示） */
export function fmtClock(ts: string): string {
  const t = ts.replace('T', ' ');
  return t.length >= 19 ? t.slice(11, 19) : t.slice(11);
}

/** 毫秒 → 人话耗时（<1s 显 ms；<60s 显 s；否则 X 分 Y 秒） */
export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}分${s}秒`;
}

/** 事件短名：model_loaded → 加载完成（节点内文案，超出截断） */
const EVENT_SHORT_NAMES: Record<string, string> = {
  model_prewarm: '模型预热',
  model_loaded: '加载完成',
  model_load_failed: '加载失败',
  model_unloaded: '模型卸载',
  model_switching: '模型切换',
  gen_route_switch: '生图路由切换',
  keyframe_consistency_pass: '一致性校验通过',
  keyframe_consistency_retry: '一致性低分重抽',
  keyframe_consistency_retried: '一致性重抽完成',
  service_ready: '服务就绪',
  service_stopped: '服务停止',
  service_failed: '服务异常',
  start_cancelled: '启动取消',
  warmup_done: '预热完成',
  module_released: '资源释放',
  module_release_skipped: '释放跳过',
  logs_cleaned: '日志清理',
};

export function eventShortName(ev: FlowEvent): string {
  return EVENT_SHORT_NAMES[ev.event] ?? ev.event;
}
