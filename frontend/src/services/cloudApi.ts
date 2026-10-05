/* ==========================================================================
 * OmniSpace AI —— 云端 API 服务商接入（批1：地基+文本，2026-09-06）
 * --------------------------------------------------------------------------
 * 严格对齐后端 src/api/cloud.py（用户自带 Key 直连服务商，本机存储；
 * 对外一律打码，编辑留空=保持原值）：
 * - GET    /cloud/providers                  连接列表（懒触发旧配置迁移）
 * - POST   /cloud/providers                  新增连接
 * - PUT    /cloud/providers/{id}             更新（api_key 留空=保持原值）
 * - DELETE /cloud/providers/{id}             删除（顺带清悬空绑定）
 * - POST   /cloud/providers/test             连通性测试
 * - GET    /cloud/providers/{id}/models      拉取模型列表
 * - GET    /cloud/bindings                   工位绑定表
 * - PUT    /cloud/bindings/{slot}            绑定/换绑
 * - DELETE /cloud/bindings/{slot}            解绑（回本地引擎）
 * - GET    /cloud/status                     文本引擎当前承载
 * ========================================================================== */

import { del, get, post, put } from './api';

/** 疑似非文本对话模型关键词（2026-09-21 实测案例：qwen-audio-asr 系列
 *  被绑成「文本对话」→ 服务商 400 UNSUPPORTED_FORMAT）。命中仅警示
 *  不阻断——注意 qwen3-vl 等视觉对话模型不在名单。真源在本文件，
 *  设置页保存警示与对话页模型下拉标注共用。 */
export const SUSPICIOUS_NON_TEXT_MODEL_RE =
  /audio|asr|tts|whisper|speech|paraformer|sensevoice|cosyvoice|embedding|rerank|image|video|flux|wan[\dx]|ocr/i;

/** 视觉对话模型判定（2026-09-22 云端图片透传）：与后端
 *  cloud_provider_service.VISION_MODEL_RE 同口径（跨语言无法同源，
 *  改动需双侧同步）。命中才显示图片上传按钮/真正透传图片。 */
export const VISION_MODEL_RE =
  /vl|qvq|omni|vision|glm-4v|minicpm|internvl|llava|pixtral/i;

/** 云端服务商连接（api_key 永不出后端，只有打码形态） */
export interface CloudProvider {
  id: string;
  name: string;
  protocol: string;
  base_url: string;
  api_key_masked?: string;
  models: string[];
  enabled: boolean;
  created_at?: number;
  updated_at?: number;
}

/** 工位绑定（slot → 连接+模型） */
export interface CloudBinding {
  provider_id: string;
  model: string;
  provider_name?: string;
  updated_at?: number;
}

/** 工位注册表（批1 仅 dialog.text 可绑定） */
export type CloudSlots = Record<string, string>;

export const SLOT_DIALOG_TEXT = 'dialog.text';

/** 连接列表响应 */
export interface CloudProvidersResp {
  providers: CloudProvider[];
  slots: CloudSlots;
  legacy_migrated: number;
}

/** 绑定表响应 */
export interface CloudBindingsResp {
  bindings: Record<string, CloudBinding>;
  slots: CloudSlots;
}

/** 文本引擎承载状态（对话页横幅用） */
export interface CloudStatus {
  mode: 'local' | 'cloud';
  provider_id: string;
  provider_name: string;
  model: string;
}

/** 新增/编辑连接的表单载荷（api_key 空串=保持原值） */
export interface CloudProviderInput {
  name: string;
  protocol: string;
  base_url: string;
  api_key?: string;
  models?: string[];
  enabled?: boolean;
}

export function listCloudProviders() {
  return get<CloudProvidersResp>('/cloud/providers');
}

/** 保存响应 data = 打码后的 provider 本体；removed_models = 按工位类别
 *  被后端严格过滤掉的模型清单（2026-09-21，仅保存时混入不匹配类别才有） */
export function createCloudProvider(body: CloudProviderInput) {
  return post<{ removed_models?: string[] }>(
    '/cloud/providers',
    body,
  );
}

export function updateCloudProvider(id: string, body: CloudProviderInput) {
  return put<{ removed_models?: string[] }>(
    `/cloud/providers/${encodeURIComponent(id)}`,
    body,
  );
}

export function deleteCloudProvider(id: string) {
  return del<{ deleted: string; bindings_cleared: boolean }>(
    `/cloud/providers/${encodeURIComponent(id)}`,
  );
}

export function testCloudProvider(body: {
  provider_id?: string;
  base_url?: string;
  api_key?: string;
}) {
  return post<{ reachable: boolean; detail: string; base_url: string }>(
    '/cloud/providers/test',
    body,
  );
}

export function fetchCloudProviderModels(id: string) {
  return get<{ models: string[]; removed: string[]; error: string }>(
    `/cloud/providers/${encodeURIComponent(id)}/models`,
  );
}

/** 余额查询响应（尽力而为：站点不支持时 supported=false，非错误） */
export interface CloudBilling {
  supported: boolean;
  total_usd?: number;
  used_usd?: number;
  remain_usd?: number;
  error?: string;
}

export function fetchCloudProviderBilling(id: string) {
  return get<CloudBilling>(
    `/cloud/providers/${encodeURIComponent(id)}/billing`,
  );
}

export function listCloudBindings() {
  return get<CloudBindingsResp>('/cloud/bindings');
}

export function setCloudBinding(slot: string, body: {
  provider_id: string;
  model: string;
}) {
  return put<CloudBinding>(
    `/cloud/bindings/${encodeURIComponent(slot)}`,
    body,
  );
}

export function clearCloudBinding(slot: string) {
  return del<{ cleared: string }>(
    `/cloud/bindings/${encodeURIComponent(slot)}`,
  );
}

export function getCloudStatus() {
  return get<CloudStatus>('/cloud/status');
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
