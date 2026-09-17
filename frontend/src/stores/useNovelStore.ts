/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 小说写作台状态（批2 MVP，2026-09-05）
 * --------------------------------------------------------------------------
 * 职责：书架（项目列表）/ 工作区（大纲树 + 章节 + 角色卡 + 伏笔账本 +
 * 世界观）数据与动作；生成走后端串行队列（入队即返回 + 3s 轮询进度）。
 * 功能互斥：生成属 dialog 域，由后端 worker 统一编排，前端只需轮询。
 * ========================================================================== */

import { create } from 'zustand';
import * as api from '@/services/novelApi';
import type {
  ForeshadowCheck,
  NovelChapter,
  NovelChapterDetail,
  NovelCharacter,
  NovelForeshadow,
  NovelOutlineNode,
  NovelProgress,
  NovelProject,
} from '@/services/novelApi';

/** 轮询间隔（毫秒）：后端生成按章粒度推进，3s 足够 */
const POLL_MS = 3000;

/** 已报过错的失败任务（每个只弹一次横幅） */
const seenFailureIds = new Set<string>();

export interface NovelState {
  /* ------------------------------ 数据 ------------------------------ */
  projects: NovelProject[];
  projectsLoaded: boolean;
  /** 当前打开的项目（null=书架视图） */
  project: NovelProject | null;
  outlines: NovelOutlineNode[];
  chapters: NovelChapter[];
  /** 当前编辑章节（含正文） */
  chapter: NovelChapterDetail | null;
  characters: NovelCharacter[];
  foreshadows: NovelForeshadow[];
  foreshadowCheck: ForeshadowCheck | null;
  progress: NovelProgress | null;
  /** 全局请求 loading（建项目/生成入队等按钮态） */
  busy: boolean;
  /** 页内错误横幅（空=无错） */
  err: string;
  /** 顶部提示（如「大纲已生成」） */
  notice: string;

  /* ------------------------------ 动作 ------------------------------ */
  fetchProjects: () => Promise<void>;
  createProject: (body: {
    name: string;
    genre?: string;
    description?: string;
    style_notes?: string;
  }) => Promise<boolean>;
  removeProject: (pid: string) => Promise<void>;
  openProject: (pid: string) => Promise<void>;
  closeProject: () => void;
  setErr: (msg: string) => void;
  setNotice: (msg: string) => void;

  generateOutline: () => Promise<void>;
  saveOutline: (oid: string, body: { title?: string; content?: string }) => Promise<void>;
  removeOutline: (oid: string) => Promise<void>;

  addChapter: (title: string, outline: string) => Promise<void>;
  selectChapter: (cid: string) => Promise<void>;
  saveChapter: (body: { title?: string; content?: string }) => Promise<void>;
  generateChapter: (cid: string) => Promise<void>;
  generateAllChapters: (skipCompleted?: boolean) => Promise<void>;
  removeChapter: (cid: string) => Promise<void>;
  cancelTask: (taskId: string) => Promise<void>;
  pollProgress: () => Promise<void>;

  refreshCharacters: () => Promise<void>;
  generateCharacters: (basis: string) => Promise<void>;
  addCharacter: (name: string, role: string, summary: string) => Promise<void>;
  removeCharacter: (id: string) => Promise<void>;

  refreshForeshadows: () => Promise<void>;
  addForeshadow: (description: string, plantedChapterId: string) => Promise<void>;
  setForeshadowStatus: (
    id: string,
    status: NovelForeshadow['status'],
    payoffChapterId?: string,
  ) => Promise<void>;
  removeForeshadow: (id: string) => Promise<void>;

  importWorldbuild: (content: string) => Promise<number>;
}

export const useNovelStore = create<NovelState>((set, get) => ({
  projects: [],
  projectsLoaded: false,
  project: null,
  outlines: [],
  chapters: [],
  chapter: null,
  characters: [],
  foreshadows: [],
  foreshadowCheck: null,
  progress: null,
  busy: false,
  err: '',
  notice: '',

  setErr: (msg) => set({ err: msg }),
  setNotice: (msg) => set({ notice: msg }),

  fetchProjects: async () => {
    try {
      const { items } = await api.fetchProjects();
      set({ projects: items, projectsLoaded: true, err: '' });
    } catch (e) {
      set({ err: (e as Error).message, projectsLoaded: true });
    }
  },

  createProject: async (body) => {
    set({ busy: true, err: '' });
    try {
      await api.createProject(body);
      await get().fetchProjects();
      set({ notice: `《${body.name}》已创建` });
      return true;
    } catch (e) {
      set({ err: (e as Error).message });
      return false;
    } finally {
      set({ busy: false });
    }
  },

  removeProject: async (pid) => {
    try {
      await api.deleteProject(pid);
      if (get().project?.id === pid) set({ project: null });
      await get().fetchProjects();
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  openProject: async (pid) => {
    set({ busy: true, err: '' });
    try {
      const p = get().projects.find((x) => x.id === pid) ?? null;
      set({
        project: p,
        outlines: [],
        chapters: [],
        chapter: null,
        characters: [],
        foreshadows: [],
        foreshadowCheck: null,
        progress: null,
      });
      const [outlines, chapters, chars, fores] = await Promise.all([
        api.fetchOutlines(pid),
        api.fetchChapters(pid),
        api.fetchCharacters(pid),
        api.fetchForeshadows(pid),
      ]);
      set({
        outlines: outlines.items,
        chapters: chapters.items,
        characters: chars.items,
        foreshadows: fores.items,
        foreshadowCheck: fores.check,
      });
    } catch (e) {
      set({ err: (e as Error).message });
    } finally {
      set({ busy: false });
    }
  },

  closeProject: () =>
    set({
      project: null,
      outlines: [],
      chapters: [],
      chapter: null,
      characters: [],
      foreshadows: [],
      foreshadowCheck: null,
      progress: null,
      err: '',
      notice: '',
    }),

  generateOutline: async () => {
    const p = get().project;
    if (!p) return;
    set({ busy: true, err: '', notice: '大纲生成已排队（对话模型就绪后自动开跑）' });
    try {
      await api.generateOutline(p.id);
      get().pollProgress();
    } catch (e) {
      set({ err: (e as Error).message, notice: '' });
    } finally {
      set({ busy: false });
    }
  },

  saveOutline: async (oid, body) => {
    try {
      await api.updateOutline(oid, body);
      const { items } = await api.fetchOutlines(get().project!.id);
      set({ outlines: items, notice: '大纲已保存' });
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  removeOutline: async (oid) => {
    try {
      await api.deleteOutline(oid);
      const { items } = await api.fetchOutlines(get().project!.id);
      set({ outlines: items });
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  addChapter: async (title, outline) => {
    const p = get().project;
    if (!p) return;
    try {
      await api.createChapter({ project_id: p.id, title, outline });
      const { items } = await api.fetchChapters(p.id);
      set({ chapters: items, notice: '章节已添加' });
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  selectChapter: async (cid) => {
    try {
      const detail = await api.fetchChapter(cid);
      set({ chapter: detail, err: '' });
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  saveChapter: async (body) => {
    const cur = get().chapter;
    if (!cur) return;
    try {
      await api.updateChapter(cur.id, body);
      const detail = await api.fetchChapter(cur.id);
      set({ chapter: detail, notice: '已保存' });
      const { items } = await api.fetchChapters(cur.project_id);
      set({ chapters: items });
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  generateChapter: async (cid) => {
    try {
      await api.generateChapter(cid);
      set({ notice: '已加入生成队列（对话模型就绪后自动开跑）' });
      get().pollProgress();
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  generateAllChapters: async (skipCompleted = true) => {
    const p = get().project;
    if (!p) return;
    try {
      const { queued, skipped_completed } = await api.generateChaptersBatch(p.id, skipCompleted);
      const skipNote = skipped_completed > 0 ? `（跳过已完成 ${skipped_completed} 章）` : '';
      set({
        notice: queued > 0
          ? `已排队 ${queued} 章，将按章序串行生成${skipNote}`
          : `没有需要生成的章节${skipNote}`,
      });
      get().pollProgress();
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  removeChapter: async (cid) => {
    try {
      await api.deleteChapter(cid);
      const p = get().project!;
      const { items } = await api.fetchChapters(p.id);
      set({ chapters: items, chapter: get().chapter?.id === cid ? null : get().chapter });
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  cancelTask: async (taskId) => {
    try {
      await api.cancelTask(taskId);
      set({ notice: '已请求取消' });
      get().pollProgress();
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  pollProgress: async () => {
    const p = get().project;
    if (!p) return;
    try {
      const prog = await api.fetchProgress(p.id);
      const prev = get().progress;
      // 有变化才落 state：轮询每 3s 一次，无脑 set 会整树重渲染，
      // 把 Playwright/用户的点击活活挤丢（实弹 19:1x 点击超时根因）
      if (JSON.stringify(prev) !== JSON.stringify(prog)) {
        set({ progress: prog });
      }
      // 失败留痕 → 错误横幅（每个任务只报一次）
      for (const f of prog.failures ?? []) {
        if (!seenFailureIds.has(f.task_id)) {
          seenFailureIds.add(f.task_id);
          set({ err: f.detail || '生成任务失败', notice: '' });
        }
      }
      // 有活动任务：静默刷新章节状态（轻端点，不含正文）
      if (prog.active || prev?.active) {
        const { items } = await api.fetchChapters(p.id);
        set({ chapters: items });
        // 任务收尾拍（active→idle）：回刷项目卡（完成数/字数）——
        // UAT 2026-09-10 缺陷⑤：此前书架停留「正在执行」须手动刷新
        if (prev?.active && !prog.active) {
          void get().fetchProjects();
        }
        // 当前编辑章状态翻终态 → 重取详情（正文写回编辑器）
        const cur = get().chapter;
        if (cur) {
          const now = items.find((c) => c.id === cur.id);
          const before = (prev?.chapter_states ?? []).find((c) => c.id === cur.id);
          if (now && before && before.status === 'generating'
              && (now.status === 'done' || now.status === 'error')) {
            const detail = await api.fetchChapter(cur.id);
            set({ chapter: detail });
          }
        }
      }
      // 队列排空瞬间（active 翻 false）：全量刷新工作区数据
      // （大纲/角色任务落库在完成一刻，不主动刷就看不见）
      if (prev?.active && !prog.active) {
        const [outlines, chars, fores] = await Promise.all([
          api.fetchOutlines(p.id),
          api.fetchCharacters(p.id),
          api.fetchForeshadows(p.id),
        ]);
        set({
          outlines: outlines.items,
          characters: chars.items,
          foreshadows: fores.items,
          foreshadowCheck: fores.check,
          notice: '生成任务已完成',
        });
      }
    } catch {
      /* silent-intent: 轮询失败静默（下个周期再试） */
    }
  },

  refreshCharacters: async () => {
    const p = get().project;
    if (!p) return;
    try {
      const { items } = await api.fetchCharacters(p.id);
      set({ characters: items });
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  generateCharacters: async (basis) => {
    const p = get().project;
    if (!p) return;
    try {
      await api.generateCharacters(p.id, basis);
      set({ notice: '角色设计已排队' });
      get().pollProgress();
      // 角色任务几秒~几十秒完成：延迟刷一次
      window.setTimeout(() => void get().refreshCharacters(), POLL_MS * 4);
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  addCharacter: async (name, role, summary) => {
    const p = get().project;
    if (!p) return;
    try {
      await api.createCharacter({ project_id: p.id, name, role, summary });
      await get().refreshCharacters();
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  removeCharacter: async (id) => {
    try {
      await api.deleteCharacter(id);
      await get().refreshCharacters();
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  refreshForeshadows: async () => {
    const p = get().project;
    if (!p) return;
    try {
      const { items, check } = await api.fetchForeshadows(p.id);
      set({ foreshadows: items, foreshadowCheck: check });
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  addForeshadow: async (description, plantedChapterId) => {
    const p = get().project;
    if (!p) return;
    try {
      await api.createForeshadow({
        project_id: p.id,
        description,
        planted_chapter_id: plantedChapterId,
      });
      await get().refreshForeshadows();
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  setForeshadowStatus: async (id, status, payoffChapterId) => {
    try {
      await api.updateForeshadow(id, {
        status,
        payoff_chapter_id: payoffChapterId ?? '',
      });
      await get().refreshForeshadows();
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  removeForeshadow: async (id) => {
    try {
      await api.deleteForeshadow(id);
      await get().refreshForeshadows();
    } catch (e) {
      set({ err: (e as Error).message });
    }
  },

  importWorldbuild: async (content) => {
    const p = get().project;
    if (!p) return 0;
    const { added } = await api.importWorldbuilding(p.id, content);
    return added;
  },
}));
// 本项目仅供学习使用，商业授权请+Q 3559331368
