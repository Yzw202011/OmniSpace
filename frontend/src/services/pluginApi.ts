/**
 * OmniSpace AI v2.5.0 —— 插件系统 API（对齐后端 src/api/plugins.py）
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

/* --------------------- 插件技能（技能插座批1，2026-09-17） --------------------- */

/** 技能索引项（对齐后端 registry.skills_info 返回字段） */
export const PluginSkillSchema = z
  .object({
    plugin: z.string(),
    id: z.string(),
    feature: z.string(),
    title: z.string(),
    description: z.string().optional().default(''),
    input: z.string().optional().default('text'),
    trust: z.string(),
    trust_label: z.string(),
  })
  .passthrough();
export type PluginSkillInfo = z.infer<typeof PluginSkillSchema>;

/** 按功能页拉技能清单（chat/novel/comic/manga；轻量无加载副作用） */
export async function listSkills(feature: string): Promise<PluginSkillInfo[]> {
  const res = await get<unknown>('/plugins/skills', { feature });
  const raw = (res as { skills?: unknown } | null)?.skills ?? [];
  return parseWith(z.array(PluginSkillSchema), raw, '插件技能清单');
}

/** 对话技能执行结果（POST /dialog/skill） */
export const ChatSkillResultSchema = z
  .object({
    plugin: z.string(),
    skill_id: z.string(),
    title: z.string().optional().default(''),
    output: z.string(),
  })
  .passthrough();
export type ChatSkillResult = z.infer<typeof ChatSkillResultSchema>;

/** 调用对话插件技能（文本进文本出，显式触发；产出由前端作 plugin_context 注入） */
export async function invokeChatSkill(payload: {
  plugin: string;
  skill_id: string;
  text: string;
  timeout_s?: number;
}): Promise<ChatSkillResult> {
  const res = await post<unknown>('/chat/skill', payload);
  return parseWith(ChatSkillResultSchema, res, '插件技能执行');
}

/* ------------------- 漫画图像技能（技能插座批3） ------------------- */

/** 漫画技能执行结果（POST /comic/skill：图像进图像出/文本兜底） */
export const ComicSkillResultSchema = z
  .object({
    plugin: z.string(),
    skill_id: z.string(),
    title: z.string().optional().default(''),
    /** /manga/media 可回读的相对路径（前端经 getMediaUrl 拼 URL） */
    image_urls: z.array(z.string()),
    text: z.string().optional().default(''),
  })
  .passthrough();
export type ComicSkillResult = z.infer<typeof ComicSkillResultSchema>;

/** 跑漫画图像技能：image_paths 为 DATA_DIR 相对路径（comic_assets/keyframes） */
export async function invokeComicSkill(payload: {
  plugin: string;
  skill_id: string;
  image_paths: string[];
  text?: string;
}): Promise<ComicSkillResult> {
  const res = await post<unknown>('/comic/skill', payload);
  return parseWith(ComicSkillResultSchema, res, '漫画技能执行');
}
