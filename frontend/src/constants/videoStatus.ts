// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 视频任务状态常量（TASK-P1-04 抽取，原 MangaWorkspace 内联）
 * --------------------------------------------------------------------------
 * 单一真源：后端 video_tasks.status 取值（src/api/manga/ 包）。
 * 状态文案集中化第一步（P2-07 将继续收编其余枚举标签）。
 * 一致性测试：src/constants/videoStatus.test.ts 直读后端源码双向核对。
 * ========================================================================== */

/** 视频任务状态全集（对齐后端 video_tasks.status：pending/generating/done/error/cancelled；
 *  2026-10-19 补 SR 超分任务四态 running/busy/failed/none——一致性测试整包扫描
 *  src/api/manga/，超分列与任务列共用前端承接面，超集接受防轮询响应被误拒） */
export const VIDEO_TASK_STATUSES = [
  'pending',
  'generating',
  'done',
  'error',
  'cancelled',
  'running',
  'busy',
  'failed',
  'none',
] as const;

/** 视频任务状态类型（useMangaStore.MangaVideoTask.status 引用此类型） */
export type VideoTaskStatus = (typeof VIDEO_TASK_STATUSES)[number];

/** 视频任务状态中文标签（任务条/记录弹窗/超分列共用） */
export const VIDEO_STATUS_LABELS: Record<VideoTaskStatus, string> = {
  pending: '排队中',
  generating: '生成中',
  done: '已完成',
  error: '失败',
  cancelled: '已取消',
  running: '进行中',
  busy: '引擎忙碌',
  failed: '超分失败',
  none: '无任务',
};
