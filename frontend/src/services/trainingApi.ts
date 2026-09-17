// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * 训练中心统一队列 API（P1 训练中心，2026-09-17 用户拍板 2A）
 * --------------------------------------------------------------------------
 * GET /training/tasks —— 聚合知识训练(/learn)与风格训练(/style)两套任务源
 * （后端 src/api/training.py 薄聚合层，单源故障 fail-soft 且 sources 披露）。
 * normalizeTrainingItem/sortUnifiedTasks 为纯函数（单测直测，node 环境）。
 * ========================================================================== */

import { get } from './api';

/** 统一训练队列条目（两源归一化后的公共形状） */
export interface UnifiedTrainingTask {
  id: string;
  kind: 'knowledge' | 'style';
  kindLabel: string;
  name: string;
  status: string;
  /** 0~1 */
  progress: number;
  created_at: number | null;
  updated_at: number | null;
}

/** 一源健康度（诚实降级：坏侧 ok=false 不伪装成空队） */
export interface TrainingSourceHealth {
  ok: boolean;
  count: number;
  error: string | null;
}

export interface UnifiedQueue {
  items: UnifiedTrainingTask[];
  total: number;
  sources: Record<string, TrainingSourceHealth>;
}

/** 归一化一条任务（与后端 _norm 同口径；容错字段缺失） */
export function normalizeTrainingItem(
  raw: Record<string, unknown>,
  kind: 'knowledge' | 'style',
): UnifiedTrainingTask {
  const name = String(
    raw.name ?? raw.style_prompt ?? raw.base_model ?? raw.id ?? '?',
  ).slice(0, 80);
  return {
    id: String(raw.id ?? ''),
    kind,
    kindLabel: kind === 'knowledge' ? '知识训练' : '风格训练',
    name,
    status: String(raw.status ?? 'unknown'),
    progress: Number(raw.progress ?? 0) || 0,
    created_at: typeof raw.created_at === 'number' ? raw.created_at : null,
    updated_at: typeof raw.updated_at === 'number' ? raw.updated_at : null,
  };
}

/** 队列排序：created_at 倒序，缺失者沉底（与后端口径一致） */
export function sortUnifiedTasks(items: UnifiedTrainingTask[]): UnifiedTrainingTask[] {
  return [...items].sort((a, b) => {
    if (a.created_at === null && b.created_at === null) return 0;
    if (a.created_at === null) return 1;
    if (b.created_at === null) return -1;
    return b.created_at - a.created_at;
  });
}

/** 拉取统一训练队列（信封由 api.get 解包；失败抛 ApiError 由调用方处置） */
export async function fetchUnifiedTasks(): Promise<UnifiedQueue> {
  const data = await get<Record<string, unknown>>('/training/tasks');
  const rawItems = Array.isArray(data.items) ? data.items : [];
  const items = sortUnifiedTasks(
    rawItems
      .filter((x): x is Record<string, unknown> => typeof x === 'object' && x !== null)
      .map((x) => {
        const kind = x.kind === 'style' ? 'style' : 'knowledge';
        return normalizeTrainingItem(x, kind);
      }),
  );
  const sources = (data.sources ?? {}) as Record<string, TrainingSourceHealth>;
  return { items, total: Number(data.total ?? items.length), sources };
}
