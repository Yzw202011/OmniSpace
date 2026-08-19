/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 漫剧状态类型与切片契约（TASK-P2-01 自 useMangaStore 切片）
 * --------------------------------------------------------------------------
 * 后端真实能力边界（backend/api/manga/ 逐项核对，2026-08-12 重构全量接线）：
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
 *
 * 切片结构：本目录按子域拆分（project/rows/asset/keyframe/voice/video），
 * 由 stores/useMangaStore.ts 组装为单一 store——对外接口与切片前完全一致。
 * ========================================================================== */

import type {
  ComicAsset,
  ComicAssetKind,
  ComicProject,
  KeyframeItem,
  StoryboardRow,
} from '@/types';
import type { VoiceItem } from '@/services/schema';
import type { VideoTaskStatus } from '@/constants/videoStatus';

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
  status: VideoTaskStatus;
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

/* ------------------------------ 项目切片 ------------------------------ */

export interface ProjectSlice {
  /** 项目列表（GET /comic/project/list） */
  projects: ComicProject[];
  /** 项目列表是否已加载过 */
  projectsLoaded: boolean;
  /** 项目列表加载中 */
  projectsLoading: boolean;
  /** 当前项目（null=项目库视图；打开项目后进入工作区） */
  currentProject: MangaProjectRef | null;

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
  /** 设置当前项目 */
  setProject: (project: MangaProjectRef) => void;
}

/* ------------------------------ 分镜表切片 ------------------------------ */

export interface RowsSlice {
  /** 分镜行列表 */
  rows: StoryboardRow[];
  /** 分镜表加载中 */
  loading: boolean;
  /** AI 自动分镜中 */
  splitting: boolean;
  /** 当前选中分镜行 ID（行点击联动右侧属性面板，COMIC-020） */
  selectedRowId: string | null;

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
}

/* ------------------------------ 资产切片 ------------------------------ */

export interface AssetSlice {
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

  /** 拉取当前项目资产（可按类型过滤） */
  fetchAssets: (kind?: ComicAssetKind) => Promise<void>;
  /** 绑定/解绑资产到分镜行（toggle 语义：已绑定 → unbind，未绑定 → bind；以响应 asset_ids 更新行） */
  bindAssetToRow: (assetId: string, rowId: string) => Promise<void>;
  /** 设置资产详情面板打开的资产（null 关闭；与行检查器互斥由工作台协调） */
  setSelectedAsset: (assetId: string | null) => void;
  /** 设置资产绑定目标（表格「+」进入绑定模式；null 退出） */
  setBindingTarget: (target: { rowId: string; kind: ComicAssetKind } | null) => void;
}

/* ------------------------------ 关键帧切片 ------------------------------ */

export interface KeyframeSlice {
  /** 关键帧版本缓存（row_id → 版本列表，版本倒序） */
  keyframes: Record<string, KeyframeItem[]>;

  /** 拉取分镜行关键帧版本（写入缓存并返回） */
  fetchKeyframes: (rowId: string) => Promise<KeyframeItem[]>;
  /** 失效某行关键帧缓存（生成/回退/删除后调用） */
  invalidateKeyframes: (rowId: string) => void;
}

/* ------------------------------ 音色切片 ------------------------------ */

export interface VoiceSlice {
  /** 音色列表（真实 GET /manga/voices） */
  voices: VoiceItem[];
  /** 音色是否已加载过（避免重复拉取） */
  voicesLoaded: boolean;

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
}

/* ------------------------------ 视频切片 ------------------------------ */

export interface VideoSlice {
  /** 视频生成任务列表 */
  videoTasks: MangaVideoTask[];
  /** 是否有视频生成中 */
  videoGenerating: boolean;

  /** 发起视频生成（功能互斥 video_gen + 轮询进度） */
  generateVideo: (row: StoryboardRow) => Promise<boolean>;
  /** 取消在途视频任务（POST cancel + 停止轮询） */
  cancelVideo: (taskId: string) => Promise<void>;
  /** 手动刷新任务状态 */
  fetchVideoStatus: (taskId: string) => Promise<void>;
  /** 移除已完结任务条目 */
  removeVideoTask: (taskId: string) => void;
}

/** 漫剧状态（六切片合成；对外接口与切片前的单一 MangaState 完全一致） */
export interface MangaState
  extends ProjectSlice,
    RowsSlice,
    AssetSlice,
    KeyframeSlice,
    VoiceSlice,
    VideoSlice {}
