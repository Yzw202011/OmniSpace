/**
 * OmniSpace AI v2.3.1 —— 插件系统 API（对齐后端 src/api/plugins.py）
 * --------------------------------------------------------------------------
 * - GET    /plugins                       插件清单（含信任档/来源/状态）
 * - POST   /plugins/import                导入用户插件（multipart，2026-09-16）
 * - POST   /plugins/{name}/load|unload    加载/卸载（invoke 会自动加载）
 * - POST   /plugins/{name}/enable|disable 启用/停用（停用即卸载）
 * - DELETE /plugins/{name}                删除用户插件（出厂插件拒删）
 * - GET    /plugins/kernel                内核状态
 * 导入规范见 docs/插件开发规范.md；含源码档需 confirmSource 显式确认。
 */
import { z } from 'zod';
import { get, post, del, upload } from './api';
import { parseWith } from './schema';

/** 插件信任档（对齐后端 _TRUST_LABELS 四档） */
export const PluginInfoSchema = z
  .object({
    name: z.string(),
    trust: z.string(),
    trust_label: z.string(),
    origin: z.enum(['factory', 'user']),
    imported_at: z.string().optional().default(''),
    enabled: z.boolean(),
    state: z.string(),
    source: z.string(),
    capability: z.string().optional().default(''),
    last_error: z.string().optional().nullable().default(''),
  })
  .passthrough();
export type PluginInfo = z.infer<typeof PluginInfoSchema>;

/** 导入结果 */
export const ImportResultSchema = z
  .object({
    name: z.string(),
    trust: z.string(),
    trust_label: z.string(),
    base_model: z.string().optional().default(''),
    route: z.string().optional().default(''),
    capability: z.string().optional().default(''),
    origin: z.literal('user'),
    imported_at: z.string(),
  })
  .passthrough();
export type ImportResult = z.infer<typeof ImportResultSchema>;

/** 插件清单（轻量无副作用） */
export async function listPlugins(): Promise<PluginInfo[]> {
  const res = await get<unknown>('/plugins');
  const raw = (res as { plugins?: unknown } | null)?.plugins ?? [];
  return parseWith(z.array(PluginInfoSchema), raw, '插件清单');
}

/**
 * 导入用户插件（单文件：源码内嵌在 .CuteMamen 包里，v2.1 规范）。
 * @param pkgFile        .CuteMamen 插件包（新类型插件的源码由开发者打进包内）
 * @param confirmSource  含源码包的显式确认（用户勾选后前端才置 true；
 *                       后端对带源码包未确认会回 PLUGIN_SOURCE_CONFIRM_REQUIRED）
 */
export async function importPlugin(
  pkgFile: File,
  confirmSource: boolean,
): Promise<ImportResult> {
  const fd = new FormData();
  fd.append('package', pkgFile);
  if (confirmSource) fd.append('confirm_source', 'true');
  const res = await upload<unknown>('/plugins/import', fd);
  return parseWith(ImportResultSchema, res, '插件导入');
}

/** 启用插件（停用件恢复可用） */
export async function enablePlugin(name: string): Promise<void> {
  await post<unknown>(`/plugins/${encodeURIComponent(name)}/enable`);
}

/** 停用插件（即卸载释放内存；重启后保持停用） */
export async function disablePlugin(name: string): Promise<void> {
  await post<unknown>(`/plugins/${encodeURIComponent(name)}/disable`);
}

/** 删除用户插件（出厂插件后端拒删） */
export async function deletePlugin(name: string): Promise<void> {
  await del<unknown>(`/plugins/${encodeURIComponent(name)}`);
}
