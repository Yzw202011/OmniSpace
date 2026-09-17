/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 关键 API 响应 Zod 运行时校验（FE-033）
 * --------------------------------------------------------------------------
 * 依据：文档B §9.1.2「前端使用 Zod 校验」、文档C TS 铁律「Schema.parse(response)」。
 * 范围：漫剧模块全部响应 + 系统/硬件关键响应（崩溃高危点）。
 * 约定：
 *   - 信封 {success,data,error,meta} 由 api.ts 统一拆解；本模块校验 data 载荷。
 *   - 校验失败抛出 ApiError(FRONTEND_PARSE_ERROR)，附带首个字段错误明细。
 *   - schema 一律 .passthrough() 宽容多余字段（后端加字段不破坏前端），
 *     但必需字段缺失/类型错误立即报错（防止 undefined 渗透 UI 引发崩溃）。
 * ========================================================================== */

import { z } from 'zod';
import type { ApiError } from '@/types';

/**
 * 以 Zod schema 校验 API 响应载荷。
 * @param schema  Zod schema
 * @param data    api.ts 解包后的 data 载荷（unknown）
 * @param label   业务名（用于错误提示，如「分镜表」）
 * @returns       校验通过后的强类型数据
 * @throws        ApiError(FRONTEND_PARSE_ERROR) 校验失败时
 */
export function parseWith<S extends z.ZodType>(
  schema: S,
  data: unknown,
  label: string,
): z.infer<S> {
  const result = schema.safeParse(data);
  if (!result.success) {
    const first = result.error.issues[0];
    const path = first?.path.join('.') || '(root)';
    const err: ApiError = {
      code: 'FRONTEND_PARSE_ERROR',
      message: `${label}响应格式非法：字段 ${path} ${first?.message ?? '校验失败'}`,
      detail: JSON.stringify(result.error.issues.slice(0, 3)),
    };
    throw err;
  }
  return result.data as z.infer<S>;
}

/* ============================== 漫剧：分镜表 ============================== */

/** 分镜行（对齐后端 _row_to_storyboard_row 输出） */
export const StoryboardRowSchema = z
  .object({
    id: z.string(),
    shot_number: z.number(),
    original_dialogue: z.string().default(''),
    description: z.string().default(''),
    characters: z.array(z.string()).default([]),
    scene: z.string().default(''),
    props: z.array(z.string()).default([]),
    voice_id: z.string().default(''),
    voice_emotion: z.string().default('默认'),
    // COMIC-069：语速 0.5~2.0 / 音量 -12~0dB（范围校验在后端 PUT 行更新）
    speed: z.number().default(1.0),
    volume: z.number().default(0.0),
    director_stage_done: z.boolean().default(false),
    generation_status: z
      .enum(['pending', 'generating', 'done', 'error', 'skipped'])
      .default('pending'),
    is_ai_generated: z.boolean().default(false),
    // 2026-08-13 多资产契约：asset_ids 多绑定列表（asset_id 恒=最近绑定/剩余首元素）
    asset_ids: z.array(z.string()).default([]),
    // 行锁定：批量操作自动跳过
    is_locked: z.boolean().default(false),
  })
  .passthrough();

/** GET/PUT /manga/storyboard/{projectId} 响应 */
export const StoryboardRowsRespSchema = z
  .object({
    project_id: z.string(),
    rows: z.array(StoryboardRowSchema),
    total: z.number(),
  })
  .passthrough();

/** PUT /manga/storyboard/{projectId}/rows/{rowId} 响应 */
export const StoryboardRowRespSchema = z
  .object({
    row: StoryboardRowSchema,
  })
  .passthrough();

/** POST /manga/storyboard/{projectId}/auto-split 响应 */
export const AutoSplitRespSchema = z
  .object({
    project_id: z.string(),
    added: z.array(StoryboardRowSchema),
    total: z.number(),
  })
  .passthrough();

/** POST /manga/storyboard/{id}/auto-split dry_run=true 预览响应（2026-08-23 镜头级真分镜） */
export const AutoSplitPreviewRespSchema = z
  .object({
    project_id: z.string(),
    split_id: z.string(),
    rows: z.array(StoryboardRowSchema),
    count: z.number(),
    /** ai=全块推理成功 | ai-partial=部分块降级按行 | fallback=全程按行 */
    engine: z.string(),
    truncated: z.boolean(),
  })
  .passthrough();

/** GET /manga/storyboard/{id}/auto-split/progress 响应（切分进度轮询） */
export const SplitProgressRespSchema = z
  .object({
    active: z.boolean(),
    blocks_done: z.number(),
    blocks_total: z.number(),
    /** gpu=GPU 加速 | cpu=显存不足回落 CPU 慢速路径（预警） */
    mode: z.string().optional(),
    /** 预估总耗时（分钟，按档位） */
    eta_minutes: z.number().optional(),
  })
  .passthrough();

/** POST /manga/storyboard/import 响应 */
export const ImportScriptRespSchema = z
  .object({
    project_id: z.string(),
    rows: z.array(StoryboardRowSchema),
    total: z.number(),
  })
  .passthrough();

/** GET /manga/storyboard/{projectId}/export 响应（csv 与 json 两态） */
export const ExportStoryboardRespSchema = z
  .object({
    project_id: z.string(),
    format: z.enum(['csv', 'json']),
    content: z.string().optional(),
    rows: z.array(StoryboardRowSchema).optional(),
    total: z.number(),
  })
  .passthrough();
export type ExportStoryboardResp = z.infer<typeof ExportStoryboardRespSchema>;

/* ============================== 漫剧：视频生成 ============================== */

/** POST /manga/video/generate 响应 */
export const VideoGenerateRespSchema = z
  .object({
    task_id: z.string(),
    status: z.string(),
    /** 排队位次（1 起，入队即返回时携带；2026-09-02 视频队列） */
    queue_position: z.number().int().positive().optional(),
  })
  .passthrough();
export type VideoGenerateResp = z.infer<typeof VideoGenerateRespSchema>;

/** GET /manga/video/{taskId}/status 响应 */
export const VideoStatusRespSchema = z
  .object({
    task_id: z.string(),
    status: z.enum(['pending', 'generating', 'done', 'error', 'cancelled']),
    progress: z.number().min(0).max(1),
    error: z.string().optional(),
    /** 排队位次（1 起，仅 pending 且仍在队列中时返回；2026-09-02 视频队列） */
    queue_position: z.number().int().positive().optional(),
    /** 预计剩余秒数（生成中且引擎已采样到步耗时才返回，2026-08-22） */
    eta_seconds: z.number().int().nonnegative().optional(),
  })
  .passthrough();
export type VideoStatusResp = z.infer<typeof VideoStatusRespSchema>;

/** 视频结果详情（对齐后端 VideoGenResult + download_url/file_exists 扩展） */
export const VideoResultDetailSchema = z
  .object({
    id: z.string(),
    file_path: z.string(),
    model_used: z.string(),
    duration_seconds: z.number(),
    resolution: z.string(),
    generation_time_ms: z.number(),
    has_audio_sync: z.boolean(),
    download_url: z.string().optional(),
    file_exists: z.boolean().optional(),
  })
  .passthrough();

/** GET /manga/video/{taskId}/result 响应（未完成时 result 为 null） */
export const VideoResultRespSchema = z
  .object({
    task_id: z.string(),
    status: z.string(),
    result: VideoResultDetailSchema.nullable(),
  })
  .passthrough();
export type VideoResultResp = z.infer<typeof VideoResultRespSchema>;

/* ============================== 漫剧：音色 ============================== */

/** 音色项（对齐后端 _row_to_voice + emotions 扩展） */
export const VoiceItemSchema = z
  .object({
    id: z.string(),
    name: z.string(),
    character_id: z.string().default(''),
    is_preset: z.boolean().default(false),
    emotion: z.string().default('默认'),
    emotions: z.array(z.string()).default(['默认', '愤怒', '悲伤']),
  })
  .passthrough();
export type VoiceItem = z.infer<typeof VoiceItemSchema>;

/** GET /manga/voices 响应 */
export const VoiceListRespSchema = z
  .object({
    items: z.array(VoiceItemSchema),
    total: z.number(),
  })
  .passthrough();
export type VoiceListResp = z.infer<typeof VoiceListRespSchema>;

/** POST /manga/voices/bind 响应 */
export const VoiceBindRespSchema = z
  .object({
    character_id: z.string(),
    voice_id: z.string(),
  })
  .passthrough();

/** PUT /manga/voices/{voiceId}/emotion 响应 */
export const VoiceEmotionRespSchema = z
  .object({
    voice_id: z.string(),
    emotion: z.string(),
  })
  .passthrough();

/** POST /manga/voices/preview 响应 */
export const VoicePreviewRespSchema = z
  .object({
    voice_id: z.string(),
    text: z.string(),
    emotion: z.string(),
    audio: z.string(),
    format: z.string().default('wav'),
  })
  .passthrough();
export type VoicePreviewResp = z.infer<typeof VoicePreviewRespSchema>;

/* ============================== 系统/硬件关键响应 ============================== */

/** GET /system/version 响应 */
export const SystemVersionRespSchema = z
  .object({
    version: z.string(),
    name: z.string().optional(),
  })
  .passthrough();

/** GET /hardware/info 响应（核心字段：FE 首页曾因其缺失崩溃） */
export const HardwareInfoRespSchema = z
  .object({
    // B7 步4 补齐：cpu/power 为 HardwareProfile 必填（缺失即接口漂移，
    // 宁可在 parse 层报错也不让首页崩在 undefined 访问）
    cpu: z
      .object({
        name: z.string().default(''),
        usage_percent: z.number().optional(),
        cores: z.number().optional(),
        threads: z.number().optional(),
        temp_celsius: z.number().optional(),
      })
      .passthrough(),
    power: z.string().default('ac'),
    gpu: z
      .object({
        name: z.string().default(''),
        vram_total_mb: z.number().optional(),
      })
      .passthrough()
      .optional(),
    ram: z
      .object({
        total_gb: z.number().optional(),
        total_mb: z.number().optional(),
        available_mb: z.number().optional(),
      })
      .passthrough()
      .optional(),
    disk: z
      .object({
        total_gb: z.number().optional(),
        percent: z.number().optional(),
      })
      .passthrough()
      .optional(),
  })
  .passthrough();

/** GET /models 列表项（模型管理页关键字段） */
// B7 步4（2026-09-14）：照后端 ModelInfo（data/models.py ModelInfo）全集
// 补齐字段——schema 与前端接口类型结构性一致后，parseWith 返回可单层
// 断言（消灭 as unknown as 双跳）。宁松勿严：未知字段 passthrough 兜底。
export const ModelItemSchema = z
  .object({
    id: z.string(),
    name: z.string().default(''),
    category: z.string(),
    purpose: z.string().default(''),
    size_gb: z.number().default(0),
    params: z.string().default(''),
    min_vram_gb: z.number().default(0),
    associated_features: z.array(z.string()).default([]),
    status: z.string().default('not_installed'),
    file_path: z.string().default(''),
    sha256: z.string().default(''),
    // 前端运行时扩展：/models 响应为后端合并下载态/装载态后的视图
    downloaded: z.boolean().default(false),
    loaded: z.boolean().default(false),
  })
  .passthrough();

/* ============================== WS 消息与高频端点（批 3：FE-033 渐进覆盖） ============================== */

/** WS 帧信封：ws.ts onmessage 单点校验（/ws、/hardware/realtime、/dialog/stream 三端点共用） */
export const WsFrameSchema = z
  .object({
    type: z.string().min(1),
    data: z.unknown(),
  })
  .passthrough();

/** /hardware/realtime `system_status` 载荷（宁松勿严：只锁对象性与数值类型，
 *  字段缺失放行——normalizeRealtime 本就逐字段可选链兜底） */
export const RealtimePayloadSchema = z
  .object({
    cpu: z
      .object({ usage_percent: z.number().optional() })
      .passthrough()
      .optional(),
    ram: z
      .object({
        usage_percent: z.number().optional(),
        total_gb: z.number().optional(),
        available_gb: z.number().optional(),
      })
      .passthrough()
      .optional(),
    gpu: z
      .object({
        usage_percent: z.number().optional(),
        vram_used_mb: z.number().optional(),
        vram_total_mb: z.number().optional(),
        temp_celsius: z.number().optional(),
      })
      .passthrough()
      .optional(),
    timestamp: z.unknown().optional(),
  })
  .passthrough();

/** /ws hub 任务广播载荷（宁松勿严：仅断言「是对象」；字段差异大，
 *  逐字段守卫仍由 useTaskStore.normalizeTaskEvent 承担） */
export const TaskBroadcastSchema = z.record(z.string(), z.unknown());

/** GET /draw/result/{taskId} 绘画任务状态（queue_position 链 types→api→store） */
export const DrawStatusRespSchema = z
  .object({
    task_id: z.string().optional(),
    status: z.string(),
    percent: z.number().optional(),
    step: z.number().optional(),
    queue_position: z.number().int().positive().optional(),
    file_path: z.string().optional(),
    image: z.string().optional(),
    error: z.string().optional(),
  })
  .passthrough();

/** GET /chat/sessions 会话项（对话高频端点） */
export const DialogSessionItemSchema = z
  .object({
    id: z.string(),
    title: z.string(),
    pinned: z.boolean().optional(),
    mode: z.string().optional(),
    created_at: z.union([z.string(), z.number()]),
    updated_at: z.union([z.string(), z.number()]),
  })
  .passthrough();

/** GET /chat/sessions 分页响应 */
export const DialogSessionListRespSchema = z
  .object({
    items: z.array(DialogSessionItemSchema),
    total: z.number(),
  })
  .passthrough();

/* ============================== 小说：写作台（批2 MVP） ============================== */

/** GET /novel/project/list 项目项 */
export const NovelProjectSchema = z
  .object({
    id: z.string(),
    name: z.string(),
    genre: z.string().default(''),
    description: z.string().default(''),
    style_notes: z.string().default(''),
    chapter_total: z.number().default(0),
    chapter_done: z.number().default(0),
    total_words: z.number().default(0),
  })
  .passthrough();

/** GET /novel/project/list 响应 */
export const NovelProjectListRespSchema = z
  .object({
    items: z.array(NovelProjectSchema),
    total: z.number(),
  })
  .passthrough();

/** GET /novel/chapters/{pid} 章节轻行（不含正文） */
export const NovelChapterSchema = z
  .object({
    id: z.string(),
    project_id: z.string(),
    outline_id: z.string().default(''),
    chapter_index: z.number(),
    title: z.string().default(''),
    summary: z.string().default(''),
    word_count: z.number().default(0),
    status: z.enum(['pending', 'generating', 'done', 'error']).default('pending'),
    progress: z.number().default(0),
    error: z.string().default(''),
    updated_at: z.number().optional(),
  })
  .passthrough();

/** GET /novel/chapters/{pid} 响应 */
export const NovelChapterListRespSchema = z
  .object({
    project_id: z.string(),
    items: z.array(NovelChapterSchema),
    total: z.number(),
  })
  .passthrough();

/** GET /novel/chapter/{cid} 章节详情（含正文） */
export const NovelChapterDetailSchema = NovelChapterSchema.extend({
  content: z.string().default(''),
}).passthrough();
// 本项目仅供学习使用，商业授权请+Q 3559331368
