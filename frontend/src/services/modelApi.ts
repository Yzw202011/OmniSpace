/* ==========================================================================
 * OmniSpace AI v2.1 —— 模型管理 API（规格 §4.5 模型管理端点）
 * --------------------------------------------------------------------------
 * 严格对齐后端 backend/api/models.py 实际路由（/v1 前缀由 api.ts 拼接）：
 * - GET    /models                  模型列表（groups + models + downloaded 标记）
 * - GET    /models/{model_id}       模型详情
 * - POST   /models/import           导入模型（body: {path}，ModelImportRequest）
 * - POST   /models/{model_id}/verify  SHA256 校验
 * - DELETE /models/{model_id}       从注册表移除模型
 * - PUT    /models/select           手动选择模型（body: {feature, model_id}）
 * - POST   /models/load             加载到 GPU（body: {model_id, category?}）
 * - POST   /models/unload           从 GPU 卸载（body: {model_id}）
 *
 * 已移除悬空端点（后端不存在，调用必然 404）：
 *   /models/replace、/models/rollback、/models/cache/lock、
 *   /models/vram、/models/download/status
 * VRAM 使用率请改从 useHardwareStore（realtime.vram_percent / synergy.vram）读取。
 * ========================================================================== */

import { z } from 'zod';
import { get, post, put, del } from './api';
import type { QueryParams } from './api';
import { parseWith, ModelItemSchema } from './schema';
import type { ModelInfo, ModelCategory } from '@/types';

/** 模型列表响应（后端返回分组 + 平铺两种结构） */
export interface ModelListResponse {
  groups: Record<string, ModelInfo[]>;
  models: ModelInfo[];
  total: number;
  downloaded: number;
}

/** 模型列表（可按类别过滤）；后端返回 {groups, models, total, downloaded}，此处解包为平铺数组（Zod 逐项校验） */
export async function listModels(query?: QueryParams): Promise<ModelInfo[]> {
  const res = await get<unknown>('/models', query);
  const raw = Array.isArray(res)
    ? res
    : ((res as ModelListResponse | null)?.models ?? []);
  return parseWith(z.array(ModelItemSchema), raw, '模型列表') as unknown as ModelInfo[];
}

/** 模型详情（Zod 校验） */
export async function getModelDetail(modelId: string): Promise<ModelInfo> {
  const res = parseWith(
    ModelItemSchema,
    await get<unknown>(`/models/${encodeURIComponent(modelId)}`),
    '模型详情',
  );
  return res as unknown as ModelInfo;
}

/* ------------------------------ 导入/校验/删除 ------------------------------ */

/**
 * 导入模型请求体（对齐后端 ModelImportRequest：仅接受 path 字段，
 * 后端自行推断名称/类别/大小，多传字段会被 pydantic 忽略）。
 */
export interface ImportModelBody {
  /** 权重文件或目录路径（相对路径以项目根解析） */
  path: string;
}

/** 导入模型（路径校验，不存在返回 30001） */
export function importModel(body: ImportModelBody) {
  return post<ModelInfo>('/models/import', body);
}

/** 校验结果（对齐后端 models_verify 返回） */
export interface VerifyResult {
  model_id: string;
  sha256: string;
  verified: boolean;
}

/** 校验模型完整性（SHA256；目录模型对特征文件做组合指纹） */
export function verifyModel(modelId: string) {
  return post<VerifyResult>(`/models/${encodeURIComponent(modelId)}/verify`, {});
}

/** 删除模型（从注册表移除；已加载模型后端先自动卸载） */
export function deleteModel(modelId: string) {
  return del<{ deleted: string }>(`/models/${encodeURIComponent(modelId)}`);
}

/* ------------------------------ 手动选择 ------------------------------ */

/** 可选择模型的功能名（后端 models_select 合法值：dialog/paint/video/voice） */
export type SelectableFeature = 'dialog' | 'paint' | 'video' | 'voice';

/** 手动选择请求体（对齐后端 ModelSelectRequest） */
export interface SelectModelBody {
  feature: SelectableFeature;
  model_id: string;
}

/** 选择响应（含全部 feature → model_id 绑定快照） */
export interface SelectModelResult {
  feature: string;
  model_id: string;
  selections: Record<string, string>;
}

/**
 * 模型类别 → 可选择功能名映射。
 * 与后端 _category_to_feature 对齐：dialog/language→dialog，vision→paint，
 * video→video，voice→voice；3d/auxiliary 不可手动选择，返回 null。
 */
export function categoryToFeature(category: ModelCategory): SelectableFeature | null {
  switch (category) {
    case 'dialog':
    case 'language':
      return 'dialog';
    case 'vision':
      return 'paint';
    case 'video':
      return 'video';
    case 'voice':
      return 'voice';
    default:
      return null;
  }
}

/** 手动选择模型为某功能当前使用（PUT /models/select，规格 §14 约束10） */
export function selectModel(body: SelectModelBody) {
  return put<SelectModelResult>('/models/select', body);
}

/* ------------------------------ 加载/卸载 ------------------------------ */

/** 加载请求体（对齐后端 ModelLoadRequest；category 缺省时后端自动推断） */
export interface LoadModelBody {
  model_id: string;
  category?: string;
}

/** 加载/卸载响应（对齐后端 models_load / models_unload 返回） */
export interface LoadStateResult {
  model_id: string;
  category?: string;
  loaded: boolean;
  loaded_models?: Array<{ model_id: string; [key: string]: unknown }>;
}

/**
 * 加载模型到 GPU 显存。
 * 错误码（20xxx 段）：20011 未下载 / 20014 功能互斥阻断 /
 * 20013 显存不足 / 20010 其他加载失败。
 */
export function loadModel(body: LoadModelBody) {
  return post<LoadStateResult>('/models/load', body);
}

/** 从 GPU 卸载模型（未加载时返回 20012） */
export function unloadModel(modelId: string) {
  return post<LoadStateResult>('/models/unload', { model_id: modelId });
}

export default {
  listModels,
  getModelDetail,
  importModel,
  verifyModel,
  deleteModel,
  selectModel,
  categoryToFeature,
  loadModel,
  unloadModel,
};
