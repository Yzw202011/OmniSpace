/* ==========================================================================
 * 漫剧生图按钮实时进度（2026-08-27 用户需求：点击生成时按钮内进度条）
 * --------------------------------------------------------------------------
 * 数据链路：后端生成循环/采样步级回调 → common.broadcast_gen_progress
 * → WsHub task_progress → 本 hook 按 kind+ctxId 过滤 → 按钮填充进度条。
 * kind: 'keyframe'（分镜关键帧，ctxId=row_id）
 *      'asset'（资产图，ctxId=asset_id）
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { getWsHub } from '@/services/ws';

/** 规整后的生图进度事件 */
export interface GenProgress {
  /** 0~100 全局百分比 */
  percent: number;
  /** 阶段中文标签（第 2/4 镜 / 生成正面视图 / 正在生成…） */
  label: string;
  /** 当前子步（镜序/视图序） */
  current?: number;
  /** 子步总数 */
  total?: number;
  /** running / done / error */
  status?: string;
  /** 失败原因（status=error 时） */
  error?: string;
}

/**
 * 订阅指定生图上下文的实时进度（WS 断连时静默，按钮回退 spinner 文案）。
 *
 * @param kind 进度类别（keyframe / asset）
 * @param ctxId 上下文 ID（row_id / asset_id）
 * @param active 是否处于生成中（false 时不订阅、进度清空）
 */
export function useGenProgress(
  kind: 'keyframe' | 'asset',
  ctxId: string | undefined,
  active: boolean,
): GenProgress | null {
  const [progress, setProgress] = useState<GenProgress | null>(null);

  useEffect(() => {
    setProgress(null);
    if (!active || !ctxId) return;
    const conn = getWsHub();
    conn.connect();
    const off = conn.on<Record<string, unknown>>('task_progress', (data) => {
      if (!data || data.kind !== kind || String(data.id ?? '') !== ctxId) {
        return;
      }
      const percent =
        typeof data.percent === 'number'
          ? Math.max(0, Math.min(100, Math.round(data.percent)))
          : 0;
      setProgress({
        percent,
        label: typeof data.label === 'string' ? data.label : '',
        current: typeof data.current === 'number' ? data.current : undefined,
        total: typeof data.total === 'number' ? data.total : undefined,
        status: typeof data.status === 'string' ? data.status : undefined,
        error: typeof data.error === 'string' ? data.error : undefined,
      });
    });
    return () => {
      off();
    };
  }, [kind, ctxId, active]);

  return progress;
}

export default useGenProgress;
