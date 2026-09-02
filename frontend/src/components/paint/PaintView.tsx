/**
 * PaintView 绘画主视图
 * OmniSpace AI v2.1 — Sakura 主题；2026-08-20 三模式扩展
 * --------------------------------------------------------------------------
 * 三种生成模式（顶部分段控件）：
 *   1. 文字生成图片     —— 现有文生图管线（/draw/generate）
 *   2. 图片生成视频     —— 选历史图/上传 → 视频（/video/generate，I2V）
 *   3. 文字+图片生成视频 —— 提示词 + 图 → 视频（文+图驱动）
 * 布局：左侧结果区（图片画廊 / 视频卡片）+ 右侧参数面板（随模式切换）。
 * 组件通过 props 注入数据与回调，与 stores/services 解耦（受控模式）。
 * 规格 §9.3 绘画模块。
 */
import { useCallback, useState } from 'react';
import { Sparkles, Palette, ImageIcon, Clapperboard, Layers } from 'lucide-react';
import { Button } from '../common/Button';
import { PaintParams } from './PaintParams';
import type { PaintParamsValue } from './PaintParams';
import { ImageGrid } from './ImageGrid';
import type { GeneratedImage } from './ImageGrid';
import { VideoPanel, VideoResults, DEFAULT_VIDEO_PARAMS } from './VideoPanel';
import type { VideoParamsValue } from './VideoPanel';
import type { PaintVideoTask } from '@/stores/usePaintStore';
import {
  loadPaintModelPref,
  savePaintModelPref,
} from '@/components/model/ModuleModelConfig';

/** 生成模式 */
type PaintMode = 'image' | 'i2v' | 'ti2v';

const MODE_TABS: { key: PaintMode; label: string; icon: typeof ImageIcon }[] = [
  { key: 'image', label: '文字生成图片', icon: ImageIcon },
  { key: 'i2v', label: '图片生成视频', icon: Clapperboard },
  { key: 'ti2v', label: '文字+图片生成视频', icon: Layers },
];

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
  /** 手动模式下可选模型列表 */
  modelOptions?: string[];
  /** 已生成的图片列表 */
  images?: GeneratedImage[];
  /** 是否正在生成图片 */
  generating?: boolean;
  /** 生成进度 0~100 */
  progress?: number;
  /** 图片生成回调（model 为手动模式选定的模型 id） */
  onGenerate: (params: PaintParamsValue, model?: string) => void;
  /** 视频生成回调（variant: i2v 纯图 / ti2v 文+图） */
  onGenerateVideo?: (
    variant: 'i2v' | 'ti2v',
    params: VideoParamsValue,
  ) => void;
  /** 视频任务列表 */
  videoTasks?: PaintVideoTask[];
  /** 视频是否生成中 */
  videoGenerating?: boolean;
  /** 移除视频任务记录 */
  onRemoveVideoTask?: (taskId: string) => void;
  /** 下载回调 */
  onDownload?: (image: GeneratedImage) => void;
  /** 收藏切换回调 */
  onToggleFavorite?: (image: GeneratedImage) => void;
  /** 删除单张回调 */
  onDelete?: (image: GeneratedImage) => void | Promise<void>;
  /** 批量删除回调 */
  onBatchDelete?: (ids: string[]) => void | Promise<void>;
}

export function PaintView({
  initialParams,
  modelOptions,
  images = [],
  generating = false,
  progress,
  onGenerate,
  onGenerateVideo,
  videoTasks = [],
  videoGenerating = false,
  onRemoveVideoTask,
  onDownload,
  onToggleFavorite,
  onDelete,
  onBatchDelete,
}: PaintViewProps) {
  const [params, setParams] = useState<PaintParamsValue>(initialParams || DEFAULT_PARAMS);
  // 模型偏好初值来自共享 localStorage（模型管理页「功能模块模型配置」写入；
  // 此处切换时写回，两处双向同步——2026-08-23）
  const [modelMode, setModelMode] = useState<'auto' | 'manual'>(() => loadPaintModelPref().mode);
  const [selectedModel, setSelectedModel] = useState<string>(() => loadPaintModelPref().modelId);
  const [mode, setMode] = useState<PaintMode>('image');
  const [videoParams, setVideoParams] = useState<VideoParamsValue>(DEFAULT_VIDEO_PARAMS);

  /** 切换路由模式：同步写回共享偏好（模型管理页同源读取） */
  const handleModelModeChange = useCallback((next: 'auto' | 'manual') => {
    setModelMode(next);
    savePaintModelPref({ mode: next, modelId: next === 'manual' ? loadPaintModelPref().modelId : '' });
  }, []);

  /** 切换指定模型：同步写回共享偏好 */
  const handleModelChange = useCallback((modelId: string) => {
    setSelectedModel(modelId);
    if (modelId) savePaintModelPref({ mode: 'manual', modelId });
  }, []);

  function handleGenerate() {
    if (!params.prompt.trim()) return;
    onGenerate(
      params,
      modelMode === 'manual' && selectedModel ? selectedModel : undefined,
    );
  }

  const isVideoMode = mode !== 'image';

  return (
    <div className="flex h-full bg-[var(--color-bg)]">
      {/* ============ 左侧：结果展示区（约 60%） ============ */}
      <div className="flex-[3] min-w-0 overflow-y-auto p-6">
        <div className="max-w-4xl mx-auto">
          {isVideoMode ? (
            <VideoResults tasks={videoTasks} onRemove={onRemoveVideoTask ?? (() => {})} />
          ) : (
            <ImageGrid
              images={images}
              loading={false}
              progress={generating ? progress : undefined}
              onDownload={onDownload}
              onToggleFavorite={onToggleFavorite}
              onDelete={onDelete}
              onBatchDelete={onBatchDelete}
            />
          )}
        </div>
      </div>

      {/* ============ 右侧：参数面板（约 40%，min-width 保护） ============ */}
      <div className="flex-[2] min-w-80 shrink-0 border-l border-[var(--color-border-light)] bg-[var(--color-card)] flex flex-col">
        <div className="p-4 border-b border-[var(--color-divider)] shrink-0">
          <h2 className="text-base font-semibold text-[var(--color-text-primary)] inline-flex items-center gap-2"><Palette size={18} aria-hidden="true" className="text-[var(--color-primary)]" /> AI 绘画</h2>
          <p className="text-xs text-[var(--color-text-tertiary)] mt-0.5">FLUX.2 Klein · Qwen-Image 本地推理</p>

          {/* 三模式分段控件 */}
          <div className="flex gap-1 mt-3 p-1 rounded-lg bg-[var(--color-input-bg)]" role="tablist">
            {MODE_TABS.map(({ key, label, icon: Icon }) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={mode === key}
                onClick={() => setMode(key)}
                className={[
                  'flex-1 h-8 px-1 rounded-md text-xs font-medium transition-colors inline-flex items-center justify-center gap-1',
                  mode === key
                    ? 'bg-sakura-500 text-white shadow-sm'
                    : 'text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]',
                ].join(' ')}
                title={label}
              >
                <Icon size={13} aria-hidden="true" className="shrink-0" />
                <span className="hidden xl:inline">{label}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {mode === 'image' ? (
            <PaintParams
              value={params}
              onChange={setParams}
              modelMode={modelMode}
              onModelModeChange={handleModelModeChange}
              modelOptions={modelOptions}
              selectedModel={selectedModel}
              onModelChange={handleModelChange}
              disabled={generating}
            />
          ) : (
            <VideoPanel
              variant={mode}
              value={videoParams}
              onChange={setVideoParams}
              images={images}
              generating={videoGenerating}
              onGenerate={(v) => onGenerateVideo?.(mode, v)}
            />
          )}
        </div>

        {/* 生成按钮（固定底部，仅文生图模式；视频模式按钮在面板内） */}
        {mode === 'image' ? (
          <div className="p-4 border-t border-[var(--color-divider)] shrink-0">
            <Button
              onClick={handleGenerate}
              loading={generating}
              disabled={!params.prompt.trim()}
              className="w-full"
              size="lg"
            >
              {generating ? (
                '生成中…'
              ) : (
                <>
                  <Sparkles className="w-4 h-4" aria-hidden="true" />
                  生成图像
                </>
              )}
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default PaintView;
