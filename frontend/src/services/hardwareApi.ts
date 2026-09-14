/* ==========================================================================
 * OmniSpace AI v2.1 —— 硬件 API（规格 §4.6 硬件API）
 * --------------------------------------------------------------------------
 * - GET /hardware/info       硬件画像（静态：GPU/CPU/RAM/Disk/Power）
 * - GET /hardware/realtime   实时遥测（GPU利用率/显存/温度/CPU/内存，1秒刷新）
 * - GET /hardware/synergy    协同调度状态（功能互斥锁 + VRAM 协同状态）
 * - WebSocket ws://127.0.0.1:5800/api/v1/hardware/realtime  实时推送
 * ========================================================================== */

import { get } from './api';
import { HARDWARE_REALTIME_URL } from './ws';
import { parseWith, HardwareInfoRespSchema, RealtimePayloadSchema } from './schema';
import { reportBgError } from '@/utils/errors';
import type {
  HardwareProfile,
  HardwareRealtime,
  SynergyState,
} from '@/types';

/** 获取硬件画像（静态信息；Zod 校验核心字段，防止缺失导致首页崩溃） */
export async function getHardwareInfo(): Promise<HardwareProfile> {
  const res = parseWith(
    HardwareInfoRespSchema,
    await get<unknown>('/hardware/info'),
    '硬件信息',
  );
  // B7 已知债：HardwareProfile 全集（GpuInfo 7 字段/CpuInfo/RamInfo/
  // DiskInfo/PowerInfo/tier/precision_spec）手译 Zod 成本高于收益——
  // schema 双轨结构性限制，登记待「接口类型从 schema infer 派生」工程项
  // 治本；此处双跳断言为 parseWith 核心字段校验后的受控收口。
  return res as unknown as HardwareProfile;
}

/** 后端遥测原始结构（嵌套 gpu/cpu/ram；P1-05：探测失败字段为 null + available=false） */
interface RealtimeRaw {
  gpu?: {
    available?: boolean;
    usage_percent?: number | null;
    vram_used_mb?: number | null;
    vram_total_mb?: number | null;
    temp_celsius?: number | null;
  };
  cpu?: { available?: boolean; usage_percent?: number | null };
  ram?: {
    available?: boolean;
    total_gb?: number | null;
    available_gb?: number | null;
    usage_percent?: number | null;
  };
  timestamp?: number;
}

/** 归一化后端嵌套遥测 → 前端扁平 HardwareRealtime。
 *  P1-05：探测失败的 null 原样保留（UI 显示 --），不再强转 0 假读数。 */
export function normalizeRealtime(raw: RealtimeRaw | null | undefined): HardwareRealtime {
  const r = raw || {};
  return {
    cpu_percent: r.cpu?.usage_percent ?? null,
    ram_percent: r.ram?.usage_percent ?? null,
    ram_total_mb: r.ram?.total_gb != null ? Math.round(r.ram.total_gb * 1024) : undefined,
    // 后端遥测仅推送 total_gb/available_gb，已用内存由差值换算（无数据保持 undefined → UI 显示 --）
    ram_used_mb:
      r.ram?.total_gb != null && r.ram?.available_gb != null
        ? Math.round((r.ram.total_gb - r.ram.available_gb) * 1024)
        : undefined,
    gpu_util_pct: r.gpu?.usage_percent ?? null,
    vram_used_mb: r.gpu?.vram_used_mb ?? undefined,
    vram_total_mb: r.gpu?.vram_total_mb ?? undefined,
    vram_percent:
      r.gpu?.vram_total_mb && r.gpu.vram_total_mb > 0 && r.gpu.vram_used_mb != null
        ? (r.gpu.vram_used_mb / r.gpu.vram_total_mb) * 100
        : undefined,
    gpu_temp_celsius: r.gpu?.temp_celsius ?? null,
    ts: r.timestamp,
  };
}

/** 获取实时遥测（1 秒刷新；批 3-3b：响应过 Zod，非法抛错由调用方兜底保持末值） */
export async function getRealtime(): Promise<HardwareRealtime> {
  const raw = await get<RealtimeRaw>('/hardware/realtime');
  const parsed = RealtimePayloadSchema.safeParse(raw);
  if (!parsed.success) {
    reportBgError('hardware.realtime', parsed.error.issues[0] ?? new Error('遥测响应格式非法'));
    throw new Error('硬件遥测响应格式非法（FRONTEND_PARSE_ERROR）');
  }
  return normalizeRealtime(parsed.data as RealtimeRaw);
}

/** 获取协同调度状态（功能互斥锁 + VRAM 协同状态） */
export function getSynergy() {
  return get<SynergyState>('/hardware/synergy');
}

/** P3-⑤ 资源占用采样样本（后台采样器近 2 小时 RAM/显存/磁盘 30s 快照） */
export interface ResourceSample {
  timestamp: number;
  ram_pct: number | null;
  vram_pct: number | null;
  disk_pct: number | null;
  disk_free_gb: number | null;
}

/** P3-⑤ 资源占用采样趋势响应（/hardware/resource-samples） */
export interface ResourceSamplesResp {
  interval_s: number;
  backlog_s: number;
  count: number;
  samples: ResourceSample[];
  note?: string;
}

/** 获取资源占用采样趋势（limit 默认 240≈2 小时） */
export function getResourceSamples(limit = 240) {
  return get<ResourceSamplesResp>('/hardware/resource-samples', { limit });
}

/** 硬件实时 WebSocket 端点（供 store 直接订阅） */
export const HARDWARE_REALTIME_WS = HARDWARE_REALTIME_URL;

export default {
  getHardwareInfo,
  getRealtime,
  getSynergy,
  HARDWARE_REALTIME_WS,
};
// 本项目仅供学习使用，商业授权请+Q 3559331368
