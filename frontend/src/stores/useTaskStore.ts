/* ==========================================================================
 * OmniSpace AI v2.1 —— 全局任务状态（规格 §2.2 任务进度 + §5.3 WebSocket 通知）
 * --------------------------------------------------------------------------
 * 职责：
 *   - 全局任务列表（绘画/视频/训练/爬取等统一表示）
 *   - 进度跟踪（WS task_progress 实时推送）
 *   - 任务控制（暂停/恢复/取消/重排）
 * 统一 WS 消息：task_progress / task_preview / task_complete / task_error
 * ========================================================================== */

import { create } from 'zustand';
import type { OmniTask, WsStatus } from '@/types';
import { getWsHub } from '@/services/ws';
import { TaskBroadcastSchema } from '@/services/schema';
import { reportBgError } from '@/utils/errors';

/** WsHub 广播的原始载荷（各后端服务字段不完全一致，统一在此规整） */
type TaskBroadcastPayload = Record<string, unknown>;

/** 任务模块/类型中文标签（通知中心与任务列表显示用，禁止英文透出） */
export const TASK_TYPE_LABELS: Record<string, string> = {
  paint: 'AI 绘画',
  learn: '知识学习',
  system: '系统任务',
  task: '后台任务',
  text2img: '文生图',
  img2img: '图生图',
  video_gen: '视频生成',
  manga: '漫剧生成',
};

/** 规整后的任务事件 */
interface NormalizedTaskEvent {
  /** 任务 ID（兼容 id / task_id） */
  id: string;
  /** 进度 0~1（兼容 progress 0~1 或 0~100、percent 0~100） */
  progress?: number;
  /** 状态文本（running/done/error 等） */
  status?: string;
  /** 错误信息 */
  error?: string;
  /** 结果 URL */
  resultUrl?: string;
  /** 预览图 URL */
  previewUrl?: string;
  /** 来源模块（paint/learn/...） */
  module?: string;
}

/**
 * 规整 WsHub 广播载荷。
 * 后端各服务字段差异：draw 用 {task_id, percent, status}；
 * lora_training 用 {task_id, event, progress}；统一映射到 OmniTask 约定。
 * Zod 信封校验（批 3-3b）：非对象帧 reportBgError 后返回 null（调用方跳过），
 * 字段级守卫保持本函数原有 typeof 链——宁松勿严双层防线。
 * @returns 规整事件；载荷非法时返回 null
 */
function normalizeTaskEvent(data: TaskBroadcastPayload | null | unknown): NormalizedTaskEvent | null {
  const parsed = TaskBroadcastSchema.safeParse(data);
  if (!parsed.success) {
    reportBgError('task.broadcast', parsed.error.issues[0] ?? new Error('任务广播帧非法'));
    return null;
  }
  const d = parsed.data;
  const id =
    (typeof d.id === 'string' && d.id) ||
    (typeof d.task_id === 'string' && d.task_id) ||
    '';
  let progress: number | undefined;
  if (typeof d.progress === 'number') {
    progress = d.progress > 1 ? d.progress / 100 : d.progress;
  } else if (typeof d.percent === 'number') {
    progress = d.percent / 100;
  }
  return {
    id,
    progress,
    status: typeof d.status === 'string' ? d.status : undefined,
    error: typeof d.error === 'string' ? d.error : undefined,
    resultUrl:
      (typeof d.result_url === 'string' && d.result_url) ||
      (typeof d.url === 'string' && d.url) ||
      undefined,
    previewUrl: typeof d.preview_url === 'string' ? d.preview_url : undefined,
    module: typeof d.module === 'string' ? d.module : undefined,
  };
}

/** 任务状态 */
export interface TaskState {
  /** 全局任务列表 */
  tasks: OmniTask[];
  /** WS 连接状态 */
  wsStatus: WsStatus;
  /** 是否已初始化订阅 */
  subscribed: boolean;

  /* ------------------------------ 动作 ------------------------------ */
  /** 初始化 WS 订阅（幂等，组件挂载时调用一次） */
  init: () => void;
  /** 销毁订阅 */
  destroy: () => void;
  /** 更新或插入任务（按 id 合并） */
  upsertTask: (task: OmniTask) => void;
  /** 批量替换任务列表 */
  setTasks: (tasks: OmniTask[]) => void;
  /** 按 id 移除任务 */
  removeTask: (taskId: string) => void;
  /** 更新单个任务进度 */
  updateProgress: (taskId: string, progress: number, extra?: Partial<OmniTask>) => void;
  /** 标记任务完成 */
  completeTask: (taskId: string, resultUrl?: string) => void;
  /** 标记任务失败 */
  failTask: (taskId: string, error: string) => void;
}

/** 解订函数集合 */
let unsubscribers: Array<() => void> = [];

/**
 * 陈旧任务自动收敛阈值（2026-09-02 幽灵任务修复）：running/pending
 * 任务超过此时长无任何进度更新，视为已完成（后端漏发终态广播或后端
 * 中途重启的兜底——否则通知中心永久挂「进行中」幽灵任务）。
 * 15 分钟对所有真实任务都足够宽裕（关键帧逐镜 ~2min 一广播、视频
 * 轮询 2~10s 一刷、四视图逐视图数分钟一报）。
 */
const STALE_TASK_MS = 15 * 60 * 1000;
/** 陈旧收敛巡检周期 */
const STALE_SWEEP_INTERVAL_MS = 60 * 1000;
let staleSweeper: ReturnType<typeof setInterval> | null = null;

export const useTaskStore = create<TaskState>((set, get) => ({
  tasks: [],
  wsStatus: 'closed',
  subscribed: false,

  init: () => {
    if (get().subscribed) {
      return;
    }
    set({ subscribed: true });

    // 连接通用消息中枢 /ws（规格 §2.2：任务事件经 WsHub 广播下发；
    // 此前误用 /v1/hardware/realtime 硬件遥测通道，任务事件永远收不到）
    const conn = getWsHub();
    conn.connect();
    set({ wsStatus: conn.status });

    const offStatus = conn.onStatus((status) => {
      set({ wsStatus: status });
    });

    // task_progress：进度更新（status=done/error 时收敛为完成/失败；
    // 绘画等模块仅在 task_progress 内携带终态，不单独发 task_complete）
    const offProgress = conn.on<TaskBroadcastPayload>('task_progress', (data) => {
      const ev = normalizeTaskEvent(data);
      if (!ev) {
        return;
      }
      if (!ev.id) {
        return;
      }
      if (ev.status === 'done' || (ev.progress !== undefined && ev.progress >= 1)) {
        get().completeTask(ev.id, ev.resultUrl);
        return;
      }
      if (ev.status === 'error') {
        get().failTask(ev.id, ev.error || '任务执行失败');
        return;
      }
      // 广播先于本地注册到达时补一条占位任务，保证进度可见
      const exists = get().tasks.some((t) => t.id === ev.id);
      if (!exists) {
        get().upsertTask({
          id: ev.id,
          type: ev.module || 'task',
          name: TASK_TYPE_LABELS[ev.module || 'task'] || '后台任务',
          status: 'running',
          progress: ev.progress ?? 0,
        });
        return;
      }
      get().updateProgress(ev.id, ev.progress ?? 0);
    });

    // task_preview：预览图更新
    const offPreview = conn.on<TaskBroadcastPayload>('task_preview', (data) => {
      const ev = normalizeTaskEvent(data);
      if (!ev) {
        return;
      }
      if (ev.id && ev.previewUrl) {
        const existing = get().tasks.find((t) => t.id === ev.id);
        get().upsertTask({
          type: ev.module || 'task',
          status: 'running',
          progress: 0,
          ...existing,
          id: ev.id,
          preview_url: ev.previewUrl,
        });
      }
    });

    // task_complete：任务完成
    const offComplete = conn.on<TaskBroadcastPayload>('task_complete', (data) => {
      const ev = normalizeTaskEvent(data);
      if (!ev) {
        return;
      }
      if (ev.id) {
        get().completeTask(ev.id, ev.resultUrl);
      }
    });

    // task_error：任务失败
    const offError = conn.on<TaskBroadcastPayload>('task_error', (data) => {
      const ev = normalizeTaskEvent(data);
      if (!ev) {
        return;
      }
      if (ev.id) {
        get().failTask(ev.id, ev.error || '任务执行失败');
      }
    });

    unsubscribers = [offStatus, offProgress, offPreview, offComplete, offError];

    // 陈旧任务巡检（2026-09-02 幽灵任务兜底）：running/pending 超过
    // STALE_TASK_MS 无进度更新 → 标记完成。任何真实任务的进度心跳
    // 都远密于阈值；只有「后端已死/漏发终态」的任务会命中。
    if (staleSweeper === null) {
      staleSweeper = setInterval(() => {
        const now = Date.now();
        const stale = get().tasks.filter((t) => {
          if (t.status !== 'running' && t.status !== 'pending') return false;
          // updated_at 兼容 string | number（OmniTask 契约）
          const ts = typeof t.updated_at === 'number'
            ? t.updated_at
            : (Date.parse(String(t.updated_at ?? '')) || 0);
          return now - ts > STALE_TASK_MS;
        });
        for (const t of stale) {
          get().completeTask(t.id);
        }
      }, STALE_SWEEP_INTERVAL_MS);
    }
  },

  destroy: () => {
    unsubscribers.forEach((off) => {
      try {
        off();
      } catch {
        /* 忽略 */
      }
    });
    unsubscribers = [];
    if (staleSweeper !== null) {
      clearInterval(staleSweeper);
      staleSweeper = null;
    }
    set({ subscribed: false });
  },

  upsertTask: (task) => {
    set((state) => {
      const idx = state.tasks.findIndex((t) => t.id === task.id);
      if (idx >= 0) {
        const next = [...state.tasks];
        next[idx] = { ...next[idx], ...task, updated_at: Date.now() };
        return { tasks: next };
      }
      return { tasks: [...state.tasks, { ...task, updated_at: Date.now() }] };
    });
  },

  setTasks: (tasks) => set({ tasks }),

  removeTask: (taskId) => {
    set((state) => ({ tasks: state.tasks.filter((t) => t.id !== taskId) }));
  },

  updateProgress: (taskId, progress, extra) => {
    set((state) => ({
      tasks: state.tasks.map((t) =>
        t.id === taskId
          ? { ...t, progress, status: 'running', ...extra,
              updated_at: Date.now() }
          : t,
      ),
    }));
  },

  completeTask: (taskId, resultUrl) => {
    set((state) => ({
      tasks: state.tasks.map((t) =>
        t.id === taskId
          ? {
              ...t,
              status: 'done',
              progress: 1,
              result_url: resultUrl ?? t.result_url,
            }
          : t,
      ),
    }));
  },

  failTask: (taskId, error) => {
    set((state) => ({
      tasks: state.tasks.map((t) =>
        t.id === taskId ? { ...t, status: 'error', error } : t,
      ),
    }));
  },
}));

export default useTaskStore;
// 本项目仅供学习使用，商业授权请+Q 3559331368
