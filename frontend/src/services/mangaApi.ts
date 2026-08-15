/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 漫剧创作 API（对齐后端 backend/api/manga.py 真实端点）
 * --------------------------------------------------------------------------
 * 一、分镜表（/manga/storyboard/*，50 行上限 STORYBOARD_MAX_ROWS）
 *    - GET    /manga/storyboard/{projectId}            获取分镜表
 *    - PUT    /manga/storyboard/{projectId}            全量保存（增/删/重排）
 *    - PUT    /manga/storyboard/{projectId}/rows/{rowId} 更新单行
 *    - POST   /manga/storyboard/{projectId}/auto-split AI 自动分镜 {script}
 *    - POST   /manga/storyboard/import                 导入剧本 {project_id, script}
 *    - GET    /manga/storyboard/{projectId}/export     导出 ?format=csv|json
 *
 * 二、导演台（/manga/director/*，统一挂接后端默认 stage）
 *    - POST   /manga/director/panorama                 生成全景图 {scene_id, resolution}
 *    - POST   /manga/director/screenshot-4in1          4合1截图 {scene_id, camera_ids[4]}
 *    - POST   /manga/director/character/position       更新角色位置
 *    - POST   /manga/director/camera/add               添加机位
 *    - PUT    /manga/director/camera/{cameraId}        更新机位
 *    - POST   /manga/director/character/lock|unlock    锁定/解锁占位
 *
 * 三、视频生成（/manga/video/*，生成期间持有 video_gen 功能锁）
 *    - POST   /manga/video/generate                    发起生成 → {task_id, status}
 *    - GET    /manga/video/{taskId}/status             轮询状态/进度
 *    - GET    /manga/video/{taskId}/result             取结果（含 download_url）
 *    - GET    /manga/video/{taskId}/download           下载 MP4（raw 文件流）
 *
 * 四、音色（/manga/voices/*，预置音色后端首启种子化）
 *    - GET    /manga/voices                            音色列表 {items, total}
 *    - POST   /manga/voices/bind                       绑定角色 {voice_id, character_id}
 *    - PUT    /manga/voices/{voiceId}/emotion          更新情感 {emotion_label}
 *    - POST   /manga/voices/preview                    试听 {voice_id, text, emotion}
 *
 * 注：响应统一经 services/schema.ts 的 Zod schema 运行时校验（FE-033）。
 * ========================================================================== */

import { get, post, put, del, upload, API_BASE } from './api';
import {
  StoryboardRowsRespSchema,
  StoryboardRowRespSchema,
  AutoSplitRespSchema,
  ImportScriptRespSchema,
  ExportStoryboardRespSchema,
  PanoramaRespSchema,
  Screenshot4in1RespSchema,
  CameraRespSchema,
  CharacterPositionRespSchema,
  CharacterLockRespSchema,
  VideoGenerateRespSchema,
  VideoStatusRespSchema,
  VideoResultRespSchema,
  VoiceListRespSchema,
  VoiceBindRespSchema,
  VoiceEmotionRespSchema,
  VoicePreviewRespSchema,
  parseWith,
  type ExportStoryboardResp,
  type PanoramaResp,
  type Screenshot4in1Resp,
  type VideoResultResp,
  type VideoStatusResp,
  type VoiceListResp,
  type VoicePreviewResp,
} from './schema';
import type {
  StoryboardRow,
  ComicProject,
  ComicAsset,
  ComicAssetKind,
  KeyframeItem,
} from '@/types';

/* ============================== 一、分镜表 ============================== */

/** 获取分镜表行（GET /manga/storyboard/{projectId}） */
export async function getStoryboardRows(projectId: string): Promise<StoryboardRow[]> {
  const res = parseWith(
    StoryboardRowsRespSchema,
    await get<unknown>(`/manga/storyboard/${projectId}`),
    '分镜表',
  );
  return res.rows ?? [];
}

/** 全量保存分镜表（PUT /manga/storyboard/{projectId}，覆盖增/删/重排） */
export async function saveStoryboardRows(
  projectId: string,
  rows: StoryboardRow[],
): Promise<StoryboardRow[]> {
  const res = parseWith(
    StoryboardRowsRespSchema,
    await put<unknown>(`/manga/storyboard/${projectId}`, { rows }),
    '分镜表保存',
  );
  return res.rows ?? [];
}

/** 更新单个分镜行（PUT /manga/storyboard/{projectId}/rows/{rowId}，仅更新非空字段） */
export async function updateStoryboardRow(
  projectId: string,
  rowId: string,
  patch: Partial<StoryboardRow>,
): Promise<StoryboardRow> {
  const res = parseWith(
    StoryboardRowRespSchema,
    await put<unknown>(`/manga/storyboard/${projectId}/rows/${rowId}`, patch),
    '分镜行更新',
  );
  return res.row;
}

/** AI 自动分镜（POST /manga/storyboard/{projectId}/auto-split，按剧本文本拆行） */
export async function autoSplitStoryboard(
  projectId: string,
  script: string,
): Promise<{ added: StoryboardRow[]; total: number }> {
  const res = parseWith(
    AutoSplitRespSchema,
    await post<unknown>(`/manga/storyboard/${projectId}/auto-split`, { script }),
    'AI 自动分镜',
  );
  return { added: res.added, total: res.total };
}

/** 导入剧本（POST /manga/storyboard/import，剧本文本按行拆分为分镜行） */
export async function importScript(
  projectId: string,
  script: string,
): Promise<{ rows: StoryboardRow[]; total: number }> {
  const res = parseWith(
    ImportScriptRespSchema,
    await post<unknown>('/manga/storyboard/import', { project_id: projectId, script }),
    '剧本导入',
  );
  return { rows: res.rows, total: res.total };
}

/** 导出分镜表（GET /manga/storyboard/{projectId}/export?format=csv|json） */
export async function exportStoryboard(
  projectId: string,
  format: 'csv' | 'json',
): Promise<ExportStoryboardResp> {
  return parseWith(
    ExportStoryboardRespSchema,
    await get<unknown>(`/manga/storyboard/${projectId}/export`, { format }),
    '分镜表导出',
  );
}

/* ============================== 二、导演台 ============================== */

/** 生成全景图（POST /manga/director/panorama；resolution ∈ 1024/2048/4096/8192） */
export async function generatePanorama(
  sceneId: string,
  resolution: number,
): Promise<PanoramaResp> {
  return parseWith(
    PanoramaRespSchema,
    await post<unknown>('/manga/director/panorama', {
      scene_id: sceneId,
      resolution,
    }),
    '全景图生成',
  );
}

/** 4合1截图（POST /manga/director/screenshot-4in1；必须恰好 4 个机位，否则 SCREENSHOT_CAMERA_MISMATCH） */
export async function screenshot4in1(
  sceneId: string,
  cameraIds: string[],
): Promise<Screenshot4in1Resp> {
  return parseWith(
    Screenshot4in1RespSchema,
    await post<unknown>('/manga/director/screenshot-4in1', {
      scene_id: sceneId,
      camera_ids: cameraIds,
    }),
    '4合1截图',
  );
}

/** 更新角色位置（POST /manga/director/character/position） */
export async function setCharacterPosition(body: {
  character_id: string;
  position: { x: number; y: number; z: number };
  rotation?: { x: number; y: number; z: number };
  scale?: number;
}): Promise<void> {
  parseWith(
    CharacterPositionRespSchema,
    await post<unknown>('/manga/director/character/position', body),
    '角色位置更新',
  );
}

/** 添加机位（POST /manga/director/camera/add）→ 返回后端持久化的机位 id */
export async function addCamera(body: {
  name: string;
  position: { x: number; y: number; z: number };
  rotation: { x: number; y: number; z: number };
  fov: number;
}): Promise<string> {
  const res = parseWith(
    CameraRespSchema,
    await post<unknown>('/manga/director/camera/add', body),
    '机位添加',
  );
  return res.camera.id;
}

/** 更新机位（PUT /manga/director/camera/{cameraId}，仅更新非空字段） */
export async function updateCamera(
  cameraId: string,
  patch: {
    name?: string;
    position?: { x: number; y: number; z: number };
    rotation?: { x: number; y: number; z: number };
    fov?: number;
  },
): Promise<void> {
  parseWith(
    CameraRespSchema,
    await put<unknown>(`/manga/director/camera/${cameraId}`, patch),
    '机位更新',
  );
}

/** 锁定/解锁角色占位（POST /manga/director/character/lock|unlock） */
export async function setCharacterLock(
  characterId: string,
  locked: boolean,
): Promise<void> {
  parseWith(
    CharacterLockRespSchema,
    await post<unknown>(`/manga/director/character/${locked ? 'lock' : 'unlock'}`, {
      character_id: characterId,
    }),
    '角色锁定',
  );
}

/* ============================== 三、视频生成 ============================== */

/** 视频生成参数（对齐后端 VideoGenerateRequest 模型） */
export interface VideoGeneratePayload {
  /** 分镜行 ID */
  storyboard_row_id: string;
  /** 画面描述 */
  description: string;
  /** 4合1截图 base64（可空字符串） */
  screenshot_4in1: string;
  /** 角色资产列表 */
  character_assets?: string[];
  /** 音频路径（可选，带音频时最长 10 秒） */
  audio_path?: string;
  /** 分辨率 720p/1080p/2k/4k */
  resolution?: string;
  /** 帧率 1~48 */
  fps?: number;
  /** 时长秒 1~20 */
  duration_seconds?: number;
  /** 编码 h264/h265/vp9/av1 */
  codec?: string;
  /** 模型覆盖（可选） */
  model_override?: string;
}

/** 发起视频生成（POST /manga/video/generate）→ {task_id, status} */
export async function generateVideo(
  body: VideoGeneratePayload,
): Promise<{ task_id: string; status: string }> {
  return parseWith(
    VideoGenerateRespSchema,
    await post<unknown>('/manga/video/generate', body),
    '视频生成',
  );
}

/** 轮询视频生成状态（GET /manga/video/{taskId}/status） */
export async function getVideoStatus(taskId: string): Promise<VideoStatusResp> {
  return parseWith(
    VideoStatusRespSchema,
    await get<unknown>(`/manga/video/${taskId}/status`),
    '视频状态',
  );
}

/** 获取视频生成结果（GET /manga/video/{taskId}/result） */
export async function getVideoResult(taskId: string): Promise<VideoResultResp> {
  return parseWith(
    VideoResultRespSchema,
    await get<unknown>(`/manga/video/${taskId}/result`),
    '视频结果',
  );
}

/** 视频下载地址（GET /manga/video/{taskId}/download，文件流） */
export function getVideoDownloadUrl(taskId: string): string {
  return `${API_BASE}/manga/video/${taskId}/download`;
}

/** 视频任务记录项（GET /manga/video/tasks item，生成记录弹窗数据源） */
export interface VideoTaskRecord {
  task_id: string;
  row_id: string;
  shot_number: number;
  status: string;
  progress: number;
  file_path: string;
  model_used: string;
  created_at: number;
  generation_time_ms: number;
}

/** 项目视频任务记录（GET /manga/video/tasks?project_id=） */
export async function listVideoTasks(
  projectId: string,
): Promise<{ items: VideoTaskRecord[]; total: number }> {
  return get('/manga/video/tasks', { project_id: projectId });
}

/* ============================== 四、音色 ============================== */

/** 音色列表（GET /manga/voices）→ {items, total} */
export async function listVoices(): Promise<VoiceListResp['items']> {
  const res = parseWith(
    VoiceListRespSchema,
    await get<unknown>('/manga/voices'),
    '音色列表',
  );
  return res.items;
}

/** 绑定角色与音色（POST /manga/voices/bind） */
export async function bindVoice(voiceId: string, characterId: string): Promise<void> {
  parseWith(
    VoiceBindRespSchema,
    await post<unknown>('/manga/voices/bind', {
      voice_id: voiceId,
      character_id: characterId,
    }),
    '音色绑定',
  );
}

/** 更新音色情感（PUT /manga/voices/{voiceId}/emotion） */
export async function updateVoiceEmotion(
  voiceId: string,
  emotionLabel: string,
): Promise<void> {
  parseWith(
    VoiceEmotionRespSchema,
    await put<unknown>(`/manga/voices/${voiceId}/emotion`, {
      emotion_label: emotionLabel,
    }),
    '音色情感更新',
  );
}

/** 试听音色（POST /manga/voices/preview）→ base64 音频 */
export async function previewVoice(
  voiceId: string,
  text: string,
  emotion?: string,
): Promise<VoicePreviewResp> {
  return parseWith(
    VoicePreviewRespSchema,
    await post<unknown>('/manga/voices/preview', {
      voice_id: voiceId,
      text,
      emotion: emotion || '默认',
    }),
    '音色试听',
  );
}

/* ============================== 五、项目 CRUD（/comic/project/*） ==============================
 * 漫剧重构（2026-08-12）接线：后端项目 CRUD 一直存在（COMIC-001~004），
 * 旧版前端「后端无项目端点」注释系陈旧结论，本轮全量接线。
 * 响应形状经 backend/api/manga.py 端点逐一核对（信封内 data 直取）。
 * ============================================================================================== */

/** 新建项目（POST /comic/project/create；template=comic_drama 预置 5 行漫剧分镜；workMode=narrative 解说漫剧） */
export async function createProject(
  name: string,
  template?: string,
  workMode?: string,
): Promise<{ project_id: string; name: string; rows: StoryboardRow[] }> {
  return post('/comic/project/create', { name, template: template || undefined, work_mode: workMode || 'regular' });
}

/** 项目列表（GET /comic/project/list，按更新时间倒序） */
export async function listProjects(): Promise<ComicProject[]> {
  const res = await get<{ items: ComicProject[]; total: number }>('/comic/project/list');
  return res.items ?? [];
}

/** 重命名项目（PUT /comic/project/{id}；重名 → COMIC_PROJECT_NAME_DUPLICATED） */
export async function renameProject(projectId: string, name: string): Promise<void> {
  await put(`/comic/project/${projectId}`, { name });
}

/** 删除项目（DELETE /comic/project/{id}，级联删除分镜/资产/关键帧/场景对象） */
export async function deleteProject(projectId: string): Promise<void> {
  await del(`/comic/project/${projectId}`);
}

/* ============================== 六、剧本导入（DSL 文件） ============================== */

/** DSL 剧本文件上传导入（POST /comic/script/import-dsl，multipart .txt/.dsl ≤10MB） */
export async function importDslFile(
  projectId: string,
  file: File,
  strict = false,
): Promise<{ added: number }> {
  const fd = new FormData();
  fd.append('file', file);
  const res = await upload<{ added?: number; rows?: StoryboardRow[] }>(
    '/comic/script/import-dsl',
    fd,
    { query: { project_id: projectId, strict } },
  );
  return { added: res.added ?? res.rows?.length ?? 0 };
}

/* ============================== 七、分镜 AI 辅助与重排 ============================== */

/** 拖拽重排（POST /storyboard/reorder：按 row_ids 顺序重写 sort_index 并持久化） */
export async function reorderStoryboard(
  rowIds: string[],
  projectId?: string,
): Promise<StoryboardRow[]> {
  const res = await post<{ rows: StoryboardRow[]; total: number }>(
    '/storyboard/reorder',
    { row_ids: rowIds, project_id: projectId || undefined },
  );
  return res.rows ?? [];
}

/** AI 画面描述（POST /storyboard/ai-describe；对话引擎未就绪 → DIALOG_NOT_READY 诚实错误；
 *  promptPrefix 为可选用户自定义提示词前缀（≤500 字，提示词弹窗配置）；
 *  modelOverride 为可选模型覆盖（G2 工序弹窗选择）） */
export async function aiDescribe(
  rowId: string,
  projectId?: string,
  promptPrefix?: string,
  modelOverride?: string,
): Promise<string> {
  const res = await post<{ description: string; model?: string }>(
    '/storyboard/ai-describe',
    {
      row_id: rowId,
      project_id: projectId || undefined,
      prompt_prefix: promptPrefix?.trim() ? promptPrefix.trim() : undefined,
      model_override: modelOverride || undefined,
    },
  );
  return res.description;
}

/** 分镜预览图（POST /storyboard/preview → base64 PNG；引擎未就绪 degraded:true 占位图） */
export async function previewStoryboardImage(
  rowId: string,
  projectId?: string,
): Promise<{ image: string; degraded?: boolean; degrade_reason?: string }> {
  return post('/storyboard/preview', {
    row_id: rowId,
    project_id: projectId || undefined,
  });
}

/** 台词情绪识别（POST /manga/storyboard/emotion-detect；引擎未就绪回退规则词典 degraded） */
export async function detectEmotion(
  text: string,
): Promise<{ emotion: string; engine: string; degraded?: boolean; degrade_reason?: string }> {
  return post('/manga/storyboard/emotion-detect', { text });
}

/* ============================== 八、资产库（/comic/asset/*） ============================== */

/** 资产清单（GET /comic/asset/library?project_id&kind） */
export async function listAssets(
  projectId: string,
  kind?: ComicAssetKind,
): Promise<ComicAsset[]> {
  const res = await get<{ items: ComicAsset[]; total: number }>('/comic/asset/library', {
    project_id: projectId,
    kind,
  });
  return res.items ?? [];
}

/** 角色多视图生成（POST /comic/asset/generate-turnaround：2560×1440 横排 4 格，自动裁切入库；
 *  FLUX 未随包 → SDXL 兜底（1280×720 生成 + 2x 上采样），响应带 degraded 标记） */
export async function generateTurnaround(body: {
  project_id: string;
  name: string;
  prompt: string;
  seed?: number;
  transparent?: boolean;
}): Promise<ComicAsset & { degraded?: boolean; degrade_reason?: string }> {
  return post('/comic/asset/generate-turnaround', body);
}

/** 单个资产生成（POST /comic/asset/batch-generate 单项形态复用，后端仅暴露批量端点） */
export async function generateAsset(body: {
  project_id: string;
  kind: ComicAssetKind;
  name: string;
  prompt: string;
  width?: number;
  height?: number;
  transparent?: boolean;
}): Promise<{ succeeded: ComicAsset[]; failed: { name: string; message: string }[] }> {
  const res = await post<{
    succeeded: ComicAsset[];
    failed: { name: string; code: string; message: string }[];
  }>('/comic/asset/batch-generate', {
    project_id: body.project_id,
    kind: body.kind,
    items: [
      {
        name: body.name,
        prompt: body.prompt,
        // 出图统一规格：资产图 2560×1440（16:9）
        width: body.width ?? 2560,
        height: body.height ?? 1440,
        transparent: body.transparent ?? false,
      },
    ],
  });
  return res;
}

/** 批量资产生成（POST /comic/asset/batch-generate） */
export async function batchGenerateAssets(
  projectId: string,
  kind: ComicAssetKind,
  items: { name: string; prompt: string }[],
): Promise<{
  succeeded: ComicAsset[];
  failed: { name: string; code: string; message: string }[];
  success_count: number;
  total: number;
}> {
  return post('/comic/asset/batch-generate', { project_id: projectId, kind, items });
}

/** 资产绑定响应（2026-08-13 多资产契约：bind/unbind 均返回最新 asset_ids 列表） */
export interface AssetBindResp {
  asset_id: string;
  row_id: string;
  asset_ids: string[];
}

/** 资产 ↔ 分镜行绑定（PUT /comic/asset/bind，追加语义+去重，写 storyboard_rows.asset_ids） */
export async function bindAsset(assetId: string, rowId: string): Promise<AssetBindResp> {
  return put('/comic/asset/bind', { asset_id: assetId, row_id: rowId });
}

/** 资产 ↔ 分镜行解绑（PUT /comic/asset/unbind，从 asset_ids 移除） */
export async function unbindAsset(assetId: string, rowId: string): Promise<AssetBindResp> {
  return put('/comic/asset/unbind', { asset_id: assetId, row_id: rowId });
}

/** 更新资产名称/描述词（PUT /comic/asset/{assetId}） */
export async function updateAsset(
  assetId: string,
  patch: { name?: string; prompt?: string },
): Promise<ComicAsset> {
  const res = await put<{ asset: ComicAsset }>(`/comic/asset/${assetId}`, patch);
  return res.asset;
}

/** 资产 AI 生图（POST /comic/asset/{assetId}/regenerate；prompt 为空 → 40008 诚实错误；
 *  角色资产可选 body {mode:'four_views'}：首次携带升级为四视图资产（front/side/back/closeup
 *  四张独立 2560×1440，约 40-90s）；已有 meta.turnaround 的角色传 {} 或不传即重生成四视图） */
export async function regenerateAsset(
  assetId: string,
  body?: { mode?: string },
): Promise<{ asset: ComicAsset; degraded?: boolean; degrade_reason?: string }> {
  return post(`/comic/asset/${assetId}/regenerate`, body);
}

/** 本地图片上传为资产（POST /comic/asset/upload，multipart png/jpg/jpeg/webp ≤10MB） */
export async function uploadAsset(
  projectId: string,
  kind: ComicAssetKind,
  name: string,
  file: File,
): Promise<ComicAsset> {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('project_id', projectId);
  fd.append('kind', kind);
  fd.append('name', name);
  const res = await upload<{ asset: ComicAsset }>('/comic/asset/upload', fd);
  return res.asset;
}

/** 实体推理（POST /comic/asset/infer-entities：从分镜行 characters/scene/props 聚合建无图资产桩 status=stub） */
export async function inferEntities(projectId: string): Promise<{
  created: number;
  reused: number;
  items: { id: string; name: string; kind: string; status: string }[];
}> {
  return post('/comic/asset/infer-entities', { project_id: projectId });
}

/** 资产描述词 AI 扩写（POST /comic/asset/{assetId}/describe；对话引擎未就绪 → DIALOG_NOT_READY） */
export async function describeAsset(assetId: string): Promise<ComicAsset> {
  const res = await post<{ asset: ComicAsset }>(`/comic/asset/${assetId}/describe`);
  return res.asset;
}

/** 资产打包导出（POST /comic/asset/export-pack → zip file_path，经 /manga/media 下载） */
export async function exportAssetPack(
  projectId: string,
): Promise<{ file_path: string; total?: number }> {
  return post('/comic/asset/export-pack', { project_id: projectId });
}

/** 四视图单视图键（对齐后端 meta.views 键名） */
export type TurnaroundViewKey = 'front' | 'side' | 'back' | 'closeup';

/** 四视图资产单视图重生（POST /comic/asset/{assetId}/regenerate-view，约 10-30s/张） */
export async function regenerateAssetView(
  assetId: string,
  view: TurnaroundViewKey,
  prompt?: string,
): Promise<{ asset: ComicAsset; view: string; file_path: string }> {
  return post(`/comic/asset/${assetId}/regenerate-view`, { view, prompt: prompt || undefined });
}

/** 上传 AI 参考图（POST /comic/asset/{assetId}/reference，multipart 字段名 file，
 *  png/jpg/jpeg/webp ≤10MB；角色四视图生成时携带参考图走 img2img 保持人设一致性） */
export async function uploadAssetReference(assetId: string, file: File): Promise<ComicAsset> {
  const fd = new FormData();
  fd.append('file', file);
  const res = await upload<{ asset: ComicAsset }>(`/comic/asset/${assetId}/reference`, fd);
  return res.asset;
}

/** 删除 AI 参考图（DELETE /comic/asset/{assetId}/reference） */
export async function deleteAssetReference(assetId: string): Promise<void> {
  await del(`/comic/asset/${assetId}/reference`);
}

/** 资产生成历史项（GET /comic/asset/{assetId}/history data.items 元素；url 可直接作 img src） */
export interface AssetHistoryItem {
  /** 生成时间戳（秒或毫秒，前端自适应） */
  ts: number;
  kind: string;
  view: string | null;
  file: string;
  url: string;
}

/** 资产生成历史（GET /comic/asset/{assetId}/history，最新在前，上限 12 条） */
export async function fetchAssetHistory(assetId: string): Promise<AssetHistoryItem[]> {
  const res = await get<{ items: AssetHistoryItem[] }>(`/comic/asset/${assetId}/history`);
  return res.items ?? [];
}

/** 资产库资产复制引入当前项目（POST /comic/asset/adopt；幂等，重复引入返回 already_adopted=true 的既有副本） */
export async function adoptAsset(
  assetId: string,
  projectId: string,
): Promise<{ asset: ComicAsset; alreadyAdopted: boolean }> {
  const res = await post<{ asset: ComicAsset; already_adopted?: boolean }>('/comic/asset/adopt', {
    asset_id: assetId,
    project_id: projectId,
  });
  return { asset: res.asset, alreadyAdopted: res.already_adopted === true };
}

/** 跨项目全部角色资产（GET /comic/asset/library?kind=character，不传 project_id = 资产库全量） */
export async function listLibraryCharacters(): Promise<ComicAsset[]> {
  const res = await get<{ items: ComicAsset[]; total: number }>('/comic/asset/library', {
    kind: 'character',
  });
  return res.items ?? [];
}

/* ============================== 九、关键帧（/manga/keyframe/*） ============================== */

/** 生成关键帧（POST /manga/keyframe/generate：分镜行描述 → SDXL 文生图 → 新版本登记） */
export async function generateKeyframe(body: {
  row_id: string;
  project_id?: string;
  prompt?: string;
  width?: number;
  height?: number;
}): Promise<KeyframeItem> {
  return post('/manga/keyframe/generate', body);
}

/** 批量关键帧（POST /manga/keyframe/batch，逐行串行，聚合成功/失败明细） */
export async function batchGenerateKeyframes(
  rowIds: string[],
  projectId?: string,
): Promise<{
  succeeded: KeyframeItem[];
  failed: { row_id: string; code: string; message: string }[];
  success_count: number;
  total: number;
}> {
  return post('/manga/keyframe/batch', {
    row_ids: rowIds,
    project_id: projectId || undefined,
  });
}

/** 重新生成关键帧（POST /manga/keyframe/regenerate：产出 v{n+1}，旧版保留可回退） */
export async function regenerateKeyframe(body: {
  row_id: string;
  project_id?: string;
  prompt?: string;
}): Promise<KeyframeItem> {
  return post('/manga/keyframe/regenerate', body);
}

/** 关键帧版本回退（POST /manga/keyframe/rollback：指定版本置为当前） */
export async function rollbackKeyframe(keyframeId: string): Promise<void> {
  await post('/manga/keyframe/rollback', { keyframe_id: keyframeId });
}

/** 删除关键帧版本（DELETE /manga/keyframe/{id}；当前版本删除后自动回退上一版） */
export async function deleteKeyframe(keyframeId: string): Promise<void> {
  await del(`/manga/keyframe/${keyframeId}`);
}

/** 分镜行关键帧版本列表（GET /manga/keyframe/list?row_id=，版本倒序） */
export async function listKeyframes(rowId: string): Promise<KeyframeItem[]> {
  const res = await get<{ items: KeyframeItem[]; total: number }>(
    '/manga/keyframe/list',
    { row_id: rowId },
  );
  return res.items ?? [];
}

/* ============================== 十、导出与取消 ============================== */

/** 项目合并打包（POST /comic/export/bundle：分镜+资产+关键帧+视频 → 单 zip） */
export async function exportBundle(projectId: string): Promise<{
  file_path: string;
  contents: { storyboard_rows: number; assets: number; videos: number; keyframes: number };
}> {
  return post('/comic/export/bundle', { project_id: projectId });
}

/** 导演台状态导出（POST /director/export → JSON 包，前端下载存档） */
export async function exportDirectorStage(stageId?: string): Promise<Record<string, unknown>> {
  return post('/director/export', stageId ? { stage_id: stageId } : {});
}

/** 取消视频任务（POST /video/{taskId}/cancel） */
export async function cancelVideo(taskId: string): Promise<void> {
  await post(`/manga/video/${taskId}/cancel`);
}

/* ============================== 十一、G1 解说漫剧 ============================== */

/** 故事生词（POST /manga/story/narrative） */
export async function generateStoryNarrative(
  projectId: string,
  rowIds: string[],
  scope: 'all' | 'missing' = 'all',
  modelOverride?: string,
  promptPrefix?: string,
): Promise<{ done: number; skipped: number; failed: number; rows: StoryboardRow[] }> {
  return post('/manga/story/narrative', {
    project_id: projectId,
    row_ids: rowIds,
    scope,
    model_override: modelOverride,
    prompt_prefix: promptPrefix,
  });
}

/** 故事生图（POST /manga/story/keyframe） */
export async function generateStoryKeyframe(
  projectId: string,
  rowIds: string[],
  scope: 'all' | 'missing' = 'all',
  modelOverride?: string,
  resolution?: string,
): Promise<{
  succeeded: KeyframeItem[];
  failed: { row_id: string; message: string }[];
  success_count: number;
  total: number;
  degraded?: boolean;
  degrade_reason?: string;
}> {
  return post('/manga/story/keyframe', {
    project_id: projectId,
    row_ids: rowIds,
    scope,
    model_override: modelOverride,
    resolution,
  });
}

/** 视频生词（POST /manga/video/narrative；rows 为后端追加视频描述词后的更新行，对齐 story/narrative 契约） */
export async function generateVideoNarrative(
  projectId: string,
  rowIds: string[],
  scope: 'all' | 'missing' = 'all',
  modelOverride?: string,
  promptPrefix?: string,
): Promise<{ done: number; skipped: number; failed: number; rows: StoryboardRow[] }> {
  return post('/manga/video/narrative', {
    project_id: projectId,
    row_ids: rowIds,
    scope,
    model_override: modelOverride,
    prompt_prefix: promptPrefix,
  });
}

/** 可用模型列表（GET /manga/models/available?task_type=dialog|paint|video） */
export async function listAvailableModels(
  taskType: 'dialog' | 'paint' | 'video',
): Promise<{ items: import('@/types').AvailableModel[] }> {
  return get('/manga/models/available', { task_type: taskType });
}

/**
 * 漫剧媒体文件 URL（GET /manga/media/{relpath}）。
 * 资产/关键帧/导出包记录中的 file_path（DATA_DIR 相对路径）经此回读；
 * 后端白名单校验（comic_assets / keyframes / generated/exports）。
 *
 * version：缓存破除参数（资产图为覆盖写同路径，URL 不变浏览器会命中旧缓存）。
 * 调用方传入随生成变化的信号（meta.seed / meta.regenerated_at / created_at）。
 */
export function getMediaUrl(filePath: string, version?: string | number): string {
  const rel = filePath.replace(/\\/g, '/').replace(/^\/+/, '');
  const url = `${API_BASE}/manga/media/${rel.split('/').map(encodeURIComponent).join('/')}`;
  if (version === undefined || version === null || version === '') return url;
  return `${url}?v=${encodeURIComponent(String(version))}`;
}

/**
 * 资产图缓存破除版本信号：regenerated_at（重生成时间）> seed（每次生成变化）
 * > created_at。AI 生图/上传/多视图生成后版本必变，浏览器重新拉取新图。
 */
export function assetMediaVersion(asset: ComicAsset): string | number {
  const meta = asset.meta || {};
  const regen = meta.regenerated_at;
  if (typeof regen === 'string' && regen) return regen;
  const seed = meta.seed;
  if (typeof seed === 'number' && seed >= 0) return seed;
  return asset.created_at || '';
}

export default {
  // 分镜表
  getStoryboardRows,
  saveStoryboardRows,
  updateStoryboardRow,
  autoSplitStoryboard,
  importScript,
  exportStoryboard,
  reorderStoryboard,
  aiDescribe,
  previewStoryboardImage,
  detectEmotion,
  // 导演台
  generatePanorama,
  screenshot4in1,
  setCharacterPosition,
  addCamera,
  updateCamera,
  setCharacterLock,
  exportDirectorStage,
  // 视频
  generateVideo,
  getVideoStatus,
  getVideoResult,
  getVideoDownloadUrl,
  listVideoTasks,
  cancelVideo,
  // 音色
  listVoices,
  bindVoice,
  updateVoiceEmotion,
  previewVoice,
  // 项目
  createProject,
  listProjects,
  renameProject,
  deleteProject,
  // 剧本导入
  importDslFile,
  // 资产
  listAssets,
  generateTurnaround,
  generateAsset,
  batchGenerateAssets,
  bindAsset,
  unbindAsset,
  updateAsset,
  regenerateAsset,
  regenerateAssetView,
  uploadAsset,
  uploadAssetReference,
  deleteAssetReference,
  fetchAssetHistory,
  adoptAsset,
  listLibraryCharacters,
  inferEntities,
  describeAsset,
  exportAssetPack,
  // 关键帧
  generateKeyframe,
  batchGenerateKeyframes,
  regenerateKeyframe,
  rollbackKeyframe,
  deleteKeyframe,
  listKeyframes,
  // 媒体
  getMediaUrl,
};
