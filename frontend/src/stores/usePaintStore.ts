/* ==========================================================================
 * OmniSpace AI v2.1 —— 绘画状态（对接 backend/api/draw.py 真实契约）
 * --------------------------------------------------------------------------
 * 职责：
 *   - paintRequest 绘画参数（正向/反向提示词、尺寸、步数、CFG、种子、模型等）
 *   - results 生成结果列表
 *   - generate / img2img 生成操作（显式传参，UI 编辑值直达后端）
 *   - models 绘画模型路由表（手动模式数据源）
 * 功能互斥：绘画为重量级功能，生成前检查 activeFeature（规格 §6.1）。
 * 注意：后端暂无 LoRA 列表 / 收藏端点，相关状态保留但不拉取。
 * ========================================================================== */

import { create } from 'zustand';
import type { PaintRequest, PaintResult } from '@/types';
import * as paintApi from '@/services/paintApi';
import type { DrawModelItem } from '@/services/paintApi';
import { API_BASE } from '@/services/api';
import { useAppStore } from './useAppStore';
import { useTaskStore } from './useTaskStore';

/** 默认绘画参数 */
const DEFAULT_REQUEST: PaintRequest = {
  prompt: '',
  negative_prompt: '',
  width: 1024,
  height: 1024,
  steps: 30,
  cfg_scale: 7.5,
  sampler: 'dpm++_2m_karras',
  seed: -1,
  batch_size: 1,
  mode: 'quick',
  loras: [],
};

/** 由历史 file_path 拼图片 URL（generated/images/<file> → /v1/draw/image/<file>） */
function fileUrl(filePath: string): string {
  const name = filePath.split('/').pop() || '';
  return name ? `${API_BASE}/draw/image/${name}` : '';
}

/** 绘画状态 */
export interface PaintState {
  /* ------------------------------ 数据 ------------------------------ */
  /** 绘画参数 */
  paintRequest: PaintRequest;
  /** 生成结果列表 */
  results: PaintResult[];
  /** 是否正在生成 */
  generating: boolean;
  /** 当前任务 ID */
  currentTaskId: string | null;
  /** 绘画模型路由表（/draw/models） */
  models: DrawModelItem[];
  /** 历史是否已加载 */
  historyLoaded: boolean;

  /* ------------------------------ 动作 ------------------------------ */
  /** 更新绘画参数（局部合并） */
  setPaintRequest: (patch: Partial<PaintRequest>) => void;
  /** 重置为默认参数 */
  resetPaintRequest: () => void;
  /** 文生图生成（request 为 UI 当前编辑值；缺省用 store 参数） */
  generate: (request?: PaintRequest) => Promise<boolean>;
  /** 图生图（init_image + denoising_strength） */
  img2img: (initImage: string, denoisingStrength?: number) => Promise<boolean>;
  /** 拉取历史 */
  fetchHistory: () => Promise<void>;
  /** 拉取绘画模型列表 */
  fetchModels: () => Promise<void>;
}

export const usePaintStore = create<PaintState>((set, get) => ({
  paintRequest: { ...DEFAULT_REQUEST },
  results: [],
  generating: false,
  currentTaskId: null,
  models: [],
  historyLoaded: false,

  setPaintRequest: (patch) => {
    set((state) => ({ paintRequest: { ...state.paintRequest, ...patch } }));
  },

  resetPaintRequest: () => {
    set({ paintRequest: { ...DEFAULT_REQUEST } });
  },

  generate: async (request) => {
    const paintRequest = request ?? get().paintRequest;
    if (!paintRequest.prompt.trim()) {
      useAppStore.getState().showToast('请输入正向提示词', 'warning');
      return false;
    }
    // 同步 UI 编辑值到 store（历史回填/再次生成用）
    if (request) {
      set({ paintRequest: { ...get().paintRequest, ...request } });
    }

    // 功能互斥前置检查（规格 §6.1）
    const appStore = useAppStore.getState();
    if (!appStore.setActiveFeature('paint')) {
      return false; // 被阻断
    }

    try {
      set({ generating: true });
      const { task_id } = await paintApi.generate(paintRequest);
      set({ currentTaskId: task_id });

      // 注册到全局任务，接收进度
      useTaskStore.getState().upsertTask({
        id: task_id,
        type: 'text2img',
        name: '文生图',
        status: 'running',
        progress: 0,
        pausable: false,
      });

      // 轮询状态直到完成（进度逐次回写任务 store，驱动进度条）
      pollTaskStatus(task_id, paintRequest);
      return true;
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '生成失败';
      useAppStore.getState().showToast(msg, 'error');
      set({ generating: false });
      appStore.releaseActiveFeature();
      return false;
    }
  },

  img2img: async (initImage, denoisingStrength = 0.6) => {
    const appStore = useAppStore.getState();
    if (!appStore.setActiveFeature('paint')) {
      return false;
    }

    const params: PaintRequest = {
      ...get().paintRequest,
      init_image: initImage,
      denoising_strength: denoisingStrength,
    };
    try {
      set({ generating: true });
      const { task_id } = await paintApi.img2img(params);
      set({ currentTaskId: task_id });
      useTaskStore.getState().upsertTask({
        id: task_id,
        type: 'img2img',
        name: '图生图',
        status: 'running',
        progress: 0,
      });
      pollTaskStatus(task_id, params);
      return true;
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '生成失败';
      useAppStore.getState().showToast(msg, 'error');
      set({ generating: false });
      appStore.releaseActiveFeature();
      return false;
    }
  },

  fetchHistory: async () => {
    try {
      const res = await paintApi.listHistory({ page_size: 50 });
      const items: PaintResult[] = (res.items || []).map((it) => ({
        id: it.task_id,
        url: fileUrl(it.file_path || ''),
        params: {
          prompt: it.prompt,
          negative_prompt: it.negative,
          ...(it.params as Partial<PaintRequest>),
        },
        seed: it.seed,
        created_at: (it.created_at || 0) * 1000,
        favorite: false,
      }));
      set({ results: items, historyLoaded: true });
    } catch {
      set({ historyLoaded: true });
    }
  },

  fetchModels: async () => {
    try {
      const res = await paintApi.listModels();
      set({ models: res.items || [] });
    } catch {
      /* 忽略：模型列表拉取失败时手动模式为空 */
    }
  },
}));

/**
 * 轮询任务状态直到完成（§6.4 协调机制）。
 * 每次轮询把 percent 回写任务 store（progress 0~1 约定），驱动进度条；
 * done 时装配 PaintResult 插入结果列表，error 时 toast 并释放功能锁。
 */
function pollTaskStatus(taskId: string, request: PaintRequest) {
  let stopped = false;
  const timer = setInterval(async () => {
    if (stopped) {
      return;
    }
    try {
      const task = await paintApi.getStatus(taskId);
      // 进度回写（OmniTask.progress 约定 0~1）
      useTaskStore
        .getState()
        .updateProgress(taskId, (task.percent ?? 0) / 100, {
          status: task.status === 'error' ? 'error' : undefined,
        });

      if (task.status === 'done') {
        stopped = true;
        clearInterval(timer);
        const result: PaintResult = {
          id: taskId,
          url: task.image
            ? `data:image/png;base64,${task.image}`
            : fileUrl(task.file_path || ''),
          params: request,
          seed: task.seed,
          created_at: (task.created_at || Date.now() / 1000) * 1000,
          favorite: false,
        };
        usePaintStore.setState((state) => ({
          results: [result, ...state.results],
          generating: false,
          currentTaskId: null,
        }));
        useTaskStore.getState().completeTask(taskId, result.url);
        useAppStore.getState().releaseActiveFeature();
      } else if (task.status === 'error') {
        stopped = true;
        clearInterval(timer);
        useTaskStore
          .getState()
          .failTask(taskId, task.error || '生成失败');
        useAppStore
          .getState()
          .showToast(task.error || '生成失败', 'error');
        usePaintStore.setState({ generating: false, currentTaskId: null });
        useAppStore.getState().releaseActiveFeature();
      }
    } catch {
      // 单次失败不中断轮询
    }
  }, 1000);

  // 兜底超时（180s：首次含模型加载可能较慢）
  setTimeout(() => {
    if (!stopped && usePaintStore.getState().generating) {
      stopped = true;
      clearInterval(timer);
    }
  }, 180000);
}

export default usePaintStore;
