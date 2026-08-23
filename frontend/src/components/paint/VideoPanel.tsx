/**
 * VideoPanel 绘画视频生成面板 + VideoResults 结果列表
 * OmniSpace AI v2.3 — 2026-08-20 绘画模块三模式扩展
 * --------------------------------------------------------------------------
 * 模式（PaintView 顶部分段控件）：
 *   - i2v  图片生成视频：选历史图/上传 → 视频
 *   - ti2v 文字+图片生成视频：提示词 + 选图 → 视频
 * （纯文生视频 t2v 由 ti2v 不选图达成；引擎 screenshot_4in1 空 = T2V）
 * 输入图来源：本页历史缩略条点选 或 本地上传；生成走 /video/generate
 * 复用漫剧视频管线（真实模型探测链 → AnimateLCM → Ken Burns 降级）。
 */
import { useEffect, useRef, useState } from 'react';
import { Clapperboard, ImagePlus, Upload, Trash2, Film, Maximize2, X } from 'lucide-react';
import { Button } from '../common/Button';
import type { PaintVideoTask } from '@/stores/usePaintStore';
import { paintVideoUrl } from '@/services/paintApi';
import type { GeneratedImage } from './ImageGrid';

export interface VideoParamsValue {
  prompt: string;
  durationSeconds: number;
  fps: number;
  resolution: string;
  /** 输入图源：http URL（历史图）或 dataURL（上传） */
  imageSource: string | null;
  /** 输入图预览缩略 URL（上传 dataURL 或历史 URL） */
  imagePreview: string | null;
}

const DEFAULT_VIDEO_PARAMS: VideoParamsValue = {
  prompt: '',
  durationSeconds: 5,
  fps: 16,
  resolution: '720p',
  imageSource: null,
  imagePreview: null,
};

export interface VideoPanelProps {
  /** i2v 纯图（选图必填，无提示词）/ ti2v 文+图（提示词与选图均必填） */
  variant: 'i2v' | 'ti2v';
  value: VideoParamsValue;
  onChange: (v: VideoParamsValue) => void;
  /** 历史图片（选图缩略条数据源） */
  images: GeneratedImage[];
  generating: boolean;
  onGenerate: (v: VideoParamsValue) => void;
}

export function VideoPanel({
  variant,
  value,
  onChange,
  images,
  generating,
  onGenerate,
}: VideoPanelProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [showPicker, setShowPicker] = useState(false);

  function pickHistory(img: GeneratedImage) {
    onChange({ ...value, imageSource: img.url, imagePreview: img.url });
    setShowPicker(false);
  }

  function pickUpload(file: File | undefined) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || '');
      onChange({ ...value, imageSource: dataUrl, imagePreview: dataUrl });
    };
    reader.readAsDataURL(file);
  }

  const needPrompt = variant === 'ti2v';
  const canGenerate =
    !generating &&
    (!needPrompt || value.prompt.trim().length > 0) &&
    !!value.imageSource;

  return (
    <div className="p-4 space-y-4">
      {/* 提示词（仅 ti2v：文+图驱动；i2v 纯图无提示词） */}
      {needPrompt ? (
        <div>
          <label className="block text-xs font-medium text-[var(--color-text-secondary)] mb-1.5">
            动作/运镜描述
          </label>
          <textarea
            value={value.prompt}
            onChange={(e) => onChange({ ...value, prompt: e.target.value })}
            placeholder="描述图片如何动起来，如：人物转身，镜头拉近，发丝飘动…"
            rows={3}
            disabled={generating}
            className="w-full px-3 py-2 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus:outline-none focus:ring-2 focus:ring-sakura-300 resize-none"
          />
        </div>
      ) : null}

      {/* 输入图选择（两种视频模式均必选） */}
      <div>
          <label className="block text-xs font-medium text-[var(--color-text-secondary)] mb-1.5">
            输入图片（必选）
          </label>
          {value.imagePreview ? (
            <div className="relative rounded-lg overflow-hidden border border-[var(--color-border-light)] group">
              <img
                src={value.imagePreview}
                alt="输入图预览"
                className="w-full max-h-48 object-contain bg-[var(--color-input-bg)]"
              />
              <button
                type="button"
                aria-label="移除输入图"
                title="移除输入图"
                disabled={generating}
                onClick={() =>
                  onChange({ ...value, imageSource: null, imagePreview: null })}
                className="absolute top-2 right-2 w-7 h-7 rounded-md bg-black/50 text-white flex items-center justify-center hover:bg-[var(--color-error)] transition-colors"
              >
                <Trash2 size={14} aria-hidden="true" />
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                disabled={generating || images.length === 0}
                onClick={() => setShowPicker((v) => !v)}
                className="h-14 rounded-lg border border-dashed border-[var(--color-input-border)] text-xs text-[var(--color-text-secondary)] hover:border-sakura-400 hover:text-sakura-600 transition-colors inline-flex items-center justify-center gap-1.5 disabled:opacity-40"
              >
                <ImagePlus size={14} aria-hidden="true" />
                从历史选择
              </button>
              <button
                type="button"
                disabled={generating}
                onClick={() => fileRef.current?.click()}
                className="h-14 rounded-lg border border-dashed border-[var(--color-input-border)] text-xs text-[var(--color-text-secondary)] hover:border-sakura-400 hover:text-sakura-600 transition-colors inline-flex items-center justify-center gap-1.5"
              >
                <Upload size={14} aria-hidden="true" />
                上传本地图片
              </button>
              <input
                ref={fileRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                className="hidden"
                onChange={(e) => {
                  pickUpload(e.target.files?.[0]);
                  e.target.value = '';
                }}
              />
            </div>
          )}
          {/* 历史图缩略选择条 */}
          {showPicker && !value.imagePreview ? (
            <div className="mt-2 p-2 rounded-lg border border-[var(--color-border-light)] bg-[var(--color-input-bg)] max-h-56 overflow-y-auto">
              {images.length === 0 ? (
                <p className="text-xs text-[var(--color-text-tertiary)] text-center py-3">
                  暂无历史图片，请先使用「文字生成图片」
                </p>
              ) : (
                <div className="grid grid-cols-4 gap-1.5">
                  {images.slice(0, 24).map((img) => (
                    <button
                      key={img.id}
                      type="button"
                      onClick={() => pickHistory(img)}
                      className="aspect-square rounded-md overflow-hidden border border-transparent hover:border-sakura-400 transition-colors"
                    >
                      <img
                        src={img.url}
                        alt={(img.prompt || '').slice(0, 20)}
                        loading="lazy"
                        className="w-full h-full object-cover"
                      />
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : null}
      </div>

      {/* 时长 */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-xs font-medium text-[var(--color-text-secondary)]">
            视频时长
          </label>
          <span className="text-xs text-[var(--color-text-tertiary)]">
            {value.durationSeconds}s
          </span>
        </div>
        <input
          type="range"
          min={1}
          max={20}
          step={1}
          value={value.durationSeconds}
          disabled={generating}
          onChange={(e) =>
            onChange({ ...value, durationSeconds: Number(e.target.value) })}
          className="w-full accent-[var(--color-primary)]"
        />
      </div>

      {/* 帧率 + 分辨率 */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium text-[var(--color-text-secondary)] mb-1.5">
            帧率
          </label>
          <select
            value={value.fps}
            disabled={generating}
            onChange={(e) => onChange({ ...value, fps: Number(e.target.value) })}
            className="w-full h-9 px-2 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] focus:outline-none focus:ring-2 focus:ring-sakura-300"
          >
            {[8, 12, 16, 24, 30].map((f) => (
              <option key={f} value={f}>{f} fps</option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-[var(--color-text-secondary)] mb-1.5">
            分辨率
          </label>
          <select
            value={value.resolution}
            disabled={generating}
            onChange={(e) => onChange({ ...value, resolution: e.target.value })}
            className="w-full h-9 px-2 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] focus:outline-none focus:ring-2 focus:ring-sakura-300"
          >
            {['720p', '1080p', '2k', '4k'].map((r) => (
              <option key={r} value={r}>
                {r === '2k' ? '2K' : r === '4k' ? '4K' : r}
              </option>
            ))}
          </select>
        </div>
      </div>

      <Button
        onClick={() => onGenerate(value)}
        loading={generating}
        disabled={!canGenerate}
        className="w-full"
        size="lg"
      >
        {generating ? (
          '生成中…'
        ) : (
          <>
            <Clapperboard className="w-4 h-4" aria-hidden="true" />
            生成视频
          </>
        )}
      </Button>
    </div>
  );
}

/* ══════════════════════ 视频结果列表 ══════════════════════ */

export interface VideoResultsProps {
  tasks: PaintVideoTask[];
  onRemove: (taskId: string) => void;
}

/** ETA 秒数 → 可读文案（≤90s 直显秒；>90s 显示 X 分 X 秒） */
function formatEta(seconds: number): string {
  const s = Math.max(1, Math.ceil(seconds));
  if (s <= 90) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  return rest > 0 ? `${m} 分 ${rest} 秒` : `${m} 分钟`;
}

/** 视频结果卡片列表（新→旧；生成中显示进度，完成显示播放器） */
export function VideoResults({ tasks, onRemove }: VideoResultsProps) {
  /** 放大预览中的任务（null = 关闭）；大播放器弹层 2026-08-20 */
  const [previewTask, setPreviewTask] = useState<PaintVideoTask | null>(null);

  // ESC 关闭放大预览
  useEffect(() => {
    if (!previewTask) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPreviewTask(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [previewTask]);

  if (tasks.length === 0) {
    return (
      <div className="text-center py-16 text-[var(--color-text-tertiary)]">
        <div className="flex justify-center mb-3 text-[var(--color-primary)]">
          <Film size={36} strokeWidth={1.5} aria-hidden="true" />
        </div>
        <div className="text-base">还没有视频</div>
        <div className="text-sm mt-1">
          在右侧选择「图片生成视频」或「文字+图片生成视频」开始创作
        </div>
      </div>
    );
  }
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {tasks.map((t) => (
        <div
          key={t.taskId}
          className="rounded-xl border border-[var(--color-border-light)] bg-[var(--color-card)] overflow-hidden"
        >
          {t.status === 'done' ? (
            <div className="relative group/video">
              <video
                src={paintVideoUrl(t.taskId)}
                controls
                preload="metadata"
                className="w-full aspect-video bg-black"
              />
              {/* 放大预览按钮（hover 浮现；右上角避开原生控件） */}
              <button
                type="button"
                aria-label="放大预览"
                title="放大预览"
                onClick={() => setPreviewTask(t)}
                className="absolute top-2 right-2 w-8 h-8 rounded-md bg-black/60 text-white/90 hover:bg-black/80 hover:text-white flex items-center justify-center opacity-0 group-hover/video:opacity-100 transition-opacity"
              >
                <Maximize2 size={15} aria-hidden="true" />
              </button>
            </div>
          ) : (
            <div className="w-full aspect-video bg-[var(--color-input-bg)] flex flex-col items-center justify-center gap-2">
              {t.status === 'generating' ? (
                <>
                  <span className="inline-block w-6 h-6 border-2 border-sakura-300 border-t-transparent rounded-full animate-spin" />
                  <span className="text-xs text-[var(--color-text-tertiary)]">
                    生成中 {t.progress}%
                  </span>
                  <div className="w-2/3 h-1.5 rounded-full bg-[var(--color-divider)] overflow-hidden">
                    <div
                      className="h-full bg-sakura-500 transition-all"
                      style={{ width: `${Math.max(3, t.progress)}%` }}
                    />
                  </div>
                  {/* 预计剩余时间（后端 step 采样外推；引擎未采样时隐藏） */}
                  {t.etaSeconds != null ? (
                    <span className="text-xs text-[var(--color-text-tertiary)]">
                      预计剩余 {formatEta(t.etaSeconds)}
                    </span>
                  ) : null}
                </>
              ) : (
                <span className="text-xs text-[var(--color-error)] px-4 text-center">
                  {t.error || '生成失败'}
                </span>
              )}
            </div>
          )}
          <div className="p-3 flex items-start gap-2">
            <div className="flex-1 min-w-0">
              <p className="text-xs text-[var(--color-text-secondary)] truncate" title={t.prompt}>
                {t.prompt || '（无描述）'}
              </p>
              <p className="text-xs text-[var(--color-text-tertiary)] mt-0.5">
                {t.mode === 'i2v' ? '图生视频' : '文+图生视频'}
                {' · '}
                {t.durationSeconds}s · {t.fps}fps · {t.resolution}
                {t.degraded ? ' · 降级管线' : ''}
              </p>
            </div>
            <button
              type="button"
              aria-label="删除视频记录"
              title="删除视频记录"
              onClick={() => onRemove(t.taskId)}
              className="w-7 h-7 rounded-md text-[var(--color-text-tertiary)] hover:text-[var(--color-error)] hover:bg-[var(--color-error-bg)] flex items-center justify-center shrink-0"
            >
              <Trash2 size={14} aria-hidden="true" />
            </button>
          </div>
        </div>
      ))}

      {/* 放大预览弹层（点背景/ESC/右上角 X 关闭；自动播放大播放器） */}
      {previewTask ? (
        <div
          className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center p-4 md:p-10"
          role="dialog"
          aria-modal="true"
          aria-label="视频放大预览"
          onClick={() => setPreviewTask(null)}
        >
          <div
            className="relative w-full max-w-5xl"
            onClick={(e) => e.stopPropagation()}
          >
            <video
              src={paintVideoUrl(previewTask.taskId)}
              controls
              autoPlay
              className="w-full max-h-[80vh] bg-black rounded-lg shadow-2xl"
            />
            {/* 信息条 + 关闭按钮 */}
            <div className="mt-3 flex items-center gap-3 text-white/80">
              <p className="flex-1 min-w-0 text-sm truncate">
                {previewTask.prompt || '（无描述）'}
                <span className="ml-2 text-xs text-white/50">
                  {previewTask.mode === 'i2v' ? '图生视频' : '文+图生视频'}
                  {' · '}
                  {previewTask.durationSeconds}s · {previewTask.fps}fps
                  {' · '}
                  {previewTask.resolution}
                  {previewTask.degraded ? ' · 降级管线' : ''}
                </span>
              </p>
              <button
                type="button"
                aria-label="关闭预览"
                onClick={() => setPreviewTask(null)}
                className="w-9 h-9 rounded-full bg-white/10 hover:bg-white/20 text-white flex items-center justify-center shrink-0 transition-colors"
              >
                <X size={18} aria-hidden="true" />
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export { DEFAULT_VIDEO_PARAMS };
export type { VideoParamsValue as PaintVideoParamsValue };
