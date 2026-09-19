// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * licenseApi.ts —— 激活门禁 API（2026-09-19 激活加固批 A5/A3）
 * --------------------------------------------------------------------------
 * 关于页授权卡数据源 + 解绑入口。端点在门禁白名单内（未激活也可访问）。
 * 响应经 Zod 校验（FE-033：新接 API 入口一律 .parse()）。
 * ========================================================================== */

import { z } from 'zod';
import { get, post } from './api';
import { parseWith } from './schema';

/** 授权明细（对齐后端 license_gate._verify_code；v1 旧码无到期/代数字段） */
const LicenseInfoSchema = z
  .object({
    serial: z.string(),
    type: z.string(),
    issued: z.string(),
    fp_match: z.number().optional(),
    generation: z.number().optional(),
    expires: z.string().optional(),
    permanent: z.boolean().optional(),
    days_left: z.number().nullable().optional(),
  })
  .partial();

/** GET /license/status 载荷（对齐后端 license_gate.status()） */
const LicenseStatusSchema = z
  .object({
    gate_enabled: z.boolean(),
    activated: z.boolean(),
    reason: z.string().default(''),
    fingerprints: z.array(z.string()).default([]),
    license: LicenseInfoSchema.nullable().optional(),
    license_file: z.string().optional(),
  })
  .passthrough();

export type LicenseStatus = z.infer<typeof LicenseStatusSchema>;

/** 拉取激活门禁状态（开发构建 gate_enabled=false=门禁旁路） */
export async function fetchLicenseStatus(): Promise<LicenseStatus> {
  return parseWith(LicenseStatusSchema, await get('/license/status'), '激活状态');
}

/** 解绑本机（A3）：返回一次性解绑码（发给卖家换绑；本机授权即作废） */
export async function unbindLicense(): Promise<string> {
  const data = await post<{ unbind_code: string }>('/license/unbind', {
    confirm: true,
  });
  return data.unbind_code;
}
