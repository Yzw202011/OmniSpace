// 本项目仅供学习使用，商业授权请+Q 3559331368
/* 视频状态轮询基础设施（TASK-P2-01 自 useMangaStore 切出）
 * B7 步3（2026-09-14）：降频/可见性语义收敛到 services/poller 工厂——
 * 逐任务一个 VisibilityPoller 实例（恢复可见立即补 tick + 注册即查）。 */

import { createVisibilityPoller } from '@/services/poller';

/** 轮询条目：工厂实例（可见性降频语义内聚在工厂） */
export const videoPollers = new Map<
  string,
  { stop: () => void }
>();

/** 停止指定任务轮询（与 startVideoPoll 对称清理实例） */
export function stopVideoPoll(taskId: string): void {
  const entry = videoPollers.get(taskId);
  if (entry) {
    entry.stop();
    videoPollers.delete(taskId);
  }
}

/** 注册轮询条目（由 videoSlice 的 startVideoPoll 调用）。
 *  注意：videoSlice 注册后自带一次手动 tickFn()（既有行为）——工厂
 *  tickOnStart 必须保持 false，否则双重立即 tick 会跳过首个状态。 */
export function registerVideoPoll(taskId: string, tick: () => void): void {
  stopVideoPoll(taskId); // 幂等：重注册先清旧实例
  const poller = createVisibilityPoller({
    name: `video-${taskId}`,
    intervalMs: 2000,
    hiddenIntervalMs: 10000,
    tick,
    tickOnVisible: false, // B7 二分：临时关闭（基线对照）
  });
  poller.start();
  videoPollers.set(taskId, { stop: () => poller.stop() });
}
