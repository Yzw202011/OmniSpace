/* 漫剧视频切片（TASK-P2-01）：生成发起 / 状态轮询 / 取消收敛 / 功能锁
 * 轮询定时器基础设施在 ./videoPoller（模块级单例）。 */

import type { StateCreator } from 'zustand';
import type { StoryboardGenerationStatus } from '@/types';
import * as mangaApi from '@/services/mangaApi';
import { reportBgError } from '@/utils/errors';
import { useAppStore } from '../useAppStore';
import { useTaskStore } from '../useTaskStore';
import { currentPid } from './helpers';
import { registerVideoPoll, stopVideoPoll } from './videoPoller';
import type { MangaState, MangaVideoTask, VideoSlice } from './types';

export const createVideoSlice: StateCreator<MangaState, [], [], VideoSlice> = (set, get) => {
  /** 同步视频行状态到本地 rows，并经全量保存持久化（单行 PUT 不含该字段） */
  const syncRowGenerationStatus = (rowId: string, status: StoryboardGenerationStatus): void => {
    set((state) => ({
      rows: state.rows.map((r) =>
        r.id === rowId ? { ...r, generation_status: status } : r,
      ),
    }));
    void mangaApi.saveStoryboardRows(currentPid(get), get().rows).catch((err) => {
      // 状态持久化失败不影响主流程（下次手动保存收敛），CONSOLE 级留痕
      reportBgError('videoSlice.syncRowGenerationStatus', err);
    });
  };

  /** 无活跃视频任务时释放 video_gen 功能锁 */
  const releaseVideoLockIfIdle = (): void => {
    const busy = get().videoTasks.some(
      (t) => t.status === 'generating' || t.status === 'pending',
    );
    set({ videoGenerating: busy });
    const appStore = useAppStore.getState();
    if (!busy && appStore.activeFeature === 'video_gen') {
      appStore.releaseActiveFeature();
    }
  };

  /** 更新单个视频任务条目 */
  const patchVideoTask = (taskId: string, patch: Partial<MangaVideoTask>): void => {
    set((state) => ({
      videoTasks: state.videoTasks.map((t) =>
        t.task_id === taskId ? { ...t, ...patch } : t,
      ),
    }));
  };

  /** 当前关键帧图 → base64（I2V/分镜网格协议载体，2026-08-25 竞品对齐）。
   *
   * 后端按此图做 I2V 首帧；网格关键帧（一图多镜）由后端拆格分段生成。
   * 大图压到 ≤2048 宽（2×2 网格每格仍 ≥1024）+ JPEG 0.92 控载荷；
   * 取图失败返回 ''（退纯文生视频，不阻断生成）。 */
  const fetchCurrentKeyframeB64 = async (rowId: string): Promise<string> => {
    try {
      const items = await mangaApi.listKeyframes(rowId);
      const current = items.find((k) => k.is_current) ?? items[0];
      if (!current?.file_path) return '';
      const res = await fetch(
        mangaApi.getMediaUrl(
          current.file_path,
          `${current.version}-${current.created_at}`,
        ),
      );
      if (!res.ok) return '';
      const blob = await res.blob();
      const bmp = await createImageBitmap(blob);
      const scale = Math.min(1, 2048 / bmp.width);
      const w = Math.round(bmp.width * scale);
      const h = Math.round(bmp.height * scale);
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext('2d');
      if (!ctx) return '';
      ctx.drawImage(bmp, 0, 0, w, h);
      bmp.close();
      return (canvas.toDataURL('image/jpeg', 0.92).split(',')[1] ?? '').trim();
    } catch (err) {
      // 关键帧图缺失/解码失败不阻断视频生成（诚实降级为纯文生视频）
      reportBgError('videoSlice.fetchCurrentKeyframeB64', err);
      return '';
    }
  };

  /** 启动任务状态轮询（可见 2s / 隐藏降频 10s，终态自动停止并收敛） */
  const startVideoPoll = (taskId: string, rowId: string): void => {
    let consecutiveFailCount = 0;
    const MAX_CONSECUTIVE_FAILS = 3;
    const tick = async (): Promise<void> => {
      try {
        const st = await mangaApi.getVideoStatus(taskId);
        const degraded =
          typeof (st as { degraded?: unknown }).degraded === 'boolean'
            ? ((st as { degraded?: boolean }).degraded as boolean)
            : undefined;
        const degradeReason =
          typeof (st as { degrade_reason?: unknown }).degrade_reason === 'string'
            ? ((st as { degrade_reason?: string }).degrade_reason as string)
            : undefined;
        patchVideoTask(taskId, {
          status: st.status,
          progress: st.progress,
          error: st.error,
          degraded,
          degrade_reason: degradeReason,
        });
        useTaskStore.getState().updateProgress(taskId, st.progress);
        consecutiveFailCount = 0;

        if (st.status === 'done') {
          stopVideoPoll(taskId);
          const downloadUrl = mangaApi.getVideoDownloadUrl(taskId);
          patchVideoTask(taskId, { progress: 1, download_url: downloadUrl });
          useTaskStore.getState().completeTask(taskId, downloadUrl);
          syncRowGenerationStatus(rowId, 'done');
          useAppStore.getState().showToast(
            degradeReason ? `视频已生成（降级管线）：${degradeReason}` : '视频生成完成，可下载',
            degradeReason ? 'warning' : 'success',
          );
          releaseVideoLockIfIdle();
        } else if (st.status === 'error') {
          stopVideoPoll(taskId);
          useTaskStore.getState().failTask(taskId, st.error || '视频生成失败');
          syncRowGenerationStatus(rowId, 'error');
          useAppStore.getState().showToast(st.error || '视频生成失败', 'error');
          releaseVideoLockIfIdle();
        } else if (st.status === 'cancelled') {
          // 取消链路终态：停止轮询并释放功能锁（否则轮询永不收敛）
          stopVideoPoll(taskId);
          releaseVideoLockIfIdle();
        }
      } catch (err) {
        consecutiveFailCount += 1;
        console.warn(`[useMangaStore] 视频状态轮询失败（第 ${consecutiveFailCount} 次）:`, err);
        if (consecutiveFailCount >= MAX_CONSECUTIVE_FAILS) {
          // 放弃轮询必须收敛为终态：否则任务永久停留 generating，
          // video_gen 功能锁与 videoGenerating 永不释放（P1-04 测试发现的挂死缺陷）
          stopVideoPoll(taskId);
          const msg = '视频任务状态同步失败，已停止轮询';
          patchVideoTask(taskId, { status: 'error', error: msg });
          useTaskStore.getState().failTask(taskId, msg);
          syncRowGenerationStatus(rowId, 'error');
          useAppStore.getState().showToast(msg, 'error');
          releaseVideoLockIfIdle();
        }
      }
    };
    const tickFn = (): void => void tick();
    // 按当前可见性取间隔注册；可见性变化由 videoPoller 模块级监听统一重建
    registerVideoPoll(taskId, tickFn);
    tickFn();
  };

  return {
    videoTasks: [],
    videoGenerating: false,

    generateVideo: async (row, opts) => {
      // 功能互斥前置检查（规格 §6.1：与绘画/训练等互斥）
      const appStore = useAppStore.getState();
      if (!appStore.setActiveFeature('video_gen')) {
        return false;
      }
      const description =
        row.description || row.original_dialogue || `分镜 ${row.shot_number}`;
      try {
        // 当前关键帧图随请求进后端：I2V 首帧 / 分镜网格拆格的载体
        //（此前恒为空串，I2V 通道从未接通——2026-08-25 修复）
        const screenshotB64 = await fetchCurrentKeyframeB64(row.id);
        const res = await mangaApi.generateVideo({
          storyboard_row_id: row.id,
          description,
          screenshot_4in1: screenshotB64,
          // 引擎点名透传（P1 2026-08-29）：如 "h3_director" = MiniMax H3 导演台
          model_override: opts?.modelOverride || undefined,
        });
        const degraded =
          typeof (res as { degraded?: unknown }).degraded === 'boolean'
            ? ((res as { degraded?: boolean }).degraded as boolean)
            : undefined;
        const degradeReason =
          typeof (res as { degrade_reason?: unknown }).degrade_reason === 'string'
            ? ((res as { degrade_reason?: string }).degrade_reason as string)
            : undefined;
        const entry: MangaVideoTask = {
          task_id: res.task_id,
          row_id: row.id,
          shot_number: row.shot_number,
          description,
          status: 'generating',
          progress: 0,
          degraded,
          degrade_reason: degradeReason,
        };
        set((state) => ({ videoTasks: [...state.videoTasks, entry], videoGenerating: true }));
        // 注册全局任务（状态栏进度可见）
        useTaskStore.getState().upsertTask({
          id: res.task_id,
          type: 'video_gen',
          name: `视频生成 分镜${row.shot_number}`,
          status: 'running',
          progress: 0,
          pausable: false,
        });
        syncRowGenerationStatus(row.id, 'generating');
        startVideoPoll(res.task_id, row.id);
        return true;
      } catch (err) {
        const msg =
          err && typeof err === 'object' && 'message' in err
            ? (err as { message: string }).message
            : '视频生成失败';
        useAppStore.getState().showToast(msg, 'error');
        appStore.releaseActiveFeature();
        set({ videoGenerating: false });
        return false;
      }
    },

    /** 拉取项目历史视频任务（openProject 调用）：行级取最新一条，
     * 仅合入 store 尚不存在的终态任务（done 显示视频框 / error 显示
     * 失败重试）；在途轮询任务不受影响。 */
    loadVideoHistory: async (projectId) => {
      try {
        const res = await mangaApi.listVideoTasks(projectId);
        // 列表按 created_at 倒序：每行首条即最新
        const latestByRow = new Map<string, mangaApi.VideoTaskRecord>();
        for (const t of res.items) {
          if (t.row_id && !latestByRow.has(t.row_id)) {
            latestByRow.set(t.row_id, t);
          }
        }
        const existing = new Set(get().videoTasks.map((t) => t.task_id));
        const historyTasks: MangaVideoTask[] = [];
        latestByRow.forEach((t) => {
          if (existing.has(t.task_id)) return;
          // pending/generating 历史残留无轮询器接管，不进列表
          //（避免永久卡在"生成中"假进度）
          if (t.status !== 'done' && t.status !== 'error') return;
          historyTasks.push({
            task_id: t.task_id,
            row_id: t.row_id,
            created_at: t.created_at,
            shot_number: t.shot_number,
            description: '',
            status: t.status as MangaVideoTask['status'],
            progress: t.status === 'done' ? 1 : t.progress,
            error: t.status === 'error' ? '上次生成失败，点击重试' : undefined,
            download_url:
              t.status === 'done'
                ? mangaApi.getVideoDownloadUrl(t.task_id)
                : undefined,
          });
        });
        if (historyTasks.length > 0) {
          set((state) => ({
            videoTasks: [...historyTasks, ...state.videoTasks],
          }));
        }
      } catch (err) {
        // 历史加载失败不阻塞工作区（CONSOLE 级留痕）
        reportBgError('videoSlice.loadVideoHistory', err);
      }
    },

    fetchVideoStatus: async (taskId) => {
      try {
        const st = await mangaApi.getVideoStatus(taskId);
        patchVideoTask(taskId, {
          status: st.status,
          progress: st.progress,
          error: st.error,
        });
        releaseVideoLockIfIdle();
      } catch {
        /* 忽略：保留上次快照 */
      }
    },

    removeVideoTask: (taskId) => {
      stopVideoPoll(taskId);
      set((state) => ({
        videoTasks: state.videoTasks.filter((t) => t.task_id !== taskId),
      }));
      releaseVideoLockIfIdle();
    },

    cancelVideo: async (taskId) => {
      await mangaApi.cancelVideo(taskId);
      stopVideoPoll(taskId);
      patchVideoTask(taskId, { status: 'error', error: '已手动取消' });
      const task = get().videoTasks.find((t) => t.task_id === taskId);
      if (task) {
        useTaskStore.getState().failTask(taskId, '已手动取消');
        syncRowGenerationStatus(task.row_id, 'error');
      }
      releaseVideoLockIfIdle();
    },
  };
};
