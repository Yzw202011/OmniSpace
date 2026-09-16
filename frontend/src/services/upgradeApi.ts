/* ==========================================================================
 * upgradeApi —— 应用内升级端点封装（升级机制批2，2026-09-11）
 * --------------------------------------------------------------------------
 * 严格对齐 src/api/upgrade.py 六端点；Zod 宁松勿严（manifest 内部
 * 字段不逐个校验，关键字段强校验）。
 * ========================================================================== */
import { z } from 'zod';

import { del, get, post, upload } from './api';
import { parseWith } from './schema';

/** 当前副本身份（对齐 upgrade_service.current_info） */
export interface UpgradeCurrentInfo {
  version: string;
  build: string;
  edition: string;
  updates_dir: string;
}

const PackageManifestSchema = z.object({
  to_version: z.string(),
  to_build_id: z.string(),
  from_min: z.string().optional(),
  from_max: z.string().optional(),
  created: z.string().optional(),
  contains_migration: z.boolean().optional(),
  payload_bytes: z.number().optional(),
  files_replace: z.number().optional(),
  files_delete: z.number().optional(),
  package_bytes: z.number().optional(),
});

const PackageEntrySchema = z.object({
  file: z.string(),
  path: z.string().optional(),
  valid: z.boolean(),
  reason: z.string(),
  signature_ok: z.boolean(),
  compatible: z.boolean(),
  compat_reason: z.string().optional(),
  manifest: PackageManifestSchema.nullable().optional(),
});
export type UpgradePackageEntry = z.infer<typeof PackageEntrySchema>;

const PackagesRespSchema = z.object({
  packages: z.array(PackageEntrySchema),
  current: z.object({
    version: z.string(),
    build: z.string(),
    edition: z.string(),
    updates_dir: z.string().optional(),
  }),
});
export type UpgradePackagesResp = z.infer<typeof PackagesRespSchema>;

const StatusRespSchema = z.object({
  current: z.object({
    version: z.string(),
    build: z.string(),
    edition: z.string(),
    updates_dir: z.string().optional(),
  }),
  state: z.unknown().nullable().optional(),
  updater_present: z.boolean().optional(),
});
export type UpgradeStatusResp = z.infer<typeof StatusRespSchema>;

const ImportRespSchema = z.object({
  file: z.string(),
  bytes: z.number(),
  signature_ok: z.boolean(),
  compatible: z.boolean(),
  reason: z.string(),
  manifest: PackageManifestSchema.nullable().optional(),
});
export type UpgradeImportResp = z.infer<typeof ImportRespSchema>;

/** 升级总览 */
export function getUpgradeStatus() {
  return get<unknown>('/upgrade/status').then(
    (d) => parseWith(StatusRespSchema, d, '升级状态') as UpgradeStatusResp);
}

/** 扫描 updates/ 升级包列表 */
export function getUpgradePackages() {
  return get<unknown>('/upgrade/packages').then(
    (d) => parseWith(PackagesRespSchema, d, '升级包列表') as UpgradePackagesResp);
}

/** 导入升级包（multipart，后端流式落盘限 2GB） */
export function importUpgradePackage(file: File) {
  const fd = new FormData();
  fd.append('file', file);
  return upload<unknown>('/upgrade/import', fd).then(
    (d) => parseWith(ImportRespSchema, d, '导入升级包') as UpgradeImportResp);
}

/** 开始升级（confirm=UPGRADE 由确认弹窗回传） */
export function startUpgrade(packageName: string) {
  return post<unknown>('/upgrade/start',
    { package: packageName, confirm: 'UPGRADE' });
}

/** 删除 updates/ 下指定升级包 */
export function deleteUpgradePackage(packageName: string) {
  return del<unknown>(`/upgrade/packages/${encodeURIComponent(packageName)}`);
}

/** 打开 updates/ 文件夹 */
export function openUpgradeFolder() {
  return post<unknown>('/upgrade/open-folder', {});
}
