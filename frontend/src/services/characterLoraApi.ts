// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * 人物 LoRA API（P3 训练中心·人物页签，2026-09-17 用户拍板 3A）
 * --------------------------------------------------------------------------
 * 对接后端 src/api/character_lora.py：
 *   GET  /character/lora/assets   可训练角色资产（含图片数/当前版本）
 *   POST /character/lora/train    入队训练
 *   GET  /character/lora/tasks    任务列表
 *   POST /character/lora/tasks/{id}/cancel  取消
 *   GET  /character/lora/versions 版本列表
 *   POST /character/lora/rollback 回滚
 * ========================================================================== */

import { get, post } from './api';

export interface CharacterAssetCard {
  asset_id: string;
  name: string;
  prompt: string;
  image_count: number;
  trainable: boolean;
  current_version: string | null;
}

export interface CharacterTask {
  id: string;
  asset_id: string;
  name: string;
  status: 'queued' | 'training' | 'done' | 'error' | 'cancelled' | string;
  progress: number;
  error: string | null;
  version: string | null;
  created_at: number;
}

export interface CharacterVersion {
  version: string;
  created_at?: number;
  name?: string;
  data_count?: number;
  is_current: boolean;
}

export async function fetchCharacterAssets(): Promise<CharacterAssetCard[]> {
  const data = await get<{ items: CharacterAssetCard[] }>('/character/lora/assets');
  return data.items ?? [];
}

export async function trainCharacterLora(assetId: string): Promise<CharacterTask> {
  return post<CharacterTask>('/character/lora/train', { asset_id: assetId });
}

export async function fetchCharacterTasks(): Promise<CharacterTask[]> {
  const data = await get<{ items: CharacterTask[] }>('/character/lora/tasks');
  return data.items ?? [];
}

export async function cancelCharacterTask(taskId: string): Promise<unknown> {
  return post(`/character/lora/tasks/${encodeURIComponent(taskId)}/cancel`, {});
}

export async function fetchCharacterVersions(assetId: string): Promise<CharacterVersion[]> {
  const data = await get<{ items: CharacterVersion[] }>(
    `/character/lora/versions?asset_id=${encodeURIComponent(assetId)}`);
  return data.items ?? [];
}

export async function rollbackCharacterLora(
  assetId: string,
  version: string,
): Promise<{ version: string; deployed: string }> {
  return post('/character/lora/rollback', { asset_id: assetId, version });
}
