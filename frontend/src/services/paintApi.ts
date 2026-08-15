/* ==========================================================================
 * OmniSpace AI v2.1 —— 绘画 API（后端真实端点契约，backend/api/draw.py）
 * --------------------------------------------------------------------------
 * - POST   /draw/generate           文生图：{"task_id"}，进度走 WS/轮询
 * - POST   /draw/img2img            图生图（init_image base64 + strength）
 * - GET    /draw/result/{task_id}   任务状态/结果（done 时含 image base64）
 * - GET    /draw/status             绘画引擎状态
 * - GET    /draw/history            生成历史（paint_history 表分页）
 * - GET    /draw/models             绘画模型路由表 + 本地可用状态
 * - GET    /draw/image/{filename}   生成图回读（历史画廊缩略图）
 * - POST   /draw/controlnet/preview ControlNet 预处理预览（未装模型时 501）
 * 注意：后端暂无 LoRA 列表 / 图片上传 / 收藏端点，相关 UI 不展示。
 * ========================================================================== */

import { get, post } from './api';
import type { QueryParams } from './api';
import type { PaintRequest, Paginated } from '@/types';

/* ------------------------------ 生成 ------------------------------ */

/** 文生图生成 */
export function generate(params: PaintRequest) {
  return post<{ task_id: string }>('/draw/generate', params);
}

/** 图生图（init_image base64 + strength） */
export function img2img(params: PaintRequest) {
  return post<{ task_id: string }>('/draw/img2img', params);
}

/* ------------------------------ 状态/结果 ------------------------------ */

/** 任务查询响应（/draw/result/{task_id}） */
export interface DrawTaskResult {
  task_id: string;
  type?: string;
  /** pending/running/done/error */
  status: string;
  /** 进度 0~100 */
  percent: number;
  step: number;
  seed?: number;
  model?: string;
  sampler?: string;
  elapsed_ms?: number;
  created_at?: number;
  /** done 时返回：相对路径 generated/images/<task_id>.png */
  file_path?: string;
  /** done 时返回：图片 PNG base64（无 data: 前缀） */
  image?: string;
  /** error 时返回 */
  error?: string;
  code?: number;
}

/** 查询任务状态/结果；不传 taskId 时返回引擎状态 */
export function getStatus(taskId: string): Promise<DrawTaskResult>;
export function getStatus(): Promise<Record<string, unknown>>;
export function getStatus(taskId?: string) {
  return taskId
    ? get<DrawTaskResult>(`/draw/result/${taskId}`)
    : get<Record<string, unknown>>('/draw/status');
}

/* ------------------------------ 历史 ------------------------------ */

/** 后端历史条目（paint_history 表行） */
export interface DrawHistoryItem {
  task_id: string;
  prompt: string;
  negative: string;
  params: Record<string, unknown>;
  file_path: string;
  seed: number;
  created_at: number;
}

/** 历史列表 */
export function listHistory(query?: QueryParams) {
  return get<Paginated<DrawHistoryItem>>('/draw/history', query);
}

/* ------------------------------ 模型 ------------------------------ */

/** 绘画模型项（/draw/models） */
export interface DrawModelItem {
  id: string;
  name: string;
  category: string;
  min_vram_gb: number;
  /** ready / not_installed */
  status: string;
  /** 本地映射模型目录 id（sdxl 系列 → sdxl-base-1.0） */
  local_model: string;
}

/** 绘画模型列表（路由表 + 本地可用状态） */
export function listModels() {
  return get<{ items: DrawModelItem[]; total: number }>('/draw/models');
}

/* ------------------------------ ControlNet ------------------------------ */

/** ControlNet 预处理预览（未装模型时后端返回 501 友好错误） */
export function controlnetPreview(body: {
  type: string;
  image: string;
  params?: Record<string, unknown>;
}) {
  return post<{ url: string; method: string }>('/draw/controlnet/preview', body);
}

export default {
  generate,
  img2img,
  getStatus,
  listHistory,
  listModels,
  controlnetPreview,
};
