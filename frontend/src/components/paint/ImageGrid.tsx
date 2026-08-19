/**
 * ImageGrid 图片网格
 * OmniSpace AI v2.1 — Sakura 主题
 * --------------------------------------------------------------------------
 * 展示生成的图片，支持点击放大（弹窗查看大图）与下载。
 * 规格 §9.3 绘画模块。
 */
import { useState } from 'react';
import { Download, Heart, ImagePlus } from 'lucide-react';
import { Modal } from '../common/Modal';

/** 生成图片项 */
export interface GeneratedImage {
  /** 图片 ID */
  id: string;
  /** 图片地址 */
  url: string;
  /** 生成时的提示词 */
  prompt?: string;
  /** 宽度 */
  width?: number;
  /** 高度 */
  height?: number;
  /** 种子 */
  seed?: number;
  /** 是否收藏 */
  favorite?: boolean;
}

export interface ImageGridProps {
  /** 图片列表 */
  images: GeneratedImage[];
  /** 是否加载中（显示骨架占位） */
  loading?: boolean;
  /** 生成进度（0~100），有值时显示生成中占位 */
  progress?: number;
  /** 下载回调（不传则使用默认行为下载 url） */
  onDownload?: (image: GeneratedImage) => void;
  /** 收藏切换回调 */
  onToggleFavorite?: (image: GeneratedImage) => void;
}

export function ImageGrid({
  images,
  loading = false,
  progress,
  onDownload,
  onToggleFavorite,
}: ImageGridProps) {
  const [viewer, setViewer] = useState<GeneratedImage | null>(null);

  function handleDownload(image: GeneratedImage) {
    if (onDownload) {
      onDownload(image);
      return;
    }
    // 默认行为：触发下载
    const a = document.createElement('a');
    a.href = image.url;
    a.download = `omnispace-${image.id}.png`;
    a.target = '_blank';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  return (
    <>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {/* 加载骨架 */}
        {loading
          ? Array.from({ length: 4 }).map((_, i) => (
              <div key={`sk-${i}`} className="aspect-square rounded-xl bg-[var(--color-divider)] animate-pulse" />
            ))
          : null}

        {/* 生成中占位 */}
        {!loading && typeof progress === 'number' ? (
          <div className="aspect-square rounded-xl border-2 border-dashed border-sakura-300 bg-sakura-50 flex flex-col items-center justify-center text-sakura-600">
            <span className="inline-block w-6 h-6 border-2 border-sakura-400 border-t-transparent rounded-full animate-spin mb-2" />
            <span className="text-xs">生成中…{Math.round(progress)}%</span>
          </div>
        ) : null}

        {/* 图片项 */}
        {!loading &&
          images.map((img) => (
            <div
              key={img.id}
              className="group relative aspect-square rounded-xl overflow-hidden bg-[var(--color-divider)] cursor-pointer"
              onClick={() => setViewer(img)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  setViewer(img);
                }
              }}
              aria-label={`查看图片${img.prompt ? '：' + img.prompt : ''}`}
            >
              <img
                src={img.url}
                alt={img.prompt || '生成图片'}
                loading="lazy"
                className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105"
              />
              {/* 悬浮操作栏 */}
              <div className="absolute inset-x-0 bottom-0 p-2 bg-gradient-to-t from-black/50 to-transparent opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDownload(img);
                  }}
                  aria-label="下载图片"
                  className="w-7 h-7 rounded-md bg-[var(--color-card)]/90 text-[var(--color-text-secondary)] hover:bg-[var(--color-card)] hover:text-sakura-500 flex items-center justify-center"
                >
                  <Download className="w-3.5 h-3.5" />
                </button>
                {onToggleFavorite ? (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onToggleFavorite(img);
                    }}
                    aria-label={img.favorite ? '取消收藏' : '收藏'}
                    className={`w-7 h-7 rounded-md bg-[var(--color-card)]/90 hover:bg-[var(--color-card)] flex items-center justify-center ${
                      img.favorite ? 'text-sakura-500' : 'text-[var(--color-text-secondary)]'
                    }`}
                  >
                    <Heart className={`w-3.5 h-3.5 ${img.favorite ? 'fill-current' : ''}`} />
                  </button>
                ) : null}
              </div>
              {/* 收藏标记 */}
              {img.favorite ? (
                <Heart className="absolute top-1.5 right-1.5 w-4 h-4 text-sakura-500 fill-current drop-shadow" aria-hidden="true" />
              ) : null}
            </div>
          ))}

        {/* 空状态（引导至右侧参数面板） */}
        {!loading && images.length === 0 && typeof progress !== 'number' ? (
          <div className="col-span-full py-20 flex flex-col items-center text-center text-[var(--color-text-tertiary)]">
            <span className="flex items-center justify-center w-14 h-14 rounded-2xl bg-sakura-500/10 text-sakura-500 mb-4">
              <ImagePlus className="w-7 h-7" aria-hidden="true" />
            </span>
            <div className="text-sm font-medium text-[var(--color-text-secondary)]">还没有作品</div>
            <div className="text-xs mt-1.5 leading-relaxed">
              在右侧面板写下想象中的画面，调整参数后点击「生成图像」
            </div>
          </div>
        ) : null}
      </div>

      {/* 放大查看弹窗 */}
      {viewer ? (
        <Modal
          title="查看大图"
          width={720}
          onClose={() => setViewer(null)}
          footer={
            <>
              <button
                type="button"
                onClick={() => handleDownload(viewer)}
                className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg bg-sakura-500 text-white text-sm font-medium hover:bg-sakura-600 transition-colors"
              >
                <Download className="w-3.5 h-3.5" />
                下载
              </button>
              {onToggleFavorite ? (
                <button
                  type="button"
                  onClick={() => onToggleFavorite(viewer)}
                  className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg bg-sakura-100 text-sakura-700 text-sm font-medium hover:bg-sakura-200 transition-colors"
                >
                  <Heart className={`w-3.5 h-3.5 ${viewer.favorite ? 'fill-current' : ''}`} />
                  {viewer.favorite ? '取消收藏' : '收藏'}
                </button>
              ) : null}
              <button
                type="button"
                onClick={() => setViewer(null)}
                className="h-9 px-4 rounded-lg bg-[var(--color-divider)] text-[var(--color-text-secondary)] text-sm font-medium hover:bg-[var(--color-border-light)] transition-colors"
              >
                关闭
              </button>
            </>
          }
        >
          <div className="flex flex-col gap-3">
            <img
              src={viewer.url}
              alt={viewer.prompt || '大图'}
              className="w-full rounded-lg max-h-[60vh] object-contain bg-[var(--color-divider)]"
            />
            <div className="text-sm text-[var(--color-text-secondary)] space-y-1">
              {viewer.prompt ? (
                <div>
                  <span className="font-medium text-[var(--color-text-primary)]">提示词：</span>
                  {viewer.prompt}
                </div>
              ) : null}
              <div>
                <span className="font-medium text-[var(--color-text-primary)]">参数：</span>
                {viewer.width || '—'}×{viewer.height || '—'}
                {typeof viewer.seed === 'number' ? ` · seed ${viewer.seed}` : ''}
              </div>
            </div>
          </div>
        </Modal>
      ) : null}
    </>
  );
}

export default ImageGrid;
