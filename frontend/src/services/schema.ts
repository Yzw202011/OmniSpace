/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 关键 API 响应 Zod 运行时校验（FE-033）
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

/* ============================== 漫剧：导演台 ============================== */

/** POST /manga/director/panorama 响应 */
export const PanoramaRespSchema = z
  .object({
    scene_id: z.string(),
    resolution: z.number(),
    panorama: z.string(),
    generated_at: z.number(),
  })
  .passthrough();
export type PanoramaResp = z.infer<typeof PanoramaRespSchema>;

/** POST /manga/director/screenshot-4in1 响应 */
export const Screenshot4in1RespSchema = z
  .object({
    scene_id: z.string(),
    camera_ids: z.array(z.string()),
    screenshot: z.string(),
    layout: z.string(),
    generated_at: z.number(),
  })
  .passthrough();
export type Screenshot4in1Resp = z.infer<typeof Screenshot4in1RespSchema>;

/** 机位（对齐后端 _row_to_camera 输出） */
export const DirectorCameraSchema = z
  .object({
    id: z.string(),
    name: z.string().default(''),
    position: z.record(z.string(), z.number()).default({}),
    rotation: z.record(z.string(), z.number()).default({}),
    fov: z.number().default(60),
  })
  .passthrough();

/** POST /manga/director/camera/add 与 PUT /manga/director/camera/{id} 响应 */
export const CameraRespSchema = z
  .object({
    camera: DirectorCameraSchema,
  })
  .passthrough();

/** POST /manga/director/character/position 响应 */
export const CharacterPositionRespSchema = z
  .object({
    character_id: z.string(),
    character: z.record(z.string(), z.unknown()),
  })
  .passthrough();

/** POST /manga/director/character/lock|unlock 响应 */
export const CharacterLockRespSchema = z
  .object({
    character_id: z.string(),
    locked: z.boolean(),
  })
  .passthrough();

/* ============================== 漫剧：视频生成 ============================== */

/** POST /manga/video/generate 响应 */
export const VideoGenerateRespSchema = z
  .object({
    task_id: z.string(),
    status: z.string(),
  })
  .passthrough();

/** GET /manga/video/{taskId}/status 响应 */
export const VideoStatusRespSchema = z
  .object({
    task_id: z.string(),
    status: z.enum(['pending', 'generating', 'done', 'error', 'cancelled']),
    progress: z.number().min(0).max(1),
    error: z.string().optional(),
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
export const ModelItemSchema = z
  .object({
    id: z.string(),
    name: z.string().optional(),
    status: z.string().optional(),
  })
  .passthrough();
