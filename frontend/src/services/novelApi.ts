// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 小说写作台 API（批2 MVP，对齐 src/api/novel.py）
 * --------------------------------------------------------------------------
 * 已对接端点（全部 /api/v1 前缀，统一信封由 api.ts 拆解）：
 * - POST /novel/project/create        建项目（重名 NOVEL_PROJECT_NAME_DUPLICATED）
 * - GET  /novel/project/list          项目列表（含章数/字数统计）
 * - DELETE /novel/project/{pid}       删项目（级联清大纲/章/角色/伏笔）
 * - POST /novel/outline/generate      AI 生成大纲树（入队即返回）
 * - GET  /novel/outline/{pid}         大纲树平铺（book→volume→chapter_outline）
 * - PUT/DELETE /novel/outline/{oid}   大纲节点编辑/删除
 * - POST /novel/chapter/create        手工建章
 * - GET  /novel/chapters/{pid}        章节列表（轻行）
 * - GET/PUT/DELETE /novel/chapter/*   章节详情/保存/删除
 * - POST /novel/chapter/{cid}/generate        单章生成（入队）
 * - POST /novel/chapters/generate             批量入队（串行跑）
 * - GET  /novel/generate/progress             队列/进度轮询
 * - POST /novel/generate/cancel               取消任务
 * - GET/POST/PUT/DELETE /novel/characters*    角色卡 CRUD + /characters/generate
 * - GET/POST/PUT/DELETE /novel/foreshadows*   伏笔账本 CRUD（含 check 体检）
 * - POST /novel/worldbuilding/import          世界观入知识库（走质检闸）
 * - POST /novel/export                        整书导出 txt/md
 * 高频读端点走 schema.ts Zod 校验（parseWith），其余信封已由 api.ts 兜底。
 * ========================================================================== */

import { z } from 'zod';
import { get, post, put, del } from './api';
import {
  NovelChapterDetailSchema,
  NovelChapterListRespSchema,
  NovelProjectListRespSchema,
  parseWith,
} from './schema';

/* ------------------------------ 类型 ------------------------------ */

export interface NovelProject {
  id: string;
  name: string;
  genre: string;
  description: string;
  style_notes: string;
  chapter_total: number;
  chapter_done: number;
  total_words: number;
  [key: string]: unknown;
}

export interface NovelOutlineNode {
  id: string;
  parent_id: string;
  level: 'book' | 'volume' | 'chapter_outline';
  sort_index: number;
  title: string;
  content: string;
  status: string;
}

export interface NovelChapter {
  id: string;
  project_id: string;
  outline_id: string;
  chapter_index: number;
  title: string;
  summary: string;
  word_count: number;
  status: 'pending' | 'generating' | 'done' | 'error';
  progress: number;
  error: string;
  updated_at?: number;
}

export interface NovelChapterDetail extends NovelChapter {
  content: string;
}

export interface NovelCharacter {
  id: string;
  name: string;
  role: string;
  summary: string;
}

export interface NovelForeshadow {
  id: string;
  description: string;
  planted_chapter_id: string;
  payoff_chapter_id: string;
  status: 'planted' | 'payoff' | 'dropped';
  note: string;
}

export interface ForeshadowCheck {
  planted_total: number;
  payoff_total: number;
  dropped_total: number;
  unrecalled: { id: string; description: string; planted_chapter_index: number | null }[];
  dangling_payoff: { id: string; description: string }[];
}

export interface NovelTaskInfo {
  task_id: string;
  kind: string;
  project_id: string;
  chapter_id: string;
  position?: number;
  stage?: { stage: string; detail?: string };
}

export interface NovelProgress {
  active: boolean;
  current: NovelTaskInfo | null;
  queue: NovelTaskInfo[];
  chapter_states: { id: string; status: string; progress: number; error: string }[];
  /** 最近失败留痕（后端 worker 记录，前端据此亮错误横幅） */
  failures?: { task_id: string; detail: string }[];
}

/* ------------------------------ 接口封装 ------------------------------ */

export async function fetchProjects(): Promise<{ items: NovelProject[] }> {
  const data = await get<unknown>('/novel/project/list');
  return parseWith(NovelProjectListRespSchema, data, '小说项目列表');
}

export function createProject(body: {
  name: string;
  genre?: string;
  description?: string;
  style_notes?: string;
}) {
  return post<{ project_id: string }>('/novel/project/create', body);
}

export function deleteProject(pid: string) {
  return del(`/novel/project/${pid}`);
}

export function generateOutline(pid: string) {
  return post<{ task_id: string; position: number | null }>(
    '/novel/outline/generate',
    { project_id: pid },
  );
}

export function fetchOutlines(pid: string) {
  return get<{ project_id: string; items: NovelOutlineNode[] }>(
    `/novel/outline/${pid}`,
  );
}

export function updateOutline(oid: string, body: { title?: string; content?: string }) {
  return put(`/novel/outline/${oid}`, body);
}

export function deleteOutline(oid: string) {
  return del(`/novel/outline/${oid}`);
}

export function fetchChapters(pid: string) {
  return get<unknown>(`/novel/chapters/${pid}`).then((d) =>
    parseWith(NovelChapterListRespSchema, d, '章节列表'),
  );
}

export function fetchChapter(cid: string) {
  return get<unknown>(`/novel/chapter/${cid}`).then((d) =>
    parseWith(NovelChapterDetailSchema, d, '章节详情'),
  );
}

export function createChapter(body: {
  project_id: string;
  title?: string;
  outline?: string;
}) {
  return post<{ chapter_id: string; chapter_index: number }>(
    '/novel/chapter/create',
    body,
  );
}

export function updateChapter(cid: string, body: { title?: string; content?: string }) {
  return put(`/novel/chapter/${cid}`, body);
}

export function deleteChapter(cid: string) {
  return del(`/novel/chapter/${cid}`);
}

export function generateChapter(cid: string) {
  return post<{ task_id: string; position: number | null }>(
    `/novel/chapter/${cid}/generate`,
  );
}

export function generateChaptersBatch(pid: string, skipCompleted = false) {
  return post<{ queued: number; skipped_completed: number }>(
    '/novel/chapters/generate',
    { project_id: pid, skip_completed: skipCompleted },
  );
}

export function fetchProgress(pid: string) {
  return get<NovelProgress>(
    `/novel/generate/progress?project_id=${encodeURIComponent(pid)}`,
  );
}

export function cancelTask(taskId: string) {
  return post<{ result: string }>('/novel/generate/cancel', { task_id: taskId });
}

export function fetchCharacters(pid: string) {
  return get<{ items: NovelCharacter[] }>(`/novel/characters/${pid}`);
}

export function createCharacter(body: {
  project_id: string;
  name: string;
  role?: string;
  summary?: string;
}) {
  return post<{ character_id: string }>('/novel/characters/create', body);
}

export function deleteCharacter(id: string) {
  return del(`/novel/characters/${id}`);
}

export function generateCharacters(pid: string, basis?: string) {
  return post<{ task_id: string }>('/novel/characters/generate', {
    project_id: pid,
    basis: basis || '',
  });
}

export function fetchForeshadows(pid: string) {
  return get<{ items: NovelForeshadow[]; check: ForeshadowCheck }>(
    `/novel/foreshadows/${pid}`,
  );
}

export function createForeshadow(body: {
  project_id: string;
  description: string;
  planted_chapter_id?: string;
}) {
  return post<{ foreshadow_id: string }>('/novel/foreshadows/create', body);
}

export function updateForeshadow(
  id: string,
  body: { status?: NovelForeshadow['status']; payoff_chapter_id?: string; note?: string },
) {
  return put(`/novel/foreshadows/${id}`, body);
}

export function deleteForeshadow(id: string) {
  return del(`/novel/foreshadows/${id}`);
}

export function importWorldbuilding(pid: string, content: string) {
  return post<{ added: number }>('/novel/worldbuilding/import', {
    project_id: pid,
    content,
  });
}

export function exportBook(pid: string, fmt: 'txt' | 'md') {
  return post<{ filename: string; content: string; chapter_total: number }>(
    '/novel/export',
    { project_id: pid, fmt },
  );
}

/* ---------------- 插件技能（技能插座批2，2026-09-17） ---------------- */


/** 写作台技能执行结果（POST /novel/chapter/{id}/skill，建议不落库） */
export const NovelSkillResultSchema = z
  .object({
    chapter_id: z.string(),
    plugin: z.string(),
    skill_id: z.string(),
    title: z.string().optional().default(''),
    original_chars: z.number(),
    suggestion: z.string(),
    similarity: z.number(),
  })
  .passthrough();
export type NovelSkillResult = z.infer<typeof NovelSkillResultSchema>;

/** 跑写作台技能：全章正文进插件，产出建议文本（采纳与否由用户决定） */
export function invokeNovelSkill(
  chapterId: string,
  payload: { plugin: string; skill_id: string; text?: string },
): Promise<NovelSkillResult> {
  return post<unknown>(`/novel/chapter/${chapterId}/skill`, payload).then(
    (res) => parseWith(NovelSkillResultSchema, res, '写作台技能执行'),
  );
}
