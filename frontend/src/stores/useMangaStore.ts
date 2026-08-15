/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 漫剧状态（对齐后端 backend/api/manga.py 真实端点）
 * --------------------------------------------------------------------------
 * 后端真实能力边界（manga.py 逐项核对，2026-08-12 重构全量接线）：
 *   - 项目：POST /comic/project/create、GET list、PUT rename、DELETE 级联删除
 *     （旧注释「后端无项目 CRUD」系陈旧结论，端点实际一直存在）
 *   - 分镜表：GET/PUT /manga/storyboard/{pid}（全量保存覆盖增/删/重排）、
 *     PUT 单行、POST auto-split、POST import、GET export、POST reorder、
 *     POST ai-describe（DIALOG_NOT_READY 诚实错误）、POST preview（降级占位图）、
 *     POST emotion-detect（LLM/规则词典分级）
 *   - 资产：GET /comic/asset/library、POST generate-turnaround（角色多视图）、
 *     POST batch-generate、PUT bind（写行 asset_id）、POST export-pack（zip）
 *   - 关键帧：POST generate/regenerate、POST rollback、DELETE、GET list
 *   - 视频：POST /manga/video/generate → 后台线程真实产出；
 *     进度仅经 GET /manga/video/{taskId}/status 轮询（无 WS 广播），
 *     完成后 GET result 取 download_url；POST cancel 取消在途任务；
 *     生成期间持有 video_gen 功能锁
 *   - 音色：GET /manga/voices（首启种子化预置音色）、POST bind、
 *     PUT emotion、POST preview（base64 音频，可能降级为静音占位）
 * 媒体回读：资产/关键帧/导出包 file_path 经 GET /manga/media/{relpath}
 *   （后端白名单 comic_assets/keyframes/generated/exports + resolve 防穿越）。
 * ========================================================================== */

import { create } from 'zustand';
import type {
  ComicAsset,
  ComicAssetKind,
  ComicProject,
  KeyframeItem,
  StoryboardGenerationStatus,
  StoryboardRow,
} from '@/types';
import * as mangaApi from '@/services/mangaApi';
import type { VoiceItem } from '@/services/schema';
import { useAppStore } from './useAppStore';
import { useTaskStore } from './useTaskStore';

/** 漫剧项目引用（对齐后端 /comic/project/* 端点，含创建/重命名/删除/列表） */
export interface MangaProjectRef {
  id: string;
  name: string;
  /** 作品类型（G1）：regular=普通漫剧(5步) | narrative=解说漫剧(6步) */
  work_mode?: 'regular' | 'narrative';
}

/** 视频生成任务条目（状态来自真实轮询，绝不伪造进度） */
export interface MangaVideoTask {
  /** 任务 ID（后端下发） */
  task_id: string;
  /** 关联分镜行 ID */
  row_id: string;
  /** 镜号（列表展示） */
  shot_number: number;
  /** 画面描述（列表展示） */
  description: string;
  /** 任务状态（对齐后端 video_tasks.status；取消链路终态为 cancelled） */
  status: 'pending' | 'generating' | 'done' | 'error' | 'cancelled';
  /** 进度 0~1（真实轮询） */
  progress: number;
  /** 失败信息 */
  error?: string;
  /** 降级标记（Ken Burns 降级管线，审计 BK-014 诚实展示） */
  degraded?: boolean;
  /** 降级原因（中文，后端下发） */
  degrade_reason?: string;
  /** 完成后的 MP4 下载地址 */
  download_url?: string;
}

/** 视频状态轮询间隔（后端无 WS 推送，轮询为唯一真实进度来源） */
const VIDEO_POLL_INTERVAL = 2000;
/** 页面隐藏时的降频轮询间隔（后台节流；恢复可见时立即补一次 tick） */
const VIDEO_POLL_INTERVAL_HIDDEN = 10000;

/** 轮询条目：定时器句柄 + tick 回调（可见性变化时按当前间隔重建定时器） */
interface VideoPollerEntry {
  handle: ReturnType<typeof setInterval>;
  tick: () => void;
}

/** 任务 ID → 轮询条目（模块级，避免随 set 重建） */
const videoPollers = new Map<string, VideoPollerEntry>();

/** 当前轮询间隔：页面隐藏降频 10s，可见常速 2s */
function currentVideoPollInterval(): number {
  return typeof document !== 'undefined' && document.hidden
    ? VIDEO_POLL_INTERVAL_HIDDEN
    : VIDEO_POLL_INTERVAL;
}

/** 停止指定任务轮询（与 startVideoPoll 对称清理定时器与条目） */
function stopVideoPoll(taskId: string): void {
  const entry = videoPollers.get(taskId);
  if (entry) {
    clearInterval(entry.handle);
    videoPollers.delete(taskId);
  }
}

// 模块级可见性单例监听（随应用生命周期存在，仅注册一次）：
// 隐藏 → 全体轮询器按 10s 降频重建；恢复可见 → 按 2s 重建并立即补一次 tick。
// 轮询条目的生命周期仍由 startVideoPoll/stopVideoPoll 对称管理，监听器本身无泄漏。
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    const interval = currentVideoPollInterval();
    const hidden = document.hidden;
    for (const entry of videoPollers.values()) {
      clearInterval(entry.handle);
      entry.handle = setInterval(entry.tick, interval);
      if (!hidden) entry.tick();
    }
  });
}

/** 漫剧状态 */
export interface MangaState {
  /* ------------------------------ 项目 ------------------------------ */
  /** 项目列表（GET /comic/project/list） */
  projects: ComicProject[];
  /** 项目列表是否已加载过 */
  projectsLoaded: boolean;
  /** 项目列表加载中 */
  projectsLoading: boolean;

  /* ------------------------------ 分镜表 ------------------------------ */
  /** 当前项目（null=项目库视图；打开项目后进入工作区） */
  currentProject: MangaProjectRef | null;
  /** 分镜行列表 */
  rows: StoryboardRow[];
  /** 分镜表加载中 */
  loading: boolean;
  /** AI 自动分镜中 */
  splitting: boolean;
  /** 当前选中分镜行 ID（行点击联动右侧属性面板，COMIC-020） */
  selectedRowId: string | null;

  /* ------------------------------ 资产 ------------------------------ */
  /** 当前项目资产列表（GET /comic/asset/library） */
  assets: ComicAsset[];
  /** 资产是否已加载过（按项目维度，切项目重置） */
  assetsLoaded: boolean;
  /** 资产加载中 */
  assetsLoading: boolean;
  /** 资产详情面板打开的资产 ID（右栏槽位：抽屉 > 资产详情 > 行检查器 > 资产面板） */
  selectedAssetId: string | null;
  /** 资产绑定目标（表格资产列「+」点击设置；存在时资产面板进入绑定模式） */
  bindingTarget: { rowId: string; kind: ComicAssetKind } | null;

  /* ------------------------------ 关键帧 ------------------------------ */
  /** 关键帧版本缓存（row_id → 版本列表，版本倒序） */
  keyframes: Record<string, KeyframeItem[]>;

  /* ------------------------------ 音色 ------------------------------ */
  /** 音色列表（真实 GET /manga/voices） */
  voices: VoiceItem[];
  /** 音色是否已加载过（避免重复拉取） */
  voicesLoaded: boolean;

  /* ------------------------------ 视频 ------------------------------ */
  /** 视频生成任务列表 */
  videoTasks: MangaVideoTask[];
  /** 是否有视频生成中 */
  videoGenerating: boolean;

  /* ------------------------------ 动作：项目 ------------------------------ */
  /** 拉取项目列表 */
  fetchProjects: () => Promise<void>;
  /** 新建项目（template=comic_drama 预置 5 行分镜），返回 project_id */
  createProject: (name: string, template?: string, workMode?: string) => Promise<string>;
  /** 重命名项目 */
  renameProject: (projectId: string, name: string) => Promise<void>;
  /** 删除项目（级联；删除当前项目时回项目库） */
  removeProject: (projectId: string) => Promise<void>;
  /** 打开项目进入工作区（设置当前项目并拉取分镜表/资产） */
  openProject: (project: ComicProject) => Promise<void>;
  /** 关闭项目回项目库 */
  closeProject: () => void;

  /* ------------------------------ 动作：分镜表 ------------------------------ */
  /** 设置当前项目 */
  setProject: (project: MangaProjectRef) => void;
  /** 设置选中分镜行（COMIC-020：null 取消选中） */
  setSelectedRow: (rowId: string | null) => void;
  /** 拉取分镜表（GET /manga/storyboard/{pid}） */
  fetchRows: (projectId?: string) => Promise<StoryboardRow[]>;
  /** 全量保存分镜表（PUT，覆盖增/删/重排） */
  saveRows: (rows: StoryboardRow[]) => Promise<StoryboardRow[]>;
  /** 更新单行（PUT /rows/{rowId}，仅内容字段） */
  updateRow: (rowId: string, patch: Partial<StoryboardRow>) => Promise<void>;
  /** AI 自动分镜（POST auto-split，按剧本文本拆行追加） */
  autoSplit: (script: string) => Promise<number>;
  /** 导入剧本（POST import，按行拆分追加） */
  importScript: (script: string) => Promise<number>;
  /** 拖拽重排（POST reorder 持久化 sort_index，成功后以服务端行序为准） */
  reorderRows: (rowIds: string[]) => Promise<void>;

  /* ------------------------------ 动作：资产 ------------------------------ */
  /** 拉取当前项目资产（可按类型过滤） */
  fetchAssets: (kind?: ComicAssetKind) => Promise<void>;
  /** 绑定/解绑资产到分镜行（toggle 语义：已绑定 → unbind，未绑定 → bind；以响应 asset_ids 更新行） */
  bindAssetToRow: (assetId: string, rowId: string) => Promise<void>;
  /** 设置资产详情面板打开的资产（null 关闭；与行检查器互斥由工作台协调） */
  setSelectedAsset: (assetId: string | null) => void;
  /** 设置资产绑定目标（表格「+」进入绑定模式；null 退出） */
  setBindingTarget: (target: { rowId: string; kind: ComicAssetKind } | null) => void;

  /* ------------------------------ 动作：关键帧 ------------------------------ */
  /** 拉取分镜行关键帧版本（写入缓存并返回） */
  fetchKeyframes: (rowId: string) => Promise<KeyframeItem[]>;
  /** 失效某行关键帧缓存（生成/回退/删除后调用） */
  invalidateKeyframes: (rowId: string) => void;

  /* ------------------------------ 动作：音色 ------------------------------ */
  /** 拉取音色列表 */
  fetchVoices: () => Promise<void>;
  /** 绑定角色与音色（后端自动解除该角色旧绑定） */
  bindVoice: (voiceId: string, characterId: string) => Promise<void>;
  /** 更新音色情感标签 */
  updateVoiceEmotion: (voiceId: string, emotionLabel: string) => Promise<void>;
  /** 试听音色（返回 base64 音频，可能携带降级标记） */
  previewVoice: (voiceId: string, text: string, emotion?: string) => Promise<{
    audio: string;
    format: string;
    degraded?: boolean;
    degrade_reason?: string;
  }>;

  /* ------------------------------ 动作：视频 ------------------------------ */
  /** 发起视频生成（功能互斥 video_gen + 轮询进度） */
  generateVideo: (row: StoryboardRow) => Promise<boolean>;
  /** 取消在途视频任务（POST cancel + 停止轮询） */
  cancelVideo: (taskId: string) => Promise<void>;
  /** 手动刷新任务状态 */
  fetchVideoStatus: (taskId: string) => Promise<void>;
  /** 移除已完结任务条目 */
  removeVideoTask: (taskId: string) => void;
}

export const useMangaStore = create<MangaState>((set, get) => {
  /** 当前项目 ID（未设置时回退默认项目，与 MangaPage DEFAULT_PROJECT_ID 一致） */
  const pid = (): string => get().currentProject?.id ?? 'default';

  /** 同步视频行状态到本地 rows，并经全量保存持久化（单行 PUT 不含该字段） */
  const syncRowGenerationStatus = (rowId: string, status: StoryboardGenerationStatus): void => {
    set((state) => ({
      rows: state.rows.map((r) =>
        r.id === rowId ? { ...r, generation_status: status } : r,
      ),
    }));
    void mangaApi.saveStoryboardRows(pid(), get().rows).catch(() => {
      /* 状态持久化失败不影响主流程，下次手动保存时收敛 */
    });
  };

  /** 无活跃视频任务时释放 video_gen 功能锁 */
  const releaseVideoLockIfIdle = (): void => {
    const busy = get().videoTasks.some(
      (t) => t.status === 'generating' || t.status === 'pending',
    );
    set({ videoGenerating: busy });
    const appStore = useAppStore.getState();
    if (!busy && appStore.activeFeature === 'video_gen') {
      appStore.releaseActiveFeature();
    }
  };

  /** 更新单个视频任务条目 */
  const patchVideoTask = (taskId: string, patch: Partial<MangaVideoTask>): void => {
    set((state) => ({
      videoTasks: state.videoTasks.map((t) =>
        t.task_id === taskId ? { ...t, ...patch } : t,
      ),
    }));
  };

  /** 启动任务状态轮询（可见 2s / 隐藏降频 10s，终态自动停止并收敛） */
  const startVideoPoll = (taskId: string, rowId: string): void => {
    let consecutiveFailCount = 0;
    const MAX_CONSECUTIVE_FAILS = 3;
    const tick = async (): Promise<void> => {
      try {
        const st = await mangaApi.getVideoStatus(taskId);
        const degraded =
          typeof (st as { degraded?: unknown }).degraded === 'boolean'
            ? ((st as { degraded?: boolean }).degraded as boolean)
            : undefined;
        const degradeReason =
          typeof (st as { degrade_reason?: unknown }).degrade_reason === 'string'
            ? ((st as { degrade_reason?: string }).degrade_reason as string)
            : undefined;
        patchVideoTask(taskId, {
          status: st.status,
          progress: st.progress,
          error: st.error,
          degraded,
          degrade_reason: degradeReason,
        });
        useTaskStore.getState().updateProgress(taskId, st.progress);
        consecutiveFailCount = 0;

        if (st.status === 'done') {
          stopVideoPoll(taskId);
          const downloadUrl = mangaApi.getVideoDownloadUrl(taskId);
          patchVideoTask(taskId, { progress: 1, download_url: downloadUrl });
          useTaskStore.getState().completeTask(taskId, downloadUrl);
          syncRowGenerationStatus(rowId, 'done');
          useAppStore.getState().showToast(
            degradeReason ? `视频已生成（降级管线）：${degradeReason}` : '视频生成完成，可下载',
            degradeReason ? 'warning' : 'success',
          );
          releaseVideoLockIfIdle();
        } else if (st.status === 'error') {
          stopVideoPoll(taskId);
          useTaskStore.getState().failTask(taskId, st.error || '视频生成失败');
          syncRowGenerationStatus(rowId, 'error');
          useAppStore.getState().showToast(st.error || '视频生成失败', 'error');
          releaseVideoLockIfIdle();
        } else if (st.status === 'cancelled') {
          // 取消链路终态：停止轮询并释放功能锁（否则轮询永不收敛）
          stopVideoPoll(taskId);
          releaseVideoLockIfIdle();
        }
      } catch (err) {
        consecutiveFailCount += 1;
        console.warn(`[useMangaStore] 视频状态轮询失败（第 ${consecutiveFailCount} 次）:`, err);
        if (consecutiveFailCount >= MAX_CONSECUTIVE_FAILS) {
          stopVideoPoll(taskId);
          useAppStore.getState().showToast('视频任务状态同步失败，已停止轮询', 'error');
          releaseVideoLockIfIdle();
        }
      }
    };
    const tickFn = (): void => void tick();
    // 按当前可见性取间隔注册；可见性变化由模块级监听统一重建（见文件头部）
    videoPollers.set(taskId, { handle: setInterval(tickFn, currentVideoPollInterval()), tick: tickFn });
    tickFn();
  };

  return {
    /* ------------------------------ 项目 ------------------------------ */
    projects: [],
    projectsLoaded: false,
    projectsLoading: false,

    currentProject: null,
    rows: [],
    loading: false,
    splitting: false,
    selectedRowId: null,

    /* ------------------------------ 资产 ------------------------------ */
    assets: [],
    assetsLoaded: false,
    assetsLoading: false,
    selectedAssetId: null,
    bindingTarget: null,

    /* ------------------------------ 关键帧 ------------------------------ */
    keyframes: {},

    voices: [],
    voicesLoaded: false,

    videoTasks: [],
    videoGenerating: false,

    /* ------------------------------ 项目 ------------------------------ */
    fetchProjects: async () => {
      set({ projectsLoading: true });
      try {
        const projects = await mangaApi.listProjects();
        set({ projects, projectsLoaded: true, projectsLoading: false });
      } catch (err) {
        set({ projectsLoading: false });
        throw err;
      }
    },

    createProject: async (name, template, workMode) => {
      const res = await mangaApi.createProject(name, template, workMode);
      // 重新拉取列表（创建端点返回不含完整时间戳字段，以服务端为准）
      const projects = await mangaApi.listProjects();
      set({ projects, projectsLoaded: true });
      return res.project_id;
    },

    renameProject: async (projectId, name) => {
      await mangaApi.renameProject(projectId, name);
      set((state) => ({
        projects: state.projects.map((p) =>
          p.project_id === projectId ? { ...p, name } : p,
        ),
        currentProject:
          state.currentProject?.id === projectId
            ? { ...state.currentProject, name }
            : state.currentProject,
      }));
    },

    removeProject: async (projectId) => {
      await mangaApi.deleteProject(projectId);
      set((state) => {
        const isCurrent = state.currentProject?.id === projectId;
        // 清理当前项目的轮询器与任务状态
        if (isCurrent) {
          for (const taskId of videoPollers.keys()) {
            stopVideoPoll(taskId);
          }
          videoPollers.clear();
        }
        return {
          projects: state.projects.filter((p) => p.project_id !== projectId),
          // 删除当前项目：回项目库并清空挂接态
          ...(isCurrent
            ? {
                currentProject: null,
                rows: [],
                selectedRowId: null,
                assets: [],
                assetsLoaded: false,
                keyframes: {},
                selectedAssetId: null,
                bindingTarget: null,
                videoTasks: [],
              }
            : {}),
        };
      });
    },

    openProject: async (project) => {
      set({
        currentProject: { id: project.project_id, name: project.name, work_mode: project.work_mode },
        rows: [],
        selectedRowId: null,
        assets: [],
        assetsLoaded: false,
        keyframes: {},
        selectedAssetId: null,
        bindingTarget: null,
        loading: true,
      });
      try {
        const rows = await mangaApi.getStoryboardRows(project.project_id);
        set({ rows, loading: false });
      } catch (err) {
        set({ loading: false });
        throw err;
      }
      // 资产并行拉取（失败不阻塞分镜工作区）
      void mangaApi
        .listAssets(project.project_id)
        .then((assets) => set({ assets, assetsLoaded: true }))
        .catch(() => set({ assetsLoaded: true }));
    },

    closeProject: () => {
      // 清理所有轮询器与任务状态
      for (const taskId of videoPollers.keys()) {
        stopVideoPoll(taskId);
      }
      videoPollers.clear();
      set({
        currentProject: null,
        rows: [],
        selectedRowId: null,
        assets: [],
        assetsLoaded: false,
        keyframes: {},
        selectedAssetId: null,
        bindingTarget: null,
        videoTasks: [],
      });
    },

    /* ------------------------------ 分镜表 ------------------------------ */
    setProject: (project) => set({ currentProject: project }),

    setSelectedRow: (rowId) => set({ selectedRowId: rowId }),

    fetchRows: async (projectId) => {
      const target = projectId ?? pid();
      set({ loading: true });
      try {
        const rows = await mangaApi.getStoryboardRows(target);
        set({ rows, loading: false });
        return rows;
      } catch (err) {
        set({ loading: false });
        throw err;
      }
    },

    saveRows: async (rows) => {
      const saved = await mangaApi.saveStoryboardRows(pid(), rows);
      set({ rows: saved });
      return saved;
    },

    updateRow: async (rowId, patch) => {
      const row = await mangaApi.updateStoryboardRow(pid(), rowId, patch);
      set((state) => ({
        rows: state.rows.map((r) => (r.id === rowId ? { ...r, ...row } : r)),
      }));
    },

    autoSplit: async (script) => {
      set({ splitting: true });
      try {
        const res = await mangaApi.autoSplitStoryboard(pid(), script);
        set((state) => ({ rows: [...state.rows, ...res.added] }));
        return res.added.length;
      } finally {
        set({ splitting: false });
      }
    },

    importScript: async (script) => {
      const res = await mangaApi.importScript(pid(), script);
      set((state) => ({ rows: [...state.rows, ...res.rows] }));
      return res.rows.length;
    },

    reorderRows: async (rowIds) => {
      // 乐观更新本地顺序（sort_index 连续重编，与服务端语义一致）
      const orderMap = new Map(rowIds.map((id, i) => [id, i + 1]));
      set((state) => ({
        rows: [...state.rows]
          .sort(
            (a, b) =>
              (orderMap.get(a.id) ?? Number.MAX_SAFE_INTEGER) -
              (orderMap.get(b.id) ?? Number.MAX_SAFE_INTEGER),
          )
          .map((r) => {
            const idx = orderMap.get(r.id);
            return idx !== undefined ? { ...r, sort_index: idx } : r;
          }),
      }));
      try {
        const rows = await mangaApi.reorderStoryboard(rowIds, pid());
        set({ rows });
      } catch (err) {
        // 失败回滚：重新拉取服务端行序
        void get()
          .fetchRows()
          .catch(() => undefined);
        throw err;
      }
    },

    /* ------------------------------ 资产 ------------------------------ */
    fetchAssets: async (kind) => {
      set({ assetsLoading: true });
      try {
        const assets = await mangaApi.listAssets(pid(), kind);
        set({ assets, assetsLoaded: true, assetsLoading: false });
      } catch (err) {
        set({ assetsLoading: false });
        throw err;
      }
    },

    bindAssetToRow: async (assetId, rowId) => {
      // toggle 语义：目标行 asset_ids 已含该资产 → unbind，否则 → bind（追加去重）
      const row = get().rows.find((r) => r.id === rowId);
      const bound = (row?.asset_ids ?? (row?.asset_id ? [row.asset_id] : [])).includes(assetId);
      const res = bound
        ? await mangaApi.unbindAsset(assetId, rowId)
        : await mangaApi.bindAsset(assetId, rowId);
      // 以响应 asset_ids 为唯一真实来源更新行（不合并局部推测）
      set((state) => ({
        rows: state.rows.map((r) =>
          r.id === rowId ? { ...r, asset_ids: res.asset_ids, asset_id: res.asset_id } : r,
        ),
      }));
    },

    setSelectedAsset: (assetId) => set({ selectedAssetId: assetId }),

    setBindingTarget: (target) => set({ bindingTarget: target }),

    /* ------------------------------ 关键帧 ------------------------------ */
    fetchKeyframes: async (rowId) => {
      const items = await mangaApi.listKeyframes(rowId);
      set((state) => ({
        keyframes: { ...state.keyframes, [rowId]: items },
      }));
      return items;
    },

    invalidateKeyframes: (rowId) =>
      set((state) => {
        const next = { ...state.keyframes };
        delete next[rowId];
        return { keyframes: next };
      }),

    /* ------------------------------ 音色 ------------------------------ */
    fetchVoices: async () => {
      try {
        const items = await mangaApi.listVoices();
        set({ voices: items, voicesLoaded: true });
      } catch {
        /* 后端未就绪：标记已加载避免反复请求，列表保持空态 */
        set({ voicesLoaded: true });
      }
    },

    bindVoice: async (voiceId, characterId) => {
      await mangaApi.bindVoice(voiceId, characterId);
      // 后端语义：解除该角色旧绑定后绑定新音色 → 本地同步收敛
      set((state) => ({
        voices: state.voices.map((v) => {
          if (v.id === voiceId) return { ...v, character_id: characterId };
          if (v.character_id === characterId) return { ...v, character_id: '' };
          return v;
        }),
      }));
    },

    updateVoiceEmotion: async (voiceId, emotionLabel) => {
      await mangaApi.updateVoiceEmotion(voiceId, emotionLabel);
      set((state) => ({
        voices: state.voices.map((v) =>
          v.id === voiceId ? { ...v, emotion: emotionLabel } : v,
        ),
      }));
    },

    previewVoice: async (voiceId, text, emotion) => {
      const res = await mangaApi.previewVoice(voiceId, text, emotion);
      const degraded =
        typeof (res as { degraded?: unknown }).degraded === 'boolean'
          ? ((res as { degraded?: boolean }).degraded as boolean)
          : undefined;
      const degradeReason =
        typeof (res as { degrade_reason?: unknown }).degrade_reason === 'string'
          ? ((res as { degrade_reason?: string }).degrade_reason as string)
          : undefined;
      return { audio: res.audio, format: res.format, degraded, degrade_reason: degradeReason };
    },

    /* ------------------------------ 视频 ------------------------------ */
    generateVideo: async (row) => {
      // 功能互斥前置检查（规格 §6.1：与绘画/训练等互斥）
      const appStore = useAppStore.getState();
      if (!appStore.setActiveFeature('video_gen')) {
        return false;
      }
      const description =
        row.description || row.original_dialogue || `分镜 ${row.shot_number}`;
      try {
        const res = await mangaApi.generateVideo({
          storyboard_row_id: row.id,
          description,
          screenshot_4in1: '',
        });
        const degraded =
          typeof (res as { degraded?: unknown }).degraded === 'boolean'
            ? ((res as { degraded?: boolean }).degraded as boolean)
            : undefined;
        const degradeReason =
          typeof (res as { degrade_reason?: unknown }).degrade_reason === 'string'
            ? ((res as { degrade_reason?: string }).degrade_reason as string)
            : undefined;
        const entry: MangaVideoTask = {
          task_id: res.task_id,
          row_id: row.id,
          shot_number: row.shot_number,
          description,
          status: 'generating',
          progress: 0,
          degraded,
          degrade_reason: degradeReason,
        };
        set((state) => ({ videoTasks: [...state.videoTasks, entry], videoGenerating: true }));
        // 注册全局任务（状态栏进度可见）
        useTaskStore.getState().upsertTask({
          id: res.task_id,
          type: 'video_gen',
          name: `视频生成 分镜${row.shot_number}`,
          status: 'running',
          progress: 0,
          pausable: false,
        });
        syncRowGenerationStatus(row.id, 'generating');
        startVideoPoll(res.task_id, row.id);
        return true;
      } catch (err) {
        const msg =
          err && typeof err === 'object' && 'message' in err
            ? (err as { message: string }).message
            : '视频生成失败';
        useAppStore.getState().showToast(msg, 'error');
        appStore.releaseActiveFeature();
        set({ videoGenerating: false });
        return false;
      }
    },

    fetchVideoStatus: async (taskId) => {
      try {
        const st = await mangaApi.getVideoStatus(taskId);
        patchVideoTask(taskId, {
          status: st.status,
          progress: st.progress,
          error: st.error,
        });
        releaseVideoLockIfIdle();
      } catch {
        /* 忽略：保留上次快照 */
      }
    },

    removeVideoTask: (taskId) => {
      stopVideoPoll(taskId);
      set((state) => ({
        videoTasks: state.videoTasks.filter((t) => t.task_id !== taskId),
      }));
      releaseVideoLockIfIdle();
    },

    cancelVideo: async (taskId) => {
      await mangaApi.cancelVideo(taskId);
      stopVideoPoll(taskId);
      patchVideoTask(taskId, { status: 'error', error: '已手动取消' });
      const task = get().videoTasks.find((t) => t.task_id === taskId);
      if (task) {
        useTaskStore.getState().failTask(taskId, '已手动取消');
        syncRowGenerationStatus(task.row_id, 'error');
      }
      releaseVideoLockIfIdle();
    },
  };
});

export default useMangaStore;
