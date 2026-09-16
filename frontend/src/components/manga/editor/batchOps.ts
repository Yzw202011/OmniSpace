import { mirrorPref } from '../../../services/uiPrefs';
/* ==========================================================================
 * batchOps.ts —— 漫剧编辑器批量行操作（工序箭头/批量条共用）
 * --------------------------------------------------------------------------
 * 后端无批量端点：ai-describe / keyframe generate 均为单行端点，
 * 批量 = 前端串行循环（真实逐行调用，进度如实汇总，不伪造并行）。
 * 前置：本地新建行（row_ 前缀）先全量持久化，单行端点要求服务端已有该行。
 * 2026-08-13 竞品对齐：is_locked 行批量操作自动跳过（计入 skipped）。
 * 2026-08-14 G2 增强：所有执行器支持 ModelConfig 覆盖（模型/分辨率）。
 * 2026-08-14 G1 新增：故事生词/故事生图/视频生词批量执行器。
 * ========================================================================== */

import {
  aiDescribe,
  generateKeyframe,
  generateStoryNarrative,
  generateStoryKeyframe,
  generateVideoNarrative,
} from '@/services/mangaApi';
import { useMangaStore } from '@/stores/useMangaStore';
import { getErrorMessage, reportBgError } from '@/utils/errors';
import type { StoryboardRow, ModelConfig } from '@/types';

/** 批量执行结果汇总 */
export interface BatchResult {
  /** 成功行数 */
  done: number;
  /** 前置不满足跳过（无台词/无描述/锁定行） */
  skipped: number;
  /** 调用失败行数 */
  failed: number;
  /** 首条失败原因（供调用方 toast 透出） */
  firstError?: string;
}

/** 批量进度回调（processed 含成功/跳过/失败，从 1 递增到 total） */
export type BatchProgress = (processed: number, total: number) => void;

/** 提取错误消息（TASK-P2-08 收敛至 @/utils/errors，全站唯一定义） */

/** 本地新建行先全量保存（单行端点要求服务端已有该行） */
async function ensurePersisted(): Promise<void> {
  const st = useMangaStore.getState();
  if (st.rows.some((r) => r.id.startsWith('row_'))) {
    await st.saveRows(st.rows);
  }
}

/** 提示词配置 localStorage 键（PromptModal 写入，批量/单行生词读取） */
export const PROMPT_CFG_KEY = 'omnispace.mangaPromptCfg';
/** 自定义提示词前缀长度上限（与后端 prompt_prefix 校验一致） */
export const PROMPT_PREFIX_MAX = 500;

/** 提示词配置（default=后端内置提示词；custom=用户自定义前缀） */
export interface PromptCfg {
  mode: 'default' | 'custom';
  custom: string;
}

/** 读取提示词配置（损坏时回退默认） */
export function readPromptCfg(): PromptCfg {
  try {
    const raw = localStorage.getItem(PROMPT_CFG_KEY);
    if (!raw) return { mode: 'default', custom: '' };
    const cfg = JSON.parse(raw) as { mode?: string; custom?: string };
    return {
      mode: cfg.mode === 'custom' ? 'custom' : 'default',
      custom: typeof cfg.custom === 'string' ? cfg.custom : '',
    };
  } catch {
    return { mode: 'default', custom: '' };
  }
}

/** 写入提示词配置（custom 截断至上限） */
export function writePromptCfg(cfg: PromptCfg): void {
  const snap = { mode: cfg.mode, custom: cfg.custom.slice(0, PROMPT_PREFIX_MAX) };
  localStorage.setItem(PROMPT_CFG_KEY, JSON.stringify(snap));
  mirrorPref(PROMPT_CFG_KEY, snap); // 界面偏好镜像（2026-09-12）
}

/** 读取提示词弹窗配置，custom 且有值时回传 prompt_prefix */
export function readPromptPrefix(): string | undefined {
  const cfg = readPromptCfg();
  if (cfg.mode === 'custom' && cfg.custom.trim()) {
    return cfg.custom.trim().slice(0, PROMPT_PREFIX_MAX);
  }
  return undefined;
}

/** 批量 AI 生成描述词（串行；锁定行/无台词行/未绑定资产行跳过；onProgress 逐行回报）
 *  2026-08-25 用户裁定：未绑定资产的行不允许生成分镜描述词（外貌一致性锚点缺失，
 *  4B 会自由发明角色外貌导致跨镜人设矛盾）——与后端 ai-describe 硬门槛双保险。 */
export async function batchDescribe(
  rows: StoryboardRow[],
  projectId: string,
  onProgress?: BatchProgress,
  config?: ModelConfig,
): Promise<BatchResult> {
  const res: BatchResult = { done: 0, skipped: 0, failed: 0 };
  await ensurePersisted();
  const promptPrefix = readPromptPrefix();
  let processed = 0;
  for (const row of rows) {
    const hasAssets = (row.asset_ids?.length ?? 0) > 0 || !!row.asset_id;
    if (row.is_locked || !row.original_dialogue.trim() || !hasAssets) {
      res.skipped += 1;
    } else {
      try {
        const description = await aiDescribe(row.id, projectId, promptPrefix, config?.dialog_model);
        if (description) {
          await useMangaStore.getState().updateRow(row.id, { description, is_ai_generated: true });
        }
        res.done += 1;
      } catch (err) {
        res.failed += 1;
        console.warn(`[batchDescribe] 镜 ${row.shot_number} 生词失败:`, err);
        if (!res.firstError) {
          res.firstError = err && typeof err === 'object' && 'message' in err
            ? (err as { message: string }).message
            : 'AI 生词失败';
        }
      }
    }
    processed += 1;
    onProgress?.(processed, rows.length);
  }
  return res;
}

/** 批量生成关键帧/分镜图（串行；锁定行/无描述行跳过；onProgress 逐行回报） */
export async function batchKeyframes(
  rows: StoryboardRow[],
  projectId: string,
  onProgress?: BatchProgress,
  config?: ModelConfig,
): Promise<BatchResult> {
  const res: BatchResult = { done: 0, skipped: 0, failed: 0 };
  await ensurePersisted();
  // 出图统一规格：分镜图默认 2560×1440（16:9）
  const resMap: Record<string, { w: number; h: number }> = {
    '2560x1440': { w: 2560, h: 1440 },
    '1024x1024': { w: 1024, h: 1024 },
    '1024x576': { w: 1024, h: 576 },
    '576x1024': { w: 576, h: 1024 },
  };
  const { w, h } = resMap[config?.resolution ?? '2560x1440'] ?? { w: 2560, h: 1440 };
  let processed = 0;
  for (const row of rows) {
    if (row.is_locked || !row.description.trim()) {
      res.skipped += 1;
    } else {
      try {
        await generateKeyframe({
          row_id: row.id,
          project_id: projectId,
          width: w,
          height: h,
        });
        useMangaStore.getState().invalidateKeyframes(row.id);
        await useMangaStore.getState().fetchKeyframes(row.id);
        res.done += 1;
      } catch (err) {
        res.failed += 1;
        console.warn(`[batchKeyframes] 镜 ${row.shot_number} 生图失败:`, err);
        if (!res.firstError) {
          res.firstError = err && typeof err === 'object' && 'message' in err
            ? (err as { message: string }).message
            : '分镜图生成失败';
        }
      }
    }
    processed += 1;
    onProgress?.(processed, rows.length);
  }
  return res;
}

/* ============================== G1 解说漫剧批量执行器 ============================== */

/** 故事生词批量执行（解说漫剧第 3 步；后端聚合故事线统一生成，前端仅调一次） */
export async function batchStoryNarrative(
  rows: StoryboardRow[],
  projectId: string,
  onProgress?: BatchProgress,
  config?: ModelConfig,
): Promise<BatchResult> {
  const res: BatchResult = { done: 0, skipped: 0, failed: 0 };
  await ensurePersisted();
  const promptPrefix = readPromptPrefix();
  const runnable = rows.filter((r) => !r.is_locked && r.original_dialogue.trim());
  const skipCount = rows.length - runnable.length;
  res.skipped = skipCount;
  if (runnable.length === 0) {
    onProgress?.(rows.length, rows.length);
    return res;
  }
  try {
    const result = await generateStoryNarrative(
      projectId,
      runnable.map((r) => r.id),
      'all',
      config?.dialog_model,
      promptPrefix,
    );
    res.done = result.done;
    res.skipped += result.skipped;
    res.failed = result.failed;
    // 更新本地行数据
    if (result.rows) {
      for (const updated of result.rows) {
        useMangaStore.getState().updateRow(updated.id, {
          description: updated.description,
          is_ai_generated: true,
        }).catch((err) => reportBgError('batchStoryNarrative.updateRow', err));
      }
    }
  } catch (err) {
    console.warn('[batchStoryNarrative] 批量故事生词失败:', err);
    res.failed = runnable.length;
    if (!res.firstError) res.firstError = getErrorMessage(err, '未知错误');
  }
  onProgress?.(rows.length, rows.length);
  return res;
}

/** 故事生图批量执行（解说漫剧第 4 步；逐行调用 keyframe 端点） */
export async function batchStoryKeyframe(
  rows: StoryboardRow[],
  projectId: string,
  onProgress?: BatchProgress,
  config?: ModelConfig,
): Promise<BatchResult> {
  const res: BatchResult = { done: 0, skipped: 0, failed: 0 };
  await ensurePersisted();
  const runnable = rows.filter((r) => !r.is_locked && r.description.trim());
  const skipCount = rows.length - runnable.length;
  res.skipped = skipCount;
  if (runnable.length === 0) {
    onProgress?.(rows.length, rows.length);
    return res;
  }
  try {
    const result = await generateStoryKeyframe(
      projectId,
      runnable.map((r) => r.id),
      'all',
      config?.paint_model,
      config?.resolution,
    );
    res.done = result.success_count;
    res.failed = result.failed.length;
    // 刷新所有相关行的关键帧缓存
    for (const kf of result.succeeded) {
      useMangaStore.getState().invalidateKeyframes(kf.row_id);
      useMangaStore.getState().fetchKeyframes(kf.row_id).catch((err) =>
        reportBgError('batchStoryKeyframe.fetchKeyframes', err),
      );
    }
  } catch (err) {
    console.warn('[batchStoryKeyframe] 批量故事生图失败:', err);
    res.failed = runnable.length;
    if (!res.firstError) res.firstError = getErrorMessage(err, '未知错误');
  }
  onProgress?.(rows.length, rows.length);
  return res;
}

/** 视频生词批量执行（解说漫剧第 5 步；后端逐行生成视频专属描述词） */
export async function batchVideoNarrative(
  rows: StoryboardRow[],
  projectId: string,
  onProgress?: BatchProgress,
  config?: ModelConfig,
): Promise<BatchResult> {
  const res: BatchResult = { done: 0, skipped: 0, failed: 0 };
  await ensurePersisted();
  const promptPrefix = readPromptPrefix();
  // A/B/C 生成不再要求已有描述词：台词（原文）或描述词任一非空即可
  const runnable = rows.filter(
    (r) => !r.is_locked && (r.description.trim() || r.original_dialogue.trim()),
  );
  const skipCount = rows.length - runnable.length;
  res.skipped = skipCount;
  if (runnable.length === 0) {
    onProgress?.(rows.length, rows.length);
    return res;
  }
  try {
    const result = await generateVideoNarrative(
      projectId,
      runnable.map((r) => r.id),
      'all',
      config?.dialog_model,
      promptPrefix,
    );
    res.done = result.done;
    res.skipped += result.skipped;
    res.failed = result.failed;
    // 更新本地行数据（后端已把 A/B/C 结构化描述词覆写入库到 description，
    // 回写 store 收敛；视频生成请求直读该字段，无需额外传参）
    if (result.rows) {
      for (const updated of result.rows) {
        useMangaStore.getState().updateRow(updated.id, {
          description: updated.description,
          is_ai_generated: true,
        }).catch((err) => reportBgError('batchVideoNarrative.updateRow', err));
      }
    }
  } catch (err) {
    console.warn('[batchVideoNarrative] 批量视频生词失败:', err);
    res.failed = runnable.length;
    if (!res.firstError) res.firstError = getErrorMessage(err, '未知错误');
  }
  onProgress?.(rows.length, rows.length);
  return res;
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
