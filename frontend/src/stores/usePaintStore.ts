// 本项目仅供学习使用，商业授权请+Q 3559331368
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
import { warmupFeature } from '@/services/modelApi';
import { trackBehavior } from '@/services/learningApi';
import { useAppStore } from './useAppStore';
import { useTaskStore } from './useTaskStore';
import { useWarmupStore } from './useWarmupStore';

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

/* ──────────────────────────────────────────────────────────────────
 * 生图前置：绘画模型未加载时立即点火加载并告知用户（2026-08-31 用户需求
 * 「点击生图时未加载要立刻加载、也要告知」）。
 * 已就绪 → 零开销直通；未就绪 → POST /models/warmup feature=paint 点火
 * （与进页面预热同一通道，inflight 幂等）+ PaintWarmupModal 进度弹窗
 * + toast 说明，就绪后自动继续提交。等待上限 120s，超时仍提交——后端
 * 任务内「模型加载」节点会再次兜底 ensure_loaded，失败则如实报错。
 * ────────────────────────────────────────────────────────────────── */
const PAINT_READY_POLL_MS = 2_000;
const PAINT_READY_TIMEOUT_MS = 120_000;

async function isPaintReady(): Promise<boolean> {
  try {
    const st = (await paintApi.getStatus()) as {
      loaded?: boolean;
      state?: string;
    };
    return Boolean(st && (st.loaded === true || st.state === 'ready'));
  } catch {
    // 状态接口不可达（后端瞬断）：不拦截生成，交由后端如实报错
    return true;
  }
}

async function ensurePaintReady(model?: string): Promise<void> {
  if (await isPaintReady()) return;
  try {
    const r = await warmupFeature('paint', model || undefined);
    if (r?.reason === 'already_ready') return;
  } catch {
    // 预热请求被拒（互斥/后端异常）：不再等待，直接提交，
    // 由 /draw/generate 预检给出真实原因（用户能看到可操作的报错）
    return;
  }
  useWarmupStore.getState().begin(undefined, 'paint');
  useAppStore.getState().showToast(
    '绘画模型未加载，正在自动加载（约 0.5-1 分钟），完成后将自动开始生成，无需其他操作',
    'info',
  );
  const deadline = Date.now() + PAINT_READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, PAINT_READY_POLL_MS));
    if (await isPaintReady()) return;
  }
}

/** 模型加载类失败的补充指引：告诉用户去哪里手动处理，而不是一句「未加载」 */
function withLoadHint(msg: string): string {
  return /MODEL_LOAD_FAILED|绘画模型未就绪|模型未加载|未处于已加载/.test(msg)
    ? `${msg}。可在「模型管理」页查看绘画模型状态并手动加载，或稍后重试`
    : msg;
}

/** 由历史 file_path 拼图片 URL（generated/images/<file> → /v1/draw/image/<file>） */
function fileUrl(filePath: string): string {
  const name = filePath.split('/').pop() || '';
  return name ? `${API_BASE}/draw/image/${name}` : '';
}

/** 绘画视频任务（页面内存态；status: generating/done/error/cancelled） */
export interface PaintVideoTask {
  taskId: string;
  /** 生成模式：i2v 纯图 / ti2v 文+图 */
  mode: 'i2v' | 'ti2v';
  prompt: string;
  durationSeconds: number;
  fps: number;
  resolution: string;
  status: string;
  progress: number;
  /** 预计剩余秒数（后端 step 采样外推；null = 引擎尚未采样） */
  etaSeconds?: number;
  error?: string;
  degraded?: boolean;
  createdAt: number;
}

/** 视频轮询句柄（taskId → interval） */
const _videoTimers = new Map<string, ReturnType<typeof setInterval>>();

/** 图片 URL → 纯 base64（fetch → blob → FileReader） */
async function imageUrlToBase64(url: string): Promise<string> {
  const res = await fetch(url);
  const blob = await res.blob();
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => {
      const dataUrl = String(fr.result || '');
      resolve(dataUrl.slice(dataUrl.indexOf(',') + 1));
    };
    fr.onerror = () => reject(new Error('图片读取失败'));
    fr.readAsDataURL(blob);
  });
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
  /** 视频任务列表（新→旧，页面内存态） */
  videoTasks: PaintVideoTask[];
  /** 是否有视频任务生成中 */
  videoGenerating: boolean;

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
  /** 删除单张图片（历史记录 + 文件，乐观移除本地条目） */
  deleteImage: (id: string) => Promise<boolean>;
  /** 批量删除图片（上限 200，成功后移除本地条目） */
  batchDeleteImages: (ids: string[]) => Promise<boolean>;
  /** 发起视频生成（i2v 纯图 / ti2v 文+图；imageSource 为图片 URL 或 dataURL） */
  generateVideo: (opts: {
    mode: 'i2v' | 'ti2v';
    prompt: string;
    imageSource?: string;
    durationSeconds: number;
    fps: number;
    resolution: string;
  }) => Promise<boolean>;
  /** 拉取视频生成历史（后端 video_tasks 表 paint_ 来源任务，跨浏览器可见） */
  fetchVideoHistory: () => Promise<void>;
  /** 移除一条视频任务记录（停止其轮询；同步删除后端记录与文件） */
  removeVideoTask: (taskId: string) => void;
}

export const usePaintStore = create<PaintState>((set, get) => ({
  paintRequest: { ...DEFAULT_REQUEST },
  results: [],
  generating: false,
  currentTaskId: null,
  models: [],
  historyLoaded: false,
  videoTasks: [],
  videoGenerating: false,

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
      // 模型未加载 → 立即点火 + 弹窗告知，就绪（或超时兜底）后继续提交
      await ensurePaintReady(paintRequest.model);
      const res = await paintApi.generate(paintRequest);
      // 批量（P2 复核修复 2026-09-02）：后端按 batch_size 派发 N 个队列
      // 任务（定种子按序派生防同图）；逐张轮询，全部终态才释放
      // generating/功能锁（单张路径行为不变）
      const ids = res.task_ids?.length ? res.task_ids : [res.task_id];
      set({ currentTaskId: ids[0] });
      const pending = { n: ids.length };
      const onSettled = () => {
        pending.n -= 1;
        if (pending.n <= 0) {
          usePaintStore.setState({ generating: false, currentTaskId: null });
          useAppStore.getState().releaseActiveFeature();
        }
      };
      ids.forEach((tid, idx) => {
        useTaskStore.getState().upsertTask({
          id: tid,
          type: 'text2img',
          name: ids.length > 1 ? `文生图 ${idx + 1}/${ids.length}` : '文生图',
          status: 'running',
          progress: 0,
          pausable: false,
        });
        pollTaskStatus(tid, paintRequest, onSettled);
      });

      // 行为学习埋点（fire-and-forget，失败静默）
      trackBehavior('paint_generate', {
        content: paintRequest.prompt.slice(0, 200),
        context: paintRequest.model ? `model:${paintRequest.model}` : 'auto',
        feature: 'paint',
      });
      return true;
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '生成失败';
      useAppStore.getState().showToast(withLoadHint(msg), 'error');
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
      // 模型未加载 → 立即点火 + 弹窗告知（同文生图入口）
      await ensurePaintReady(params.model);
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
      useAppStore.getState().showToast(withLoadHint(msg), 'error');
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

  deleteImage: async (id) => {
    try {
      await paintApi.deleteHistory(id);
      set((state) => ({
        results: state.results.filter((r) => r.id !== id),
      }));
      return true;
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '删除失败';
      useAppStore.getState().showToast(msg, 'error');
      return false;
    }
  },

  batchDeleteImages: async (ids) => {
    if (!ids.length) return false;
    try {
      const res = await paintApi.batchDeleteHistory(ids);
      const removed = new Set(ids.filter((i) => !res.missing?.includes(i)));
      set((state) => ({
        results: state.results.filter((r) => !removed.has(r.id)),
      }));
      return true;
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '批量删除失败';
      useAppStore.getState().showToast(msg, 'error');
      return false;
    }
  },

  generateVideo: async (opts) => {
    const toast = useAppStore.getState().showToast;
    if (opts.mode === 'ti2v' && !opts.prompt.trim()) {
      toast('请输入动作/运镜描述', 'warning');
      return false;
    }
    if (!opts.imageSource) {
      toast('请先选择或上传输入图片', 'warning');
      return false;
    }
    try {
      // 输入图 → 纯 base64（dataURL 直取 / http URL fetch 转换）
      const imageBase64 = opts.imageSource!.startsWith('data:')
        ? opts.imageSource!.slice(opts.imageSource!.indexOf(',') + 1)
        : await imageUrlToBase64(opts.imageSource!);
      const res = await paintApi.generatePaintVideo({
        description: opts.prompt.trim(),
        image_base64: imageBase64,
        duration_seconds: opts.durationSeconds,
        fps: opts.fps,
        resolution: opts.resolution,
      });
      const task: PaintVideoTask = {
        taskId: res.task_id,
        mode: opts.mode,
        prompt: opts.prompt.trim(),
        durationSeconds: opts.durationSeconds,
        fps: opts.fps,
        resolution: opts.resolution,
        status: 'generating',
        progress: 0,
        degraded: res.degraded,
        createdAt: Date.now(),
      };
      set((state) => ({
        videoTasks: [task, ...state.videoTasks],
        videoGenerating: true,
      }));
      pollVideoStatus(res.task_id);

      // 行为学习埋点（fire-and-forget，失败静默）
      trackBehavior('video_generate', {
        content: opts.prompt.trim().slice(0, 200),
        context: `${opts.mode}/${opts.durationSeconds}s`,
        feature: 'video',
      });
      return true;
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '视频生成发起失败';
      toast(msg, 'error');
      return false;
    }
  },

  fetchVideoHistory: async () => {
    try {
      const res = await paintApi.getPaintVideoHistory(100);
      const known = new Set(usePaintStore.getState().videoTasks.map((t) => t.taskId));
      const items: PaintVideoTask[] = res.items.map((h) => ({
        taskId: h.task_id,
        mode: h.mode,
        prompt: h.prompt,
        durationSeconds: h.duration_seconds,
        fps: h.fps,
        resolution: h.resolution,
        status: h.status,
        progress: h.status === 'done' ? 100 : 0,
        degraded: h.degraded,
        createdAt: (h.created_at || 0) * 1000,
      }));
      // 生成中的历史任务恢复轮询（后端重启/页面刷新后 ETA 丢失属预期）
      for (const it of items) {
        if (it.status === 'generating' && !known.has(it.taskId)) {
          pollVideoStatus(it.taskId);
        }
      }
      set((state) => ({
        // 已存在的本地任务（含实时进度）优先，历史仅补齐缺口
        videoTasks: [
          ...state.videoTasks,
          ...items.filter((it) => !known.has(it.taskId)),
        ],
        videoGenerating: state.videoGenerating
          || items.some((it) => it.status === 'generating'),
      }));
    } catch {
      // 历史拉取失败不阻断页面（如后端重启中）；生成流程不受影响
    }
  },

  removeVideoTask: (taskId) => {
    const timer = _videoTimers.get(taskId);
    if (timer) {
      clearInterval(timer);
      _videoTimers.delete(taskId);
    }
    set((state) => ({
      videoTasks: state.videoTasks.filter((t) => t.taskId !== taskId),
      videoGenerating: state.videoTasks.some(
        (t) => t.taskId !== taskId && t.status === 'generating'),
    }));
    // 同步删除后端记录与文件（fire-and-forget；失败仅提示，本地已移除）
    paintApi.deletePaintVideoTask(taskId).catch(() => {
      useAppStore.getState().showToast('后端记录删除失败，重新打开后该记录可能仍存在', 'warning');
    });
  },
}));

/**
 * 轮询任务状态直到完成（§6.4 协调机制）。
 * 每次轮询把 percent 回写任务 store（progress 0~1 约定），驱动进度条；
 * done 时装配 PaintResult 插入结果列表，error 时 toast 并释放功能锁。
 */
function pollTaskStatus(taskId: string, request: PaintRequest,
                        onSettled?: () => void) {
  let stopped = false;
  let failStreak = 0; // 连续轮询失败计数（后端不可达检测）
  const finishError = (message: string) => {
    stopped = true;
    clearInterval(timer);
    // 模型加载类失败附上「去哪加载」的指引，避免用户面对裸「未加载」
    useTaskStore.getState().failTask(taskId, message);
    useAppStore.getState().showToast(withLoadHint(message), 'error');
    if (onSettled) {
      onSettled();  // 批量：计数释放；单张沿用旧语义（回调内释放）
    } else {
      usePaintStore.setState({ generating: false, currentTaskId: null });
      useAppStore.getState().releaseActiveFeature();
    }
  };
  const timer = setInterval(async () => {
    if (stopped) {
      return;
    }
    try {
      const task = await paintApi.getStatus(taskId);
      failStreak = 0;
      // 进度回写（OmniTask.progress 约定 0~1）；排队中如实标 pending
      // 并透出位次（统一图像队列 2026-09-02：TopBar 显示「排队中·前N」）
      useTaskStore
        .getState()
        .updateProgress(taskId, (task.percent ?? 0) / 100, {
          status: task.status === 'error' ? 'error'
            : task.status === 'pending' ? 'pending' : undefined,
          queue_position: task.queue_position,
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
        }));
        useTaskStore.getState().completeTask(taskId, result.url);
        if (onSettled) {
          onSettled();  // 批量：计数释放；单张沿用旧语义
        } else {
          usePaintStore.setState({ generating: false, currentTaskId: null });
          useAppStore.getState().releaseActiveFeature();
        }
        useAppStore.getState().showToast('生成完成，新图已添加到画廊', 'success');
      } else if (task.status === 'error') {
        finishError(task.error || '生成失败');
      }
    } catch {
      // 单次失败不中断轮询；但后端进程死亡（OOM 等）会导致轮询持续
      // 抛网络错误、UI 永远转圈——连续 10 次（10s）判定后端不可达，
      // 收敛任务为 error（2026-08-23 后端 OOM 前端卡死事故修复）
      failStreak += 1;
      if (failStreak >= 10) {
        finishError('后端连接失败：服务可能已中断，请检查服务状态后重试');
      }
    }
  }, 1000);

  // 兜底超时（180s：首次含模型加载可能较慢）——超时必须收敛任务
  // 状态并释放功能锁（历史 bug：只停表不收敛，UI 永远停在"生成中"）
  setTimeout(() => {
    if (!stopped && usePaintStore.getState().generating) {
      finishError('生成超时（180s）：任务已停止，请重试');
    }
  }, 180000);
}

/**
 * 轮询视频任务状态直到完成（2s 间隔，30 分钟兜底超时——视频生成含
 * 模型装载可能远慢于图片；完成/失败/取消即停表并收敛 videoGenerating）。
 */
function pollVideoStatus(taskId: string) {
  const timer = setInterval(async () => {
    try {
      const s = await paintApi.getPaintVideoStatus(taskId);
      const done = s.status === 'done';
      const failed = s.status === 'error' || s.status === 'cancelled';
      usePaintStore.setState((state) => ({
        videoTasks: state.videoTasks.map((t) =>
          t.taskId === taskId
            ? {
                ...t,
                status: done ? 'done' : failed ? s.status : 'generating',
                progress: Math.round((s.progress ?? 0) * 100),
                // 生成中透出后端外推 ETA；终态清空（完成后无意义）
                etaSeconds: done || failed ? undefined : s.eta_seconds,
                error: s.error,
                degraded: t.degraded || s.degraded,
              }
            : t,
        ),
        ...(done || failed ? { videoGenerating: false } : {}),
      }));
      if (done) {
        useAppStore.getState().showToast('视频生成完成', 'success');
      } else if (failed) {
        useAppStore
          .getState()
          .showToast(s.error || '视频生成失败', 'error');
      }
      if (done || failed) {
        clearInterval(timer);
        _videoTimers.delete(taskId);
      }
    } catch {
      // 单次失败不中断轮询（由兜底超时收敛）
    }
  }, 2000);
  _videoTimers.set(taskId, timer);
  setTimeout(
    () => {
      if (_videoTimers.has(taskId)) {
        clearInterval(timer);
        _videoTimers.delete(taskId);
        usePaintStore.setState((state) => ({
          videoTasks: state.videoTasks.map((t) =>
            t.taskId === taskId && t.status === 'generating'
              ? { ...t, status: 'error', error: '生成超时' }
              : t,
          ),
          videoGenerating: false,
        }));
      }
    },
    30 * 60 * 1000,
  );
}

export default usePaintStore;
