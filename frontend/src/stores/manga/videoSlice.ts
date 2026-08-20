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

    generateVideo: async (row) => {
      // 功能互斥前置检查（规格 §6.1：与绘画/训练等互斥）
      const appStore = useAppStore.getState();
      if (!appStore.setActiveFeature('video_gen')) {
        return false;
      }
      const description =
        row.description || row.original_dialogue || `分镜 ${row.shot_number}`;
      try {
        const res = await mangaApi.generateVideo({
          storyboard_row_id: row.id,
          description,
          screenshot_4in1: '',
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
