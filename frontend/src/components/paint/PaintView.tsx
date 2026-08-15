/**
 * PaintView 绘画主视图
 * OmniSpace AI v2.1 — Sakura 主题
 * --------------------------------------------------------------------------
 * 左侧图片展示区（约 60%）+ 右侧参数面板（约 40%，提示词、负向提示词、
 * 尺寸、步数、采样器、引导强度、种子、批量、ControlNet（未就绪禁用）、LoRA）；
 * 模型选择（自动 / 手动）。布局对齐文档B §6.2.2。
 * 组件通过 props 注入数据与回调，与 stores/services 解耦（受控模式）。
 * 规格 §9.3 绘画模块。
 */
import { useState } from 'react';
import { Button } from '../common/Button';
import { PaintParams } from './PaintParams';
import type { PaintParamsValue } from './PaintParams';
import { ImageGrid } from './ImageGrid';
import type { GeneratedImage } from './ImageGrid';

/** 默认绘画参数 */
const DEFAULT_PARAMS: PaintParamsValue = {
  prompt: '',
  negativePrompt: '低质量，模糊，畸形，多肢',
  width: 1024,
  height: 1024,
  steps: 20,
  guidanceScale: 7.5,
  seed: -1,
  batchSize: 1,
  sampler: 'dpm++_2m_karras',
  controlnet: null,
  loras: [],
};

export interface PaintViewProps {
  /** 初始参数（不传则使用默认） */
  initialParams?: PaintParamsValue;
  /** 可选 ControlNet 模型列表 */
  controlnetModels?: string[];
  /** 手动模式下可选模型列表 */
  modelOptions?: string[];
  /** 已生成的图片列表 */
  images?: GeneratedImage[];
  /** 是否正在生成 */
  generating?: boolean;
  /** 生成进度 0~100 */
  progress?: number;
  /** 生成回调（model 为手动模式选定的模型 id，自动模式为 undefined） */
  onGenerate: (params: PaintParamsValue, model?: string) => void;
  /** 下载回调 */
  onDownload?: (image: GeneratedImage) => void;
  /** 收藏切换回调 */
  onToggleFavorite?: (image: GeneratedImage) => void;
}

export function PaintView({
  initialParams,
  modelOptions,
  images = [],
  generating = false,
  progress,
  onGenerate,
  onDownload,
  onToggleFavorite,
}: PaintViewProps) {
  const [params, setParams] = useState<PaintParamsValue>(initialParams || DEFAULT_PARAMS);
  const [modelMode, setModelMode] = useState<'auto' | 'manual'>('auto');
  const [selectedModel, setSelectedModel] = useState<string>('');

  function handleGenerate() {
    if (!params.prompt.trim()) return;
    // 手动模式且已选模型时透传模型 id；自动模式由后端调度
    onGenerate(
      params,
      modelMode === 'manual' && selectedModel ? selectedModel : undefined,
    );
  }

  return (
    <div className="flex h-full bg-[var(--color-bg)]">
      {/* ============ 左侧：图片展示区（约 60%，文档B §6.2.2） ============ */}
      <div className="flex-[3] min-w-0 overflow-y-auto p-6">
        <div className="max-w-4xl mx-auto">
          <ImageGrid
            images={images}
            loading={false}
            progress={generating ? progress : undefined}
            onDownload={onDownload}
            onToggleFavorite={onToggleFavorite}
          />
        </div>
      </div>

      {/* ============ 右侧：参数面板（约 40%，min-width 保护） ============ */}
      <div className="flex-[2] min-w-80 shrink-0 border-l border-[var(--color-border-light)] bg-[var(--color-card)] flex flex-col">
        <div className="p-4 border-b border-[var(--color-divider)] shrink-0">
          <h2 className="text-base font-semibold text-[var(--color-text-primary)]">🎨 AI 绘画</h2>
          <p className="text-xs text-[var(--color-text-tertiary)] mt-0.5">SDXL + LCM-LoRA 本地推理</p>
        </div>

        <div className="flex-1 overflow-y-auto">
          <PaintParams
            value={params}
            onChange={setParams}
            modelMode={modelMode}
            onModelModeChange={setModelMode}
            modelOptions={modelOptions}
            selectedModel={selectedModel}
            onModelChange={setSelectedModel}
            disabled={generating}
          />
        </div>

        {/* 生成按钮（固定底部） */}
        <div className="p-4 border-t border-[var(--color-divider)] shrink-0">
          <Button
            onClick={handleGenerate}
            loading={generating}
            disabled={!params.prompt.trim()}
            className="w-full"
            size="lg"
          >
            {generating ? '生成中…' : '🌸 生成'}
          </Button>
        </div>
      </div>
    </div>
  );
}

export default PaintView;
