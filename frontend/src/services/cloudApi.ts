/* ==========================================================================
 * OmniSpace AI —— 云端 API 服务商接入（批1：地基+文本，2026-09-06）
 * --------------------------------------------------------------------------
 * 严格对齐后端 backend/api/cloud.py（用户自带 Key 直连服务商，本机存储；
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

export function createCloudProvider(body: CloudProviderInput) {
  return post<{ provider: CloudProvider | null; message: string }>(
    '/cloud/providers',
    body,
  );
}

export function updateCloudProvider(id: string, body: CloudProviderInput) {
  return put<{ provider: CloudProvider | null; message: string }>(
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
  return get<{ models: string[]; error: string }>(
    `/cloud/providers/${encodeURIComponent(id)}/models`,
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
