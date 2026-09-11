// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 状态文案集中营（TASK-P2-07，审计 P13）
 * --------------------------------------------------------------------------
 * 单一真源：全站状态枚举 → 中文标签映射。组件内禁止再散落状态标签字面量。
 *
 * 枚举来源（文案 key 与后端绑定，绑定测试 statusLabels.test.ts 双向核对）：
 *  - TRAIN：后端 data/models.py TrainStatus（queued/training/evaluating/…）
 *           经 services/learnApi.ts TRAIN_STATUS_MAP 适配为前端五值
 *  - ROW_GEN：后端 data/models.py GenerationStatus + 前端批量操作扩展 skipped
 *  - SAVE：前端本地保存状态机（ShotSaveStatus，无后端对应）
 *  - MODEL_RUNTIME：模型运行态（ModelManager 遥测，ready/not_ready/loading/error）
 *  - MODEL_AVAILABLE：GET /manga/models/available（offload 为前端显存闸门派生态）
 *  - LEARN_SESSION：知识学习会话状态（learningApi.LearnSessionStatus.status）
 *  - VIDEO：见 ./videoStatus.ts（TASK-P1-04 已建，此处转发出口）
 * ========================================================================== */

import type { AvailableModel, StoryboardGenerationStatus, TrainTask } from '@/types';
import { VIDEO_STATUS_LABELS, VIDEO_TASK_STATUSES, type VideoTaskStatus } from './videoStatus';

export { VIDEO_STATUS_LABELS, VIDEO_TASK_STATUSES };
export type { VideoTaskStatus };

/** 训练任务状态类型键（TrainTask['status'] 的名义导出，TopBar 等泛 status 字段断言用） */
export type TrainStatusKey = TrainTask['status'];

/** 训练任务状态 → 中文标签（TrainTask.status；LearnView/RightPanel/StylePage 共用） */
export const TRAIN_STATUS_LABELS: Record<TrainTask['status'], string> = {
  pending: '排队中',
  running: '训练中',
  done: '已完成',
  error: '失败',
  cancelled: '已取消',
};

/** 训练任务状态 → CSS 类名（app.css learn-status-*，LearnView 徽标） */
export const TRAIN_STATUS_CLASSES: Record<TrainTask['status'], string> = {
  pending: 'learn-status-queued',
  running: 'learn-status-running',
  done: 'learn-status-completed',
  error: 'learn-status-failed',
  cancelled: 'learn-status-cancelled',
};

/** 分镜行生成状态 → 中文标签（StoryboardGenerationStatus；VideoDrawer/InspectorPanel 共用） */
export const ROW_GEN_STATUS_LABELS: Record<StoryboardGenerationStatus, string> = {
  pending: '待生成',
  generating: '生成中',
  done: '已完成',
  error: '失败',
  skipped: '已跳过',
};

/** 分镜行生成状态 → 徽标色变体（VideoDrawer/InspectorPanel 共用） */
export const ROW_GEN_STATUS_BADGE: Record<StoryboardGenerationStatus, string> = {
  pending: 'neutral',
  generating: 'warning',
  done: 'success',
  error: 'error',
  skipped: 'neutral',
};

/** 保存状态点文案（ShotSaveStatus；idle 无文案） */
export const SAVE_STATUS_LABELS: Record<
  'idle' | 'dirty' | 'saving' | 'saved' | 'error',
  string
> = {
  idle: '',
  dirty: '未保存',
  saving: '保存中…',
  saved: '已保存',
  error: '保存失败',
};

/** 模型运行态 → 中文标签（ModelManager 遥测状态） */
export const MODEL_RUNTIME_STATUS_LABELS: Record<string, string> = {
  ready: '就绪',
  not_ready: '未就绪',
  loading: '加载中',
  error: '错误',
};

/** 可用模型状态 → 中文标签（AvailableModel.status）：
 *  ready=已加载立即可用；downloaded=已下载可按需加载（选中后生成时后端
 *  ensure_loaded 自动装载/换载）；offload=需求超总显存物理装不下 */
export const MODEL_AVAILABLE_STATUS_LABELS: Record<AvailableModel['status'], string> = {
  ready: '可用',
  loading: '加载中',
  not_installed: '未安装',
  downloaded: '已下载',
  offload: '需卸载',
};

/** 知识学习会话状态 → 中文标签（learningApi.LearnSessionStatus.status） */
export const LEARN_SESSION_STATUS_LABELS: Record<string, string> = {
  idle: '空闲',
  running: '进行中',
  paused: '已暂停',
  stopped: '已停止',
};
