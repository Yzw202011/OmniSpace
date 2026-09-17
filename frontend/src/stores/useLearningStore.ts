/* ==========================================================================
 * OmniSpace AI v2.3 —— 知识学习 v2.3 状态（TASK-036 useLearningStore）
 * --------------------------------------------------------------------------
 * 职责：
 *   - 学习主题 CRUD（/learn/topic/*）
 *   - 学习会话控制与实时状态轮询（/learn/session/*，3s 轮询）
 *   - 学习设置（修改即保存 /learn/settings）
 *   - 内置浏览器状态（截图由 BrowserView 组件自行 2s 轮询）
 *   - 知识库统计/列表/删除、文档导入
 *   - 行为学习统计/清空
 * 说明：与既有 useLearnStore（训练任务）并存，不影响 LearnView 训练功能。
 * ========================================================================== */

import { create } from 'zustand';
import * as learningApi from '@/services/learningApi';
import type {
  LearnTopic,
  LearnSessionStatus,
  LearnLogEntry,
  LearnSettings,
  BrowserStatus,
  KnowledgeStats,
  KnowledgeItem,
  KnowledgeGraph,
  BehaviorStats,
} from '@/services/learningApi';
import { useAppStore } from './useAppStore';

/** 默认学习设置（后端未就绪时的本地回退） */
export const DEFAULT_LEARN_SETTINGS: LearnSettings = {
  online_enabled: true,
  max_duration_min: 30,
  max_pages: 50,
  search_engine: 'bing',
  domain_whitelist: [],
  domain_blacklist: [],
  ad_filter: true,
  bandwidth_limit_kbps: 500,
  show_browser: true,
  auto_finetune_days: 7,
  behavior_learning: true,
};

/** 将主题列表响应归一化为数组 */
function normalizeTopics(res: LearnTopic[] | { items: LearnTopic[] }): LearnTopic[] {
  return Array.isArray(res) ? res : res.items || [];
}

/** 学习 v2.3 状态 */
export interface LearningState {
  /* ------------------------------ 数据 ------------------------------ */
  /** 主题列表 */
  topics: LearnTopic[];
  /** 主题是否已加载 */
  topicsLoaded: boolean;
  /** 当前会话状态（null 表示尚未拉取） */
  session: LearnSessionStatus | null;
  /** 学习日志（点击查看时拉取） */
  logs: LearnLogEntry[];
  /** 知识库统计 */
  knowledgeStats: KnowledgeStats | null;
  /** 知识条目列表 */
  knowledgeItems: KnowledgeItem[];
  /** 知识条目总数 */
  knowledgeTotal: number;
  /** 知识库当前页 */
  knowledgePage: number;
  /** 知识库搜索关键字 */
  knowledgeQuery: string;
  /** 知识图谱数据（TASK-055，null 未拉取） */
  knowledgeGraph: KnowledgeGraph | null;
  /** 知识图谱加载中 */
  knowledgeGraphLoading: boolean;
  /** 行为学习统计 */
  behaviorStats: BehaviorStats | null;
  /** 学习设置 */
  settings: LearnSettings;
  /** 设置是否已加载 */
  settingsLoaded: boolean;
  /** 浏览器状态 */
  browserStatus: BrowserStatus | null;
  /** 浏览器查看区域是否展开 */
  isBrowserVisible: boolean;
  /** 文档导入中 */
  importing: boolean;

  /* ------------------------------ 主题 ------------------------------ */
  fetchTopics: () => Promise<void>;
  createTopic: (name: string) => Promise<boolean>;
  deleteTopic: (id: string) => Promise<void>;

  /* ------------------------------ 会话 ------------------------------ */
  startSession: (topicId?: string) => Promise<boolean>;
  pauseSession: () => Promise<void>;
  resumeSession: () => Promise<void>;
  stopSession: () => Promise<void>;
  fetchSessionStatus: () => Promise<void>;
  fetchLogs: () => Promise<void>;

  /* ------------------------------ 浏览器 ------------------------------ */
  fetchBrowserStatus: () => Promise<void>;
  toggleBrowser: () => void;
  takeover: () => Promise<void>;
  handback: () => Promise<void>;

  /* ------------------------------ 知识库 ------------------------------ */
  fetchKnowledgeStats: () => Promise<void>;
  fetchKnowledge: (page?: number, query?: string) => Promise<void>;
  deleteKnowledge: (id: string) => Promise<void>;
  batchDeleteKnowledge: (ids: string[]) => Promise<{ deleted: number; missing: string[] }>;
  importDocument: (file: File) => Promise<boolean>;
  /** 导入图片知识（VLM 描述入库；UAT 2026-09-11 图片知识支持） */
  importImage: (file: File) => Promise<boolean>;
  fetchKnowledgeGraph: (kid?: string) => Promise<void>;

  /* ------------------------------ 行为学习 ------------------------------ */
  fetchBehaviorStats: () => Promise<void>;
  clearBehavior: () => Promise<void>;

  /* ------------------------------ 设置 ------------------------------ */
  fetchSettings: () => Promise<void>;
  updateSettings: (patch: Partial<LearnSettings>) => Promise<void>;
}

export const useLearningStore = create<LearningState>((set, get) => ({
  topics: [],
  topicsLoaded: false,
  session: null,
  logs: [],
  knowledgeStats: null,
  knowledgeItems: [],
  knowledgeTotal: 0,
  knowledgePage: 1,
  knowledgeQuery: '',
  knowledgeGraph: null,
  knowledgeGraphLoading: false,
  behaviorStats: null,
  settings: DEFAULT_LEARN_SETTINGS,
  settingsLoaded: false,
  browserStatus: null,
  isBrowserVisible: false,
  importing: false,

  /* ------------------------------ 主题 ------------------------------ */
  fetchTopics: async () => {
    try {
      const res = await learningApi.listTopics();
      set({ topics: normalizeTopics(res), topicsLoaded: true });
    } catch {
      set({ topicsLoaded: true });
    }
  },

  createTopic: async (name) => {
    try {
      await learningApi.createTopic(name);
      useAppStore.getState().showToast(`主题「${name}」已创建`, 'success');
      await get().fetchTopics();
      return true;
    } catch (err) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '创建主题失败';
      useAppStore.getState().showToast(msg, 'error');
      return false;
    }
  },

  deleteTopic: async (id) => {
    try {
      await learningApi.deleteTopic(id);
      set((s) => ({ topics: s.topics.filter((t) => t.id !== id) }));
      useAppStore.getState().showToast('主题已删除', 'success');
    } catch {
      useAppStore.getState().showToast('删除主题失败', 'error');
    }
  },

  /* ------------------------------ 会话 ------------------------------ */
  startSession: async (topicId) => {
    try {
      await learningApi.startSession(topicId);
      useAppStore.getState().showToast(
        topicId ? '学习会话已开始' : 'AI 已开始自主学习', 'success');
      await get().fetchSessionStatus();
      return true;
    } catch (err) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '启动学习会话失败';
      useAppStore.getState().showToast(msg, 'error');
      return false;
    }
  },

  pauseSession: async () => {
    try {
      await learningApi.pauseSession();
      await get().fetchSessionStatus();
    } catch {
      useAppStore.getState().showToast('暂停失败，请检查后端服务', 'error');
    }
  },

  resumeSession: async () => {
    try {
      await learningApi.resumeSession();
      await get().fetchSessionStatus();
    } catch {
      useAppStore.getState().showToast('恢复失败，请检查后端服务', 'error');
    }
  },

  stopSession: async () => {
    try {
      await learningApi.stopSession();
      useAppStore.getState().showToast('学习会话已停止', 'info');
      await get().fetchSessionStatus();
    } catch {
      useAppStore.getState().showToast('停止失败，请检查后端服务', 'error');
    }
  },

  fetchSessionStatus: async () => {
    try {
      const status = await learningApi.getSessionStatus();
      set({ session: status });
    } catch {
      /* silent-intent: 后端未就绪时保持旧状态，轮询静默 */
    }
  },

  fetchLogs: async () => {
    try {
      // 无活跃会话时携带最近一次 session_id，避免后端 40008 报错
      const sid = get().session?.session_id;
      const res = await learningApi.getSessionLogs(
        sid ? { session_id: sid, limit: 200 } : { limit: 200 },
      );
      const raw = Array.isArray(res) ? res : res.items || [];
      // 归一化后端原始字段 {ts, action, reason, result} → {time, level, message}
      const logs = raw.map((e) => ({
        ...e,
        time: e.time ?? e.ts,
        level:
          e.level ??
          (e.action === 'step_error' || e.action === 'browser_lost' ? 'error' : 'info'),
        message:
          e.message ??
          [e.action, e.reason, e.result].filter(Boolean).join(' — '),
      }));
      set({ logs });
    } catch {
      useAppStore.getState().showToast('学习日志获取失败', 'warning');
    }
  },

  /* ------------------------------ 浏览器 ------------------------------ */
  fetchBrowserStatus: async () => {
    try {
      const status = await learningApi.getBrowserStatus();
      set({ browserStatus: status });
    } catch {
      /* silent-intent: 静默 */
    }
  },

  toggleBrowser: () => {
    set((s) => ({ isBrowserVisible: !s.isBrowserVisible }));
  },

  takeover: async () => {
    try {
      await learningApi.browserTakeover();
      await get().fetchBrowserStatus();
      useAppStore.getState().showToast('已接管浏览器，AI 已暂停操作', 'info');
    } catch {
      useAppStore.getState().showToast('接管失败，请检查后端服务', 'error');
    }
  },

  handback: async () => {
    try {
      await learningApi.browserHandback();
      await get().fetchBrowserStatus();
      useAppStore.getState().showToast('已交还控制权给 AI', 'success');
    } catch {
      useAppStore.getState().showToast('交还失败，请检查后端服务', 'error');
    }
  },

  /* ------------------------------ 知识库 ------------------------------ */
  fetchKnowledgeStats: async () => {
    try {
      const stats = await learningApi.getKnowledgeStats();
      set({ knowledgeStats: stats });
    } catch {
      /* silent-intent: 静默 */
    }
  },

  fetchKnowledge: async (page, query) => {
    const p = page ?? get().knowledgePage;
    const q = query ?? get().knowledgeQuery;
    try {
      const res = await learningApi.listKnowledge({ page: p, page_size: 10, q });
      if (Array.isArray(res)) {
        set({ knowledgeItems: res, knowledgeTotal: res.length, knowledgePage: p, knowledgeQuery: q });
      } else {
        set({
          knowledgeItems: res.items || [],
          knowledgeTotal: res.total ?? 0,
          knowledgePage: p,
          knowledgeQuery: q,
        });
      }
    } catch {
      /* silent-intent: 静默，保留旧列表 */
    }
  },

  deleteKnowledge: async (id) => {
    try {
      await learningApi.deleteKnowledge(id);
      useAppStore.getState().showToast('知识条目已删除', 'success');
      await Promise.all([get().fetchKnowledge(), get().fetchKnowledgeStats()]);
    } catch {
      useAppStore.getState().showToast('删除失败', 'error');
    }
  },

  batchDeleteKnowledge: async (ids) => {
    try {
      const res = await learningApi.batchDeleteKnowledge(ids);
      useAppStore.getState().showToast(
        `已删除 ${res.deleted} 条知识${res.missing.length ? `（${res.missing.length} 条不存在）` : ''}`,
        'success');
      await Promise.all([get().fetchKnowledge(), get().fetchKnowledgeStats()]);
      return res;
    } catch {
      useAppStore.getState().showToast('批量删除失败', 'error');
      return { deleted: 0, missing: ids };
    }
  },

  fetchKnowledgeGraph: async (kid) => {
    set({ knowledgeGraphLoading: true });
    try {
      const graph = await learningApi.getKnowledgeGraph(kid);
      set({ knowledgeGraph: graph, knowledgeGraphLoading: false });
    } catch {
      set({ knowledgeGraphLoading: false });
    }
  },

  importDocument: async (file) => {
    set({ importing: true });
    try {
      const res = await learningApi.importDocument(file);
      useAppStore.getState().showToast(
        `文档导入成功，提取 ${res.imported ?? 0} 条知识`, 'success');
      await Promise.all([get().fetchKnowledgeStats(), get().fetchKnowledge(1)]);
      return true;
    } catch (err) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '文档导入失败';
      useAppStore.getState().showToast(msg, 'error');
      return false;
    } finally {
      set({ importing: false });
    }
  },

  importImage: async (file) => {
    set({ importing: true });
    try {
      await learningApi.importKnowledgeImage(file, '图片知识');
      useAppStore.getState().showToast('图片知识已入库（含视觉描述）', 'success');
      await Promise.all([get().fetchKnowledgeStats(), get().fetchKnowledge(1)]);
      return true;
    } catch (err) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '图片导入失败';
      useAppStore.getState().showToast(msg, 'error');
      return false;
    } finally {
      set({ importing: false });
    }
  },

  /* ------------------------------ 行为学习 ------------------------------ */
  fetchBehaviorStats: async () => {
    try {
      const stats = await learningApi.getBehaviorStats();
      set({ behaviorStats: stats });
    } catch {
      /* silent-intent: 静默 */
    }
  },

  clearBehavior: async () => {
    try {
      await learningApi.clearBehavior();
      useAppStore.getState().showToast('行为数据已重置', 'success');
      await get().fetchBehaviorStats();
    } catch {
      useAppStore.getState().showToast('重置失败', 'error');
    }
  },

  /* ------------------------------ 设置 ------------------------------ */
  fetchSettings: async () => {
    try {
      const data = await learningApi.getLearnSettings();
      set({ settings: { ...DEFAULT_LEARN_SETTINGS, ...data }, settingsLoaded: true });
    } catch {
      set({ settingsLoaded: true });
    }
  },

  updateSettings: async (patch) => {
    const next = { ...get().settings, ...patch };
    set({ settings: next });
    try {
      await learningApi.saveLearnSettings(patch);
      useAppStore.getState().showToast('学习设置已保存', 'success');
    } catch {
      useAppStore.getState().showToast('设置保存失败，请检查后端服务', 'warning');
    }
  },
}));

export default useLearningStore;
// 本项目仅供学习使用，商业授权请+Q 3559331368
