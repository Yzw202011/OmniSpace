/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 视频风格 LoRA 状态（对接 /style/* 真实端点）
 * --------------------------------------------------------------------------
 * 职责：
 *   - 素材上传（POST /style/upload → dataset_id）与数据集列表（GET /style/datasets）
 *   - 风格描述与 LoRA 参数（rank / alpha / learning_rate / epochs）
 *   - 发起训练（POST /style/train）与进度轮询（GET /style/tasks/{id}，2s）
 *   - 版本列表（GET /style/versions）与回滚（POST /style/rollback，真实端点）
 *   - 服务状态（GET /style/status：基座未就绪时 UI 诚实门控）
 * 功能互斥：训练中通过 useAppStore.activeFeature==='training' 置灰其他 AI 功能。
 * ========================================================================== */

import { create } from 'zustand';
import * as styleApi from '@/services/styleApi';
import type { StyleDataset, StyleLoraVersion, StyleStatus } from '@/services/styleApi';
import type { TrainTask } from '@/types';
import { useAppStore } from './useAppStore';

/** 轮询定时器与可见性监听（模块级单例） */
let pollTimer: ReturnType<typeof setInterval> | null = null;
let pollVisHandler: (() => void) | null = null;

// 性能优化：训练为分钟级长任务，前台 5s / 后台 10s 轮询足够跟踪进度；
// 可见性切换时重排间隔，隐藏时降频避免无意义请求。
const STYLE_POLL_FG_MS = 5000;
const STYLE_POLL_BG_MS = 10000;
function _pollDelay(): number {
  return typeof document !== 'undefined' && document.hidden
    ? STYLE_POLL_BG_MS : STYLE_POLL_FG_MS;
}

function clearPoll() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (pollVisHandler) {
    document.removeEventListener('visibilitychange', pollVisHandler);
    pollVisHandler = null;
  }
}

function schedulePoll() {
  clearPoll();
  pollTimer = setInterval(() => {
    useStyleStore.getState().pollTask();
  }, _pollDelay());
  pollVisHandler = () => schedulePoll();
  document.addEventListener('visibilitychange', pollVisHandler);
}

/** 视频风格状态 */
export interface StyleState {
  /* ------------------------------ 数据 ------------------------------ */
  /** 当前选中的训练数据集 ID（/style/train 入参） */
  datasetId: string | null;
  /** 已上传素材的本地文件名 */
  datasetName: string | null;
  /** 上传中 */
  uploading: boolean;
  /** 数据集列表（GET /style/datasets） */
  datasets: StyleDataset[];
  /** 数据集列表已加载 */
  datasetsLoaded: boolean;
  /** 风格描述 */
  stylePrompt: string;
  /** LoRA rank */
  rank: number;
  /** LoRA alpha */
  alpha: number;
  /** 学习率（后端钳制边界 1e-7 ~ 1e-3） */
  learningRate: number;
  /** 训练轮次 */
  epochs: number;
  /** 当前训练任务 */
  task: TrainTask | null;
  /** 训练中 */
  training: boolean;
  /** LoRA 版本列表 */
  versions: StyleLoraVersion[];
  /** 版本列表已加载 */
  versionsLoaded: boolean;
  /** 服务状态（基座就绪/FFmpeg/样本下限；null=未加载） */
  status: StyleStatus | null;

  /* ------------------------------ 动作 ------------------------------ */
  setStylePrompt: (v: string) => void;
  setRank: (v: number) => void;
  setAlpha: (v: number) => void;
  setLearningRate: (v: number) => void;
  setEpochs: (v: number) => void;
  /** 上传素材（成功后刷新数据集列表并选中新数据集） */
  uploadMaterial: (file: File) => Promise<boolean>;
  /** 拉取数据集列表 */
  fetchDatasets: () => Promise<void>;
  /** 选择训练数据集 */
  selectDataset: (datasetId: string, name?: string) => void;
  /** 拉取服务状态（基座门控） */
  fetchStatus: () => Promise<void>;
  /** 开始训练（成功后启动 2s 轮询） */
  startTraining: () => Promise<boolean>;
  /** 轮询训练进度 */
  pollTask: () => Promise<void>;
  /** 拉取 LoRA 版本列表 */
  fetchVersions: () => Promise<void>;
  /** 回滚到指定版本（POST /style/rollback 真实生效） */
  rollback: (version: string) => Promise<void>;
}

export const useStyleStore = create<StyleState>((set, get) => ({
  datasetId: null,
  datasetName: null,
  uploading: false,
  datasets: [],
  datasetsLoaded: false,
  stylePrompt: '',
  rank: 16,
  alpha: 32,
  learningRate: 2e-5,
  epochs: 10,
  task: null,
  training: false,
  versions: [],
  versionsLoaded: false,
  status: null,

  setStylePrompt: (v) => set({ stylePrompt: v }),
  setRank: (v) => set({ rank: v }),
  setAlpha: (v) => set({ alpha: v }),
  setLearningRate: (v) => set({ learningRate: v }),
  setEpochs: (v) => set({ epochs: v }),

  uploadMaterial: async (file) => {
    set({ uploading: true });
    try {
      const res = await styleApi.uploadStyleDataset(file);
      set({
        datasetId: res.dataset_id,
        datasetName: file.name,
        uploading: false,
      });
      useAppStore.getState().showToast(
        `素材「${file.name}」上传成功（${res.frame_count} 帧样本）`,
        'success',
      );
      get().fetchDatasets();
      return true;
    } catch (err) {
      set({ uploading: false });
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '素材上传失败';
      useAppStore.getState().showToast(msg, 'error');
      return false;
    }
  },

  fetchDatasets: async () => {
    try {
      const datasets = await styleApi.listStyleDatasets();
      set({ datasets, datasetsLoaded: true });
    } catch {
      set({ datasetsLoaded: true });
    }
  },

  selectDataset: (datasetId, name) => {
    set({ datasetId, datasetName: name ?? datasetId });
  },

  fetchStatus: async () => {
    try {
      const status = await styleApi.getStyleStatus();
      set({ status });
    } catch {
      /* 后端未就绪保持上次快照 */
    }
  },

  startTraining: async () => {
    const { datasetId, stylePrompt, rank, alpha, learningRate, epochs } = get();
    const appStore = useAppStore.getState();
    if (!datasetId) {
      appStore.showToast('请先上传或选择训练数据集', 'warning');
      return false;
    }
    if (!stylePrompt.trim()) {
      appStore.showToast('请填写风格描述', 'warning');
      return false;
    }
    // 功能互斥前置检查（训练重量级功能，规格 §6.1）
    if (!appStore.setActiveFeature('training')) {
      return false;
    }
    try {
      const raw = await styleApi.startStyleTrain({
        dataset_id: datasetId,
        name: `视频风格 LoRA ${new Date().toLocaleString('zh-CN')}`,
        style_prompt: stylePrompt.trim(),
        rank,
        alpha,
        learning_rate: learningRate,
        epochs,
      });
      set({ task: styleApi.mapStyleTask(raw), training: true });
      appStore.showToast('风格 LoRA 训练已开始', 'success');
      // 启动自适应轮询（前台 5s / 后台 10s，性能优化）
      schedulePoll();
      return true;
    } catch (err) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '发起训练失败';
      appStore.showToast(msg, 'error');
      appStore.releaseActiveFeature();
      return false;
    }
  },

  pollTask: async () => {
    const { task } = get();
    if (!task) {
      clearPoll();
      return;
    }
    try {
      const latest = styleApi.mapStyleTask(await styleApi.getStyleTask(task.id));
      set({ task: latest });
      if (latest.status === 'done' || latest.status === 'error' || latest.status === 'cancelled') {
        clearPoll();
        set({ training: false });
        const appStore = useAppStore.getState();
        if (appStore.activeFeature === 'training') {
          appStore.releaseActiveFeature();
        }
        if (latest.status === 'done') {
          appStore.showToast('风格 LoRA 训练完成', 'success');
          get().fetchVersions();
        } else if (latest.status === 'error') {
          appStore.showToast(`训练失败：${latest.error || '未知错误'}`, 'error');
        }
      }
    } catch {
      /* 轮询失败静默，下一周期重试 */
    }
  },

  fetchVersions: async () => {
    try {
      const versions = await styleApi.listStyleVersions();
      set({ versions, versionsLoaded: true });
    } catch {
      set({ versionsLoaded: true });
    }
  },

  rollback: async (version) => {
    const appStore = useAppStore.getState();
    try {
      await styleApi.rollbackStyle(version);
      appStore.showToast(`已回滚到「${version}」并置为当前版本`, 'success');
      get().fetchVersions();
    } catch (err) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '回滚失败';
      appStore.showToast(msg, 'error');
    }
  },
}));

export default useStyleStore;
