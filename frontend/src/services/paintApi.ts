// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * paintApi —— 绘画 API 瘦身版（W3-C 步5 清退，2026-09-13）
 * --------------------------------------------------------------------------
 * 旧绘画页 UI 已由 AI 漫画页取代（/paint 路由挂 ComicPage），旧栈生成/
 * 视频/历史端点的消费面清零，本文件仅保留存活链路：
 * - GET /draw/status   引擎状态（waitPaintReady 漫剧预热链 + PaintWarmupModal）
 * - GET /draw/models   绘画模型列表（ModuleModelConfig 直调）
 * 生成链已统一 ComfyUI klein 栈（backend/api/draw.py comfy 分支），
 * 后端端点保留兼容，前端不再消费。
 * ========================================================================== */

import { get } from './api';
import { parseWith, DrawStatusRespSchema } from './schema';

/** 任务结果（/draw/result/{task_id}；保留供历史兼容） */
export interface DrawTaskResult {
  task_id: string;
  status: string;
  image?: string;
  file_path?: string;
  [k: string]: unknown;
}

/** 查询任务状态/结果；不传 taskId 时返回引擎状态 */
export function getStatus(taskId: string): Promise<DrawTaskResult>;
export function getStatus(): Promise<Record<string, unknown>>;
export function getStatus(taskId?: string) {
  return taskId
    ? get<unknown>(`/draw/result/${taskId}`).then(
        // 批 3-3c：queue_position 链入口过 Zod（FRONTEND_PARSE_ERROR 走错误三分法）
        (d) => parseWith(DrawStatusRespSchema, d, '绘画任务状态') as unknown as DrawTaskResult,
      )
    : get<Record<string, unknown>>('/draw/status');
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
