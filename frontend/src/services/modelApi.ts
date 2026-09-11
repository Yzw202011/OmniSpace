// 本项目仅供学习使用，商业授权请+Q 3559331368
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
 * - GET    /models/vllm/status      vLLM 推理引擎运行状态（2026-08-21）
 * - POST   /models/vllm/stop        停止 vLLM 子进程回收显存（2026-08-21）
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

/** 彻底删除模型磁盘文件（卸载+删盘；DELETE /models/{id}/files，2026-08-20） */
export function purgeModelFiles(modelId: string) {
  return del<{ deleted: string; path: string; freed_gb: number }>(
    `/models/${encodeURIComponent(modelId)}/files`);
}

/* ------------------------------ 就绪总检 ------------------------------ */

/** 就绪总检模块行（GET /models/readiness；体验流 #3 绿灯/缺件指路） */
export interface ReadinessModule {
  key: string;
  label: string;
  ready: boolean;
  /** 内置件模块（随软件包发行，缺件只记 notes 不翻灰） */
  builtin: boolean;
  present: string[];
  missing: string[];
}

/** 就绪总检响应（对齐 backend/services/model_manager/readiness.py） */
export interface ModelReadiness {
  manifest_found: boolean;
  models_root: string;
  modules: ReadinessModule[];
  missing_count: number;
  all_ready: boolean;
  notes: string[];
}

/** 模型就绪总检：模块级绿/灰 + 缺件清单（「拖入→启动→首启激活→即用」验收门面） */
export function fetchModelReadiness() {
  return get<ModelReadiness>('/models/readiness');
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
 * 与后端 _category_to_feature 对齐：dialog/language/omni→dialog，
 * vision→paint，video→video，voice→voice；3d/auxiliary 不可手动选择，
 * 返回 null。omni（视觉语音全模态）归 dialog：语音/视频对话是对话
 * 模块的功能形态。
 */
export function categoryToFeature(category: ModelCategory): SelectableFeature | null {
  switch (category) {
    case 'dialog':
    case 'language':
    case 'omni':
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

/* --------------------- vLLM 推理引擎（2026-08-21） --------------------- */

/** vLLM 推理引擎运行状态（对齐后端 GET /models/vllm/status 返回 data） */
export interface VllmStatus {
  /** vLLM Python runtime 是否已安装（false 时无法启动） */
  runtime_installed: boolean;
  /** vLLM 推理子进程是否运行中 */
  running: boolean;
  /** 健康检查是否通过（运行中且 /health 探测成功） */
  healthy: boolean;
  /** P3 常驻热备：是否正在后台无阻塞预冷（进程未起、预热点火中） */
  booting?: boolean;
  /** 子进程 PID（未运行时为 null） */
  pid: number | null;
  /** 模型权重目录（空串表示未知） */
  model_dir: string;
  /** 对外服务模型名（served_model_name，空串表示未知） */
  served_name: string;
  /** OpenAI 兼容服务端口（0 表示未知） */
  port: number;
  /** 累计运行时长（秒，未运行为 0） */
  uptime_s: number;
  /** 最近一次错误信息（无错误为 null） */
  last_error: string | null;
}

/** 停止 vLLM 响应（对齐后端 models_vllm_stop 返回；未运行时幂等成功） */
export interface VllmStopResult {
  /** 是否已确认停止 */
  stopped: boolean;
}

/** 查询 vLLM 推理引擎运行状态（GET /models/vllm/status） */
export function getVllmStatus() {
  return get<VllmStatus>('/models/vllm/status');
}

/**
 * 停止 vLLM 推理子进程并回收显存（POST /models/vllm/stop）。
 * 未运行时后端幂等返回成功；启动请复用 loadModel（qwen3-vl-8b-awq / dialog）。
 */
export function stopVllm() {
  return post<VllmStopResult>('/models/vllm/stop', {});
}

/* --------------------- 模块切换资源调度（2026-08-21） --------------------- */

/** 模块资源释放结果（对齐后端 ModelManager.release_for_module 返回） */
export interface ModuleReleaseResult {
  /** 归一化后的目标模块功能名 */
  module: string;
  /** 是否在时间预算内完成全部释放（false=超预算部分释放） */
  completed: boolean;
  /** 已卸载模型 ID 列表 */
  freed_models: string[];
  /** 已卸载模型数 */
  freed_count: number;
  /** 释放显存量（GB） */
  freed_vram_gb: number;
  /** 跳过未卸载的模型（运行中任务/超预算） */
  skipped: string[];
  /** 是否终止了 vLLM 子进程（重显存模块切换按需终止） */
  vllm_stopped?: boolean;
  /** P3 常驻热备：是否保留 vLLM 子进程后台热备（轻量切换复用 worker） */
  vllm_kept_hot?: boolean;
  /** 后端实际耗时（ms） */
  duration_ms: number;
}

/**
 * 模块切换资源释放：其他模块 3 秒内释放显存/内存，优先供应目标模块。
 * 导航切换模块 / 进入漫剧项目时调用（fire-and-forget + Toast 反馈）。
 * 客户端超时 = 预算 + 2s 余量（后端自身也受预算约束）。
 */
export function releaseForModule(module: string, timeoutMs = 3000) {
  return post<ModuleReleaseResult>(
    '/models/release-for-module',
    { module, timeout_ms: timeoutMs },
    { timeout: timeoutMs + 2000 },
  );
}

/** 模块常驻模型预热结果（fire-and-forget，后端立即返回） */
export interface ModuleWarmupResult {
  feature: string;
  started: boolean;
  reason?: string;
}

/** 后台预热模块常驻模型（对话模块 vLLM 冷启动 ~157s，切入页面即点火） */
export function warmupFeature(feature: string, modelId?: string) {
  return post<ModuleWarmupResult>('/models/warmup', {
    feature,
    model_id: modelId,
  });
}

/* ------------------------------ 模块级模型选型配置 ------------------------------ */

/** 模块槽候选模型（配置界面勾选项） */
export interface ModuleModelCandidate {
  id: string;
  name: string;
  category: string;
  downloaded: boolean;
  loaded: boolean;
  min_vram_gb: number;
  purpose: string;
}

/** 单个模块槽的配置态（服务端真源 + 本地编辑态共用结构） */
export interface ModuleSlotConfig {
  allowed: string[];
  default: string;
}

/** GET/PUT /models/module-config 响应中的槽信息 */
export interface ModuleModelSlotInfo {
  slot: string;
  label: string;
  desc: string;
  hint: string;
  allowed: string[];
  default: string;
  restricted: boolean;
  candidates: ModuleModelCandidate[];
  unknown_allowed: string[];
}

/** 模块级选型配置响应 */
export interface ModuleModelConfigResponse {
  slots: ModuleModelSlotInfo[];
  config: Record<string, ModuleSlotConfig>;
  slot_keys: string[];
}

/** 读取功能模块级模型选型配置（模型管理页配置面板数据源） */
export async function getModuleModelConfig(): Promise<ModuleModelConfigResponse> {
  return get<ModuleModelConfigResponse>('/models/module-config');
}

/** 保存模块级选型配置（configs: slot → {allowed, default}） */
export function saveModuleModelConfig(configs: Record<string, ModuleSlotConfig>) {
  return put<ModuleModelConfigResponse & { warnings?: string[] }>(
    '/models/module-config',
    { configs },
  );
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
  getVllmStatus,
  stopVllm,
  releaseForModule,
  getModuleModelConfig,
  saveModuleModelConfig,
};
