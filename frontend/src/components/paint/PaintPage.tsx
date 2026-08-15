/* ==========================================================================
 * OmniSpace AI v2.1 —— 绘画页（容器组件）
 * --------------------------------------------------------------------------
 * 职责：将 usePaintStore 状态映射为 PaintView 所需 props。
 * PaintView 是受控展示组件（props-injected），本组件负责数据桥接与
 * 生命周期管理（挂载时拉取历史与绘画模型列表）。
 *
 * 关键链路：
 *   UI 编辑参数 → onGenerate(params, model?) 显式回传 → 组装 PaintRequest
 *   → store.generate(req) → /draw/generate → 轮询 /draw/result → 结果入库
 * 进度条数据源：useTaskStore 中当前任务 progress（0~1）× 100。
 * ========================================================================== */

import { useEffect, useCallback, useMemo } from 'react';
import { PaintView } from './PaintView';
import type { PaintParamsValue } from './PaintParams';
import type { GeneratedImage } from './ImageGrid';
import { usePaintStore } from '@/stores/usePaintStore';
import { useTaskStore } from '@/stores/useTaskStore';
import type { PaintRequest, PaintResult } from '@/types';

/** PaintRequest (store) → PaintParamsValue (component) */
function mapParams(req: PaintRequest): PaintParamsValue {
  return {
    prompt: req.prompt,
    negativePrompt: req.negative_prompt ?? '',
    width: req.width ?? 1024,
    height: req.height ?? 1024,
    steps: req.steps ?? 20,
    guidanceScale: req.cfg_scale ?? 7.5,
    seed: req.seed ?? -1,
    batchSize: req.batch_size ?? 1,
    sampler: req.sampler ?? 'dpm++_2m_karras',
    controlnet: null,
    loras: (req.loras ?? []).map((l) => ({
      name: l.name,
      weight: l.weight,
      enabled: true,
    })),
  };
}

/** PaintResult (store) → GeneratedImage (component) */
function mapResult(r: PaintResult): GeneratedImage {
  return {
    id: r.id,
    url: r.url,
    prompt: r.params.prompt,
    width: r.params.width,
    height: r.params.height,
    seed: r.seed,
    favorite: r.favorite,
  };
}

export default function PaintPage() {
  const paintRequest = usePaintStore((s) => s.paintRequest);
  const results = usePaintStore((s) => s.results);
  const generating = usePaintStore((s) => s.generating);
  const currentTaskId = usePaintStore((s) => s.currentTaskId);
  const historyLoaded = usePaintStore((s) => s.historyLoaded);
  const models = usePaintStore((s) => s.models);
  const generate = usePaintStore((s) => s.generate);
  const fetchHistory = usePaintStore((s) => s.fetchHistory);
  const fetchModels = usePaintStore((s) => s.fetchModels);

  // 当前任务进度（0~1），来自全局任务 store（WS/轮询回写）
  const taskProgress = useTaskStore((s) =>
    currentTaskId
      ? s.tasks.find((t) => t.id === currentTaskId)?.progress
      : undefined,
  );

  // 挂载时拉取历史与绘画模型列表
  useEffect(() => {
    if (!historyLoaded) {
      fetchHistory();
    }
    fetchModels();
  }, [historyLoaded, fetchHistory, fetchModels]);

  // 映射参数（仅作为初始值，PaintView 内部维护自身参数态）
  const initialParams = useMemo(() => mapParams(paintRequest), [paintRequest]);

  // 手动模式可选模型：仅列出本地就绪的绘画模型
  const modelOptions = useMemo(
    () => models.filter((m) => m.status === 'ready').map((m) => m.id),
    [models],
  );

  // 映射图片列表
  const images = useMemo(() => results.map(mapResult), [results]);

  // 生成回调：UI 编辑值 + 手动选定模型 → 组装请求 → store
  const handleGenerate = useCallback(
    (params: PaintParamsValue, model?: string) => {
      const req: PaintRequest = {
        prompt: params.prompt,
        negative_prompt: params.negativePrompt,
        width: params.width,
        height: params.height,
        steps: params.steps,
        cfg_scale: params.guidanceScale,
        seed: params.seed,
        batch_size: params.batchSize,
        sampler: params.sampler,
        loras: (params.loras ?? [])
          .filter((l) => l.enabled)
          .map((l) => ({ name: l.name, weight: l.weight })),
        ...(model ? { model } : {}),
      };
      generate(req);
    },
    [generate],
  );

  const handleDownload = useCallback((image: GeneratedImage) => {
    // 默认行为：浏览器直接下载
    const a = document.createElement('a');
    a.href = image.url;
    a.download = `omnispace_${image.id}.png`;
    a.target = '_blank';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }, []);

  return (
    <PaintView
      initialParams={initialParams}
      modelOptions={modelOptions}
      images={images}
      generating={generating}
      progress={
        generating && typeof taskProgress === 'number'
          ? Math.round(taskProgress * 100)
          : generating
            ? 0
            : undefined
      }
      onGenerate={handleGenerate}
      onDownload={handleDownload}
    />
  );
}
