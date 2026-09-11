// 本项目仅供学习使用，商业授权请+Q 3559331368
/* 视频状态轮询基础设施（TASK-P2-01 自 useMangaStore 切出）
 * 模块级单例：定时器 Map + 可见性降频监听，随应用生命周期存在。 */

/** 视频状态轮询间隔（后端无 WS 推送，轮询为唯一真实进度来源） */
const VIDEO_POLL_INTERVAL = 2000;
/** 页面隐藏时的降频轮询间隔（后台节流；恢复可见时立即补一次 tick） */
const VIDEO_POLL_INTERVAL_HIDDEN = 10000;

/** 轮询条目：定时器句柄 + tick 回调（可见性变化时按当前间隔重建定时器） */
interface VideoPollerEntry {
  handle: ReturnType<typeof setInterval>;
  tick: () => void;
}

/** 任务 ID → 轮询条目（模块级，避免随 set 重建） */
export const videoPollers = new Map<string, VideoPollerEntry>();

/** 当前轮询间隔：页面隐藏降频 10s，可见常速 2s */
function currentVideoPollInterval(): number {
  return typeof document !== 'undefined' && document.hidden
    ? VIDEO_POLL_INTERVAL_HIDDEN
    : VIDEO_POLL_INTERVAL;
}

/** 停止指定任务轮询（与 startVideoPoll 对称清理定时器与条目） */
export function stopVideoPoll(taskId: string): void {
  const entry = videoPollers.get(taskId);
  if (entry) {
    clearInterval(entry.handle);
    videoPollers.delete(taskId);
  }
}

/** 注册轮询条目（由 videoSlice 的 startVideoPoll 调用） */
export function registerVideoPoll(taskId: string, tick: () => void): void {
  videoPollers.set(taskId, {
    handle: setInterval(tick, currentVideoPollInterval()),
    tick,
  });
}

// 模块级可见性单例监听（随应用生命周期存在，仅注册一次）：
// 隐藏 → 全体轮询器按 10s 降频重建；恢复可见 → 按 2s 重建并立即补一次 tick。
// 轮询条目的生命周期仍由 startVideoPoll/stopVideoPoll 对称管理，监听器本身无泄漏。
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    const interval = currentVideoPollInterval();
    const hidden = document.hidden;
    for (const entry of videoPollers.values()) {
      clearInterval(entry.handle);
      entry.handle = setInterval(entry.tick, interval);
      if (!hidden) entry.tick();
    }
  });
}
