/* ==========================================================================
 * OmniSpace AI v2.1 —— 知识学习状态（规格 §4.1 知识学习 + §9.4 统一学习系统）
 * --------------------------------------------------------------------------
 * 职责：
 *   - trainTasks 训练任务队列
 *   - availableModels 可用学习模型（引擎就绪 + 阈值计数）
 *   - createTrain / getTasks 训练操作
 *   - dataset 上传 / 能力面板
 * 功能互斥：训练为重量级功能，发起训练前检查 activeFeature（规格 §6.1）。
 * ========================================================================== */

import { create } from 'zustand';
import type { TrainTask, TrainType } from '@/types';
import * as learnApi from '@/services/learnApi';
import { useAppStore } from './useAppStore';
import { useTaskStore } from './useTaskStore';

/** 可用学习模型 */
export interface AvailableLearnModel {
  name: string;
  type: TrainType;
  ready: boolean;
  current_count: number;
  threshold: number;
  description?: string;
}

/** 知识学习状态 */
export interface LearnState {
  /* ------------------------------ 数据 ------------------------------ */
  /** 训练任务列表 */
  trainTasks: TrainTask[];
  /** 可用学习模型 */
  availableModels: AvailableLearnModel[];
  /** 能力面板 */
  capability: {
    engines: Record<string, boolean>;
    chromadb: boolean;
    thresholds: Record<string, { current: number; threshold: number }>;
  } | null;
  /** 是否已加载任务 */
  tasksLoaded: boolean;
  /** 训练中 */
  training: boolean;

  /* ------------------------------ 动作 ------------------------------ */
  /** 拉取训练任务列表 */
  fetchTasks: () => Promise<void>;
  /** 拉取可用学习模型 */
  fetchAvailableModels: () => Promise<void>;
  /** 拉取能力面板 */
  fetchCapability: () => Promise<void>;
  /** 发起训练（功能互斥 training） */
  createTrain: (body: learnApi.CreateTrainBody) => Promise<boolean>;
  /** 取消训练任务 */
  cancelTask: (taskId: string) => Promise<void>;
  /** 重排优先级 */
  reorderTasks: (taskIds: string[]) => Promise<void>;
  /** 上传训练数据集 */
  uploadDataset: (
    file: File,
    meta?: { type?: TrainType },
  ) => Promise<{ path: string; size_mb?: number; count?: number }>;
  /** 手动新建知识点（文本≥100 自动建 knowledge_lora 任务） */
  createPoint: (body: {
    title?: string;
    content: string;
    source?: string;
    type?: string;
  }) => Promise<{ id: string; auto_train?: boolean; task_id?: string }>;
  /** 添加学习 URL */
  addUrl: (url: string, title?: string) => Promise<{ id: string }>;
  /** 网页爬取 */
  crawl: (url: string) => Promise<{ task_id: string }>;
  /** 自动学习（深度≤3/50页/同域 BFS） */
  autoLearn: (
    url: string,
    maxDepth?: number,
    maxPages?: number,
  ) => Promise<{ task_id: string; pages?: number }>;
}

export const useLearnStore = create<LearnState>((set, get) => ({
  trainTasks: [],
  availableModels: [],
  capability: null,
  tasksLoaded: false,
  training: false,

  fetchTasks: async () => {
    try {
      const res = await learnApi.listTasks();
      const tasks = res.items || [];
      set({ trainTasks: tasks, tasksLoaded: true });
      // 训练在后台运行，轮询时若已无 running/pending 任务则清除 training 标志
      const hasActiveTask = tasks.some(
        (t) => t.status === 'running' || t.status === 'pending',
      );
      if (!hasActiveTask) {
        set({ training: false });
      }
    } catch {
      set({ tasksLoaded: true });
    }
  },

  fetchAvailableModels: async () => {
    try {
      const list = await learnApi.listLearnModels();
      set({ availableModels: list || [] });
    } catch {
      /* 忽略 */
    }
  },

  fetchCapability: async () => {
    try {
      const cap = await learnApi.getCapability();
      set({ capability: cap });
    } catch {
      /* 忽略 */
    }
  },

  createTrain: async (body) => {
    // 功能互斥前置检查（规格 §6.1）
    const appStore = useAppStore.getState();
    if (!appStore.setActiveFeature('training')) {
      return false;
    }
    try {
      set({ training: true });
      const task = await learnApi.createTrain(body);
      set((state) => ({ trainTasks: [task, ...state.trainTasks] }));
      // 注册全局任务，接收 WS 进度
      useTaskStore.getState().upsertTask({
        id: task.id,
        type: `train:${task.type}`,
        name: task.name,
        status: 'running',
        progress: 0,
        priority: task.priority,
        pausable: false,
      });
      return true;
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '发起训练失败';
      useAppStore.getState().showToast(msg, 'error');
      appStore.releaseActiveFeature();
      // 仅在 API 调用失败时清除 training 标志（训练在后台运行，成功时保持 true）
      set({ training: false });
      return false;
    }
  },

  cancelTask: async (taskId) => {
    try {
      const task = await learnApi.cancelTask(taskId);
      set((state) => ({
        trainTasks: state.trainTasks.map((t) =>
          t.id === taskId ? { ...t, ...task, status: 'cancelled' } : t,
        ),
      }));
      useAppStore.getState().showToast('训练任务已取消', 'success');
      // 训练取消后释放功能锁
      const appStore = useAppStore.getState();
      if (appStore.activeFeature === 'training') {
        appStore.releaseActiveFeature();
      }
    } catch (err) {
      // 取消失败如实反馈（如后端未提供取消端点）
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '取消训练任务失败';
      useAppStore.getState().showToast(msg, 'error');
    }
  },

  reorderTasks: async (taskIds) => {
    try {
      await learnApi.reorderTasks(taskIds);
      // 按新顺序重排本地任务
      set((state) => {
        const map = new Map(state.trainTasks.map((t) => [t.id, t]));
        const reordered = taskIds
          .map((id) => map.get(id))
          .filter((t): t is TrainTask => t !== undefined);
        // 保留未在列表中的任务
        const rest = state.trainTasks.filter((t) => !taskIds.includes(t.id));
        return { trainTasks: [...reordered, ...rest] };
      });
    } catch {
      /* 忽略 */
    }
  },

  uploadDataset: async (file, meta) => {
    return learnApi.uploadDataset(file, meta);
  },

  createPoint: async (body) => {
    const res = await learnApi.createPoint(body);
    // 自动触发训练时刷新任务列表
    if (res.auto_train) {
      get().fetchTasks();
    }
    return res;
  },

  addUrl: async (url, title) => {
    return learnApi.addUrl({ url, title });
  },

  crawl: async (url) => {
    return learnApi.crawl({ url });
  },

  autoLearn: async (url, maxDepth, maxPages) => {
    return learnApi.autoLearn({
      url,
      max_depth: maxDepth,
      max_pages: maxPages,
    });
  },
}));

export default useLearnStore;
