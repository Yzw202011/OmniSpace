// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.1 —— 绘画 API（后端真实端点契约，src/api/draw.py）
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

import { get, post, del, API_BASE } from './api';
import type { QueryParams } from './api';
import { parseWith, DrawStatusRespSchema } from './schema';
import type { PaintRequest, Paginated } from '@/types';

/* ------------------------------ 生成 ------------------------------ */

/** 文生图生成（batch_size>1 时返回 task_ids 全量，task_id 恒为首个） */
export function generate(params: PaintRequest) {
  return post<{ task_id: string; task_ids?: string[]; batch?: number }>(
    '/draw/generate', params);
}

/** 图生图（init_image base64 + strength） */
export function img2img(params: PaintRequest) {
  return post<{ task_id: string }>('/draw/img2img', params);
}

/* ------------------------------ 视频生成（复用漫剧 /video/*） ------------------------------ */

/** 绘画视频生成请求体（I2V：文+图；T2V 模式不带 image_base64） */
export interface PaintVideoGenerateBody {
  /** 动作描述提示词 */
  description: string;
  /** 输入图 base64（无 data: 前缀）；缺省 = 纯文生视频 */
  image_base64?: string;
  /** 时长秒（1~20） */
  duration_seconds: number;
  /** 帧率（1~48） */
  fps: number;
  /** 分辨率 720p/1080p/2k/4k */
  resolution: string;
}

/** 发起绘画视频生成（POST /video/generate，复用漫剧视频管线） */
export function generatePaintVideo(body: PaintVideoGenerateBody) {
  return post<{ task_id: string; status: string; degraded?: boolean }>(
    '/video/generate',
    {
      storyboard_row_id: `paint_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
      description: body.description,
      screenshot_4in1: body.image_base64 ?? '',
      character_assets: [],
      resolution: body.resolution,
      fps: body.fps,
      duration_seconds: body.duration_seconds,
    },
  );
}

/** 视频任务状态响应 */
export interface PaintVideoStatus {
  task_id: string;
  status: string;
  progress: number;
  error?: string;
  degraded?: boolean;
  /** 预计剩余秒数（生成中且引擎已采样到步耗时才返回，2026-08-22） */
  eta_seconds?: number;
}

/** 轮询视频任务状态 */
export function getPaintVideoStatus(taskId: string) {
  return get<PaintVideoStatus>(`/video/${taskId}/status`);
}

/** 视频播放/下载 URL（后端流式返回 mp4） */
export function paintVideoUrl(taskId: string) {
  return `${API_BASE}/video/${taskId}/download`;
}

/* ------------------------------ 视频历史（2026-08-22 持久化修复） ------------------------------ */

/** 视频历史条目（后端 /video/history，storyboard_row_id 以 paint_ 开头的任务） */
export interface PaintVideoHistoryItem {
  task_id: string;
  /** i2v 纯图 / ti2v 文+图（后端按 description 是否为空推断） */
  mode: 'i2v' | 'ti2v';
  prompt: string;
  duration_seconds: number;
  fps: number;
  resolution: string;
  status: string;
  degraded?: boolean;
  created_at: number;
}

/** 拉取绘画模块视频生成历史（新→旧；跨浏览器/重开可见） */
export function getPaintVideoHistory(limit = 100) {
  return get<{ items: PaintVideoHistoryItem[]; total: number }>(
    '/video/history', { limit });
}

/** 删除视频历史记录（DB 行 + 已生成文件；仅限绘画来源任务） */
export function deletePaintVideoTask(taskId: string) {
  return del<{ task_id: string; deleted: boolean }>(
    `/video/history/${taskId}`);
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
  /** 排队位次（1 起，仅 pending 且仍在统一图像队列中时返回） */
  queue_position?: number;
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
    ? get<unknown>(`/draw/result/${taskId}`).then(
        // 批 3-3c：queue_position 链入口过 Zod（FRONTEND_PARSE_ERROR 走错误三分法）
        (d) => parseWith(DrawStatusRespSchema, d, '绘画任务状态') as unknown as DrawTaskResult,
      )
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

/** 删除单条历史（记录 + 图文件，后端 PAINT-050） */
export function deleteHistory(taskId: string) {
  return del<{ task_id: string; deleted: boolean; file_deleted: boolean }>(
    `/draw/history/${taskId}`);
}

/** 批量删除历史（上限 200，后端 PAINT-051） */
export function batchDeleteHistory(ids: string[]) {
  return post<{ deleted: number; files_deleted: number; missing: string[] }>(
    '/draw/history/batch-delete', { ids });
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
