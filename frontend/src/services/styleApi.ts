// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 视频风格 LoRA API（对齐后端 src/api/style.py 真实端点）
 * --------------------------------------------------------------------------
 * 全部对接 /v1/style/* 专用端点（审计修复：不再借用 /learn/* 知识训练管线）：
 *   POST /style/upload              上传素材（视频抽帧/图片收录）→ dataset_id
 *   GET  /style/datasets            数据集列表
 *   GET  /style/datasets/{id}       数据集样本统计 {total, sufficient}
 *   POST /style/train               触发风格 LoRA 训练（QLoRA 入队）
 *   GET  /style/tasks               训练任务列表
 *   GET  /style/tasks/{task_id}     训练任务详情（真实状态/进度轮询）
 *   GET  /style/versions            风格 LoRA 版本列表（含 is_current）
 *   POST /style/rollback            版本回滚（置 current 指针）
 *   POST /style/preview             风格预览（base64 JPEG 帧；未就绪如实 80013）
 *   GET  /style/status              服务状态（基座/FFmpeg/队列/当前版本）
 * 诚实门控：LTX-2 基座权重未随包分发时，/train 返回 80010、/preview 返回 80013，
 * 前端如实透出错误文案，不伪造进度或预览图。
 * ========================================================================== */

import { z } from 'zod';
import { parseWith } from './schema';
import { get, post, upload } from './api';
import type { TrainTask } from '@/types';

/* ------------------------------ 类型契约 ------------------------------ */

/** 风格数据集（GET /style/datasets 列表项） */
export interface StyleDataset {
  dataset_id: string;
  frame_count: number;
  dataset_path: string;
  /** 服务端更新时间戳（秒） */
  updated_at: number;
}

/** 数据集样本统计（GET /style/datasets/{id}） */
export interface StyleDatasetStats {
  dataset_id: string;
  total: number;
  sufficient: boolean;
}

/** 风格训练任务（/style/train、/style/tasks 返回行） */
export interface StyleTask {
  id: string;
  name: string;
  dataset_id: string;
  style_prompt: string;
  base_model: string;
  lora_rank: number;
  lora_alpha: number;
  learning_rate: number;
  epochs: number;
  /** queued / training / evaluating / done / error */
  status: string;
  /** 0~1 */
  progress: number;
  version: string;
  error: string;
  created_at: number;
  updated_at: number;
}

/**
 * 风格 LoRA 版本（GET /style/versions 列表项；
 * version 为版本名，meta.json 字段随版本下发，质量分/状态为可选）
 */
export interface StyleLoraVersion {
  /** 版本名（如 v3），与后端 rollback/preview 入参一致 */
  version: string;
  /** 展示名（meta.name，缺省回退 version） */
  name: string;
  /** adapter 权重是否完整 */
  has_adapter: boolean;
  /** 是否当前生效版本 */
  is_current: boolean;
  /** 风格提示词（meta.style_prompt） */
  style_prompt?: string;
  /** 质量评分 0~100（评估后下发；未评估为 null/undefined） */
  quality_score?: number | null;
  /** 版本状态（registered / pending_review） */
  status?: string;
  /** 训练样本数（meta.data_count） */
  data_count?: number;
  /** 创建时间戳（秒） */
  created_at?: number;
}

/** 风格预览结果（POST /style/preview 成功返回） */
export interface StylePreviewResult {
  version: string;
  /** base64 JPEG 帧序列 */
  frames: string[];
  prompt: string;
  count: number;
}

/** 风格服务状态（GET /style/status） */
export interface StyleStatus {
  base_model: string;
  base_ready: boolean;
  base_reason: string;
  ffmpeg_available: boolean;
  /** AV1 编码器遥测：nvenc（硬编）/ svt（软编）/ none（ffmpeg 无 AV1）/ off（无 ffmpeg） */
  av1_encoder?: 'nvenc' | 'svt' | 'none' | 'off';
  /** 编码硬件分类（rtx50 / rtx40 / rtx30 / nvidia_other / amd / cpu） */
  encoder_hw?: string;
  active_task: string | null;
  queue_size: number;
  current_version: string;
  versions_total: number;
  datasets_total: number;
  min_samples: number;
}

/* ------------------------------ 素材与数据集 ------------------------------ */

/** 上传视频/图片素材（POST /style/upload，multipart；视频由后端 FFmpeg 抽帧） */
export async function uploadStyleDataset(
  file: File,
): Promise<{ dataset_id: string; kind: string; frame_count: number; dataset_path: string }> {
  const formData = new FormData();
  formData.append('file', file);
  return upload('/style/upload', formData);
}

/** 数据集列表（GET /style/datasets，按更新时间倒序） */
export async function listStyleDatasets(): Promise<StyleDataset[]> {
  const res = await get<{ items: StyleDataset[]; total: number }>('/style/datasets');
  return res.items ?? [];
}

/** 数据集样本统计（GET /style/datasets/{id}，训练充分性判定） */
export function getStyleDatasetStats(datasetId: string): Promise<StyleDatasetStats> {
  return get<StyleDatasetStats>(`/style/datasets/${encodeURIComponent(datasetId)}`);
}

/* ------------------------------ 训练 ------------------------------ */

/**
 * 发起风格 LoRA 训练（POST /style/train）。
 * batch_size 不接受入参：后端按显存安全线固定 batch=1 + 梯度累积 4
 * （style_lora_service DEFAULT_STYLE_CONFIG），前端不提供该控件（诚实标注）。
 */
export async function startStyleTrain(body: {
  dataset_id: string;
  name: string;
  style_prompt: string;
  rank: number;
  alpha: number;
  learning_rate: number;
  epochs: number;
}): Promise<StyleTask> {
  return post<StyleTask>('/style/train', {
    dataset_id: body.dataset_id,
    name: body.name,
    style_prompt: body.style_prompt,
    lora_rank: body.rank,
    lora_alpha: body.alpha,
    learning_rate: body.learning_rate,
    epochs: body.epochs,
  });
}

/** 训练任务详情（GET /style/tasks/{id}，2 秒轮询） */
export function getStyleTask(taskId: string): Promise<StyleTask> {
  return get<StyleTask>(`/style/tasks/${encodeURIComponent(taskId)}`);
}

/** 训练控制端点返回行（STYLE-017：_task_control） */
export interface StyleTaskControlResult {
  task_id: string;
  action: 'pause' | 'resume' | 'cancel';
  status: string;
}

/** 暂停训练任务（epoch 检查点挂起，状态 → paused） */
export function pauseStyleTask(taskId: string): Promise<StyleTaskControlResult> {
  return post<StyleTaskControlResult>(
    `/style/tasks/${encodeURIComponent(taskId)}/pause`, {});
}

/** 恢复已暂停任务（清除挂起旗标，状态 → training） */
export function resumeStyleTask(taskId: string): Promise<StyleTaskControlResult> {
  return post<StyleTaskControlResult>(
    `/style/tasks/${encodeURIComponent(taskId)}/resume`, {});
}

/** 取消训练任务（训练中下个 epoch 检查点中断；已终结任务幂等） */
export function cancelStyleTask(taskId: string): Promise<StyleTaskControlResult> {
  return post<StyleTaskControlResult>(
    `/style/tasks/${encodeURIComponent(taskId)}/cancel`, {});
}

/** 后端风格任务状态 → 前端 TrainTask.status
 *  （2026-09-16 批6：补 paused/cancelled——此前两态漏映射，
 *  取消后的任务在进度卡片被错误显示为「排队中」） */
const STYLE_STATUS_MAP: Record<string, TrainTask['status']> = {
  queued: 'pending',
  training: 'running',
  evaluating: 'running',
  paused: 'paused',
  done: 'done',
  error: 'error',
  cancelled: 'cancelled',
};

/** StyleTask → 前端通用 TrainTask（复用进度卡片 UI） */
export function mapStyleTask(t: StyleTask): TrainTask {
  const rawProgress = typeof t.progress === 'number' ? t.progress : 0;
  return {
    id: t.id,
    type: 'style_lora',
    name: t.name || '风格训练任务',
    status: STYLE_STATUS_MAP[t.status] ?? 'pending',
    progress: rawProgress > 1 ? rawProgress / 100 : rawProgress,
    priority: 'manual',
    target_model: t.base_model,
    dataset: t.dataset_id,
    hyperparams: {
      lora_rank: t.lora_rank,
      lora_alpha: t.lora_alpha,
      learning_rate: t.learning_rate,
      epochs: t.epochs,
    },
    error: t.error || undefined,
    created_at: t.created_at ?? Date.now(),
    updated_at: t.updated_at,
  };
}

/* ------------------------------ 版本 / 回滚 / 预览 / 状态 ------------------------------ */

/** 版本列表（GET /style/versions；name 缺省时回退 version） */
export async function listStyleVersions(): Promise<StyleLoraVersion[]> {
  const res = await get<{ items: StyleLoraVersion[]; total: number; current: string }>(
    '/style/versions',
  );
  return (res.items ?? []).map((v) => ({ ...v, name: v.name || v.version }));
}

/** 版本回滚（POST /style/rollback，置 current 指针；版本不存在返回 80012） */
export function rollbackStyle(version: string): Promise<{ version: string; current: string }> {
  return post('/style/rollback', { version });
}

/**
 * 风格预览（POST /style/preview）→ base64 JPEG 帧序列。
 * 基座/版本/依赖未就绪时后端如实返回 80013，由调用方透出，不伪造预览图。
 */
export function previewStyle(version?: string, maxFrames = 8): Promise<StylePreviewResult> {
  return post<StylePreviewResult>('/style/preview', {
    ...(version ? { version } : {}),
    max_frames: maxFrames,
  });
}

/** 服务状态（GET /style/status：基座就绪/FFmpeg/队列/当前版本/样本下限） */
export function getStyleStatus(): Promise<StyleStatus> {
  return get<StyleStatus>('/style/status');
}

export default {
  uploadStyleDataset,
  listStyleDatasets,
  getStyleDatasetStats,
  startStyleTrain,
  getStyleTask,
  mapStyleTask,
  listStyleVersions,
  rollbackStyle,
  previewStyle,
  getStyleStatus,
};

/* ── 版本高级操作（STYLE-024/025/032；2026-09-17 前端接线，Zod 校验） ── */

/** 克隆风格版本为新训练任务（POST /style/clone） */
export async function cloneStyleVersion(
  version: string,
  name = '',
): Promise<{ version: string; new_task_id: string }> {
  const res = await post<unknown>('/style/clone', { version, name });
  return parseWith(
    z.object({ version: z.string(), new_task_id: z.string() }).passthrough(),
    res, '克隆风格',
  );
}

/** 导出风格包（POST /style/export：tar.gz 含 adapter+meta+SHA256） */
export async function exportStyleVersion(
  version = '',
): Promise<{ export_path?: string; [k: string]: unknown }> {
  const res = await post<unknown>('/style/export',
    version ? { version } : {});
  return parseWith(z.object({}).passthrough(), res, '导出风格包');
}

/** 多版本权重线性融合（POST /style/merge：versions[] + weights[]） */
export async function mergeStyleVersions(
  versions: string[],
  weights: number[],
  name = '',
): Promise<{ [k: string]: unknown }> {
  const res = await post<unknown>('/style/merge', { versions, weights, name });
  return parseWith(z.object({}).passthrough(), res, '融合风格');
}

/** 风格模板（GET/POST /style/templates） */
export interface StyleTemplate {
  name: string;
  style_prompt?: string;
  lora_rank?: number;
  lora_alpha?: number;
  learning_rate?: number;
  epochs?: number;
  [k: string]: unknown;
}

export async function listStyleTemplates(): Promise<StyleTemplate[]> {
  const res = await get<unknown>('/style/templates');
  const raw = (res as { items?: unknown } | null)?.items ?? [];
  return parseWith(
    z.array(z.object({ name: z.string() }).passthrough()),
    raw, '风格模板',
  ) as StyleTemplate[];
}

export async function saveStyleTemplate(
  tpl: StyleTemplate,
): Promise<StyleTemplate> {
  const res = await post<unknown>('/style/templates', tpl);
  return parseWith(z.object({ name: z.string() }).passthrough(), res, '保存模板');
}
