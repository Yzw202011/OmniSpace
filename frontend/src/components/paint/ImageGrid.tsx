/**
 * ImageGrid 图片网格
 * OmniSpace AI v2.1 — Sakura 主题
 * --------------------------------------------------------------------------
 * 展示生成的图片，支持点击放大（弹窗查看大图）与下载。
 * 删除：悬浮操作栏单删 / 大图弹窗删除 / 批量选择模式（全选·批量删除）。
 * 规格 §9.3 绘画模块。
 */
import { useState } from 'react';
import { Download, Heart, ImagePlus, Trash2, CheckSquare, Square, X } from 'lucide-react';
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
  /** 创建时间（毫秒 epoch；近 NEW_WINDOW_MS 内的卡片显示「新」角标） */
  created_at?: number;
}

/** 「新」角标展示窗口（ms）：仅最近生成的图标记，历史图不标 */
const NEW_WINDOW_MS = 120_000;

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
  /** 删除单张回调（不传则不显示删除入口） */
  onDelete?: (image: GeneratedImage) => void | Promise<void>;
  /** 批量删除回调（不传则不显示批量管理入口） */
  onBatchDelete?: (ids: string[]) => void | Promise<void>;
}

export function ImageGrid({
  images,
  loading = false,
  progress,
  onDownload,
  onToggleFavorite,
  onDelete,
  onBatchDelete,
}: ImageGridProps) {
  const [viewer, setViewer] = useState<GeneratedImage | null>(null);
  /** 批量选择模式（仅 onBatchDelete 提供时可用） */
  const [selectMode, setSelectMode] = useState(false);
  /** 已选中的图片 ID 集 */
  const [selected, setSelected] = useState<Set<string>>(new Set());
  /** 批量删除进行中 */
  const [batchDeleting, setBatchDeleting] = useState(false);

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

  function handleDelete(image: GeneratedImage) {
    if (!window.confirm('确定删除这张图片吗？删除后不可恢复。')) return;
    setViewer(null);
    void onDelete?.(image);
  }

  function toggleSelected(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function exitSelectMode() {
    setSelectMode(false);
    setSelected(new Set());
  }

  function toggleSelectAll() {
    setSelected((prev) =>
      prev.size === images.length
        ? new Set()
        : new Set(images.map((i) => i.id)),
    );
  }

  async function handleBatchDelete() {
    const ids = Array.from(selected);
    if (!ids.length) return;
    if (!window.confirm(`确定删除选中的 ${ids.length} 张图片吗？删除后不可恢复。`)) return;
    setBatchDeleting(true);
    try {
      await onBatchDelete?.(ids);
      exitSelectMode();
    } finally {
      setBatchDeleting(false);
    }
  }

  const canBatch = typeof onBatchDelete === 'function' && images.length > 0;

  return (
    <>
      {/* 批量管理工具栏（有图且支持批量删除时显示） */}
      {canBatch ? (
        <div className="flex items-center gap-2 mb-3 min-h-9">
          {selectMode ? (
            <>
              <button
                type="button"
                onClick={toggleSelectAll}
                className="inline-flex items-center gap-1.5 h-9 px-3 rounded-lg bg-[var(--color-divider)] text-[var(--color-text-secondary)] text-sm font-medium hover:bg-[var(--color-border-light)] transition-colors"
              >
                {selected.size === images.length && images.length > 0 ? (
                  <CheckSquare className="w-4 h-4 text-sakura-500" aria-hidden="true" />
                ) : (
                  <Square className="w-4 h-4" aria-hidden="true" />
                )}
                {selected.size === images.length && images.length > 0 ? '取消全选' : '全选'}
              </button>
              <button
                type="button"
                onClick={handleBatchDelete}
                disabled={selected.size === 0 || batchDeleting}
                className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg bg-[var(--color-error)] text-white text-sm font-medium hover:opacity-90 transition-opacity disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <Trash2 className="w-4 h-4" aria-hidden="true" />
                {batchDeleting ? '删除中…' : `删除选中（${selected.size}）`}
              </button>
              <span className="text-xs text-[var(--color-text-tertiary)]">已选 {selected.size} / {images.length} 张</span>
              <button
                type="button"
                onClick={exitSelectMode}
                aria-label="退出批量管理"
                className="ml-auto w-9 h-9 inline-flex items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-divider)] hover:text-[var(--color-text-primary)] transition-colors"
              >
                <X className="w-4 h-4" aria-hidden="true" />
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setSelectMode(true)}
              className="inline-flex items-center gap-1.5 h-9 px-3 rounded-lg bg-[var(--color-divider)] text-[var(--color-text-secondary)] text-sm font-medium hover:bg-[var(--color-border-light)] hover:text-[var(--color-text-primary)] transition-colors"
            >
              <CheckSquare className="w-4 h-4" aria-hidden="true" />
              批量管理
            </button>
          )}
        </div>
      ) : null}

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
            images.map((img) => {
            const isSelected = selected.has(img.id);
            const isNew =
              !selectMode &&
              typeof img.created_at === 'number' &&
              Date.now() - img.created_at < NEW_WINDOW_MS;
            return (
              <div
                key={img.id}
                className="group relative aspect-square rounded-xl overflow-hidden bg-[var(--color-divider)] cursor-pointer"
                onClick={() => (selectMode ? toggleSelected(img.id) : setViewer(img))}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    if (selectMode) {
                      toggleSelected(img.id);
                    } else {
                      setViewer(img);
                    }
                  }
                }}
                aria-label={
                  selectMode
                    ? `选择图片${img.prompt ? '：' + img.prompt : ''}`
                    : `查看图片${img.prompt ? '：' + img.prompt : ''}`
                }
              >
                <img
                  src={`${img.url}${img.url.includes('?') ? '&' : '?'}thumb=1`}
                  alt={img.prompt || '生成图片'}
                  loading="lazy"
                  decoding="async"
                  className={`w-full h-full object-cover transition-transform duration-300 ${
                    selectMode ? '' : 'group-hover:scale-105'
                  } ${selectMode && !isSelected ? 'opacity-50' : ''} ${isNew ? 'ring-2 ring-sakura-500' : ''}`}
                />
                {/* 新生成角标（2 分钟内，选择模式下隐藏） */}
                {isNew ? (
                  <span
                    className="absolute top-2 right-2 px-1.5 py-0.5 rounded-md bg-sakura-500 text-white text-[10px] font-bold shadow-sm pointer-events-none"
                    aria-hidden="true"
                  >
                    新
                  </span>
                ) : null}
                {/* 选择模式：选中角标 */}
                {selectMode ? (
                  <span
                    className={`absolute top-2 left-2 w-6 h-6 rounded-md flex items-center justify-center transition-colors ${
                      isSelected
                        ? 'bg-sakura-500 text-white'
                        : 'bg-black/40 text-white/80 backdrop-blur-sm'
                    }`}
                    aria-hidden="true"
                  >
                    {isSelected ? <CheckSquare className="w-4 h-4" /> : <Square className="w-4 h-4" />}
                  </span>
                ) : null}
                {/* 悬浮操作栏（选择模式下隐藏） */}
                {!selectMode ? (
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
                    {onDelete ? (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDelete(img);
                        }}
                        aria-label="删除图片"
                        title="删除图片"
                        className="w-7 h-7 rounded-md bg-[var(--color-card)]/90 text-[var(--color-text-secondary)] hover:bg-[var(--color-error)] hover:text-white flex items-center justify-center transition-colors"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    ) : null}
                  </div>
                ) : null}
                {/* 收藏标记 */}
                {img.favorite && !selectMode ? (
                  <Heart className="absolute top-1.5 right-1.5 w-4 h-4 text-sakura-500 fill-current drop-shadow" aria-hidden="true" />
                ) : null}
              </div>
            );
          })}

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
              {onDelete ? (
                <button
                  type="button"
                  onClick={() => handleDelete(viewer)}
                  className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg bg-[var(--color-error)] text-white text-sm font-medium hover:opacity-90 transition-opacity"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  删除
                </button>
              ) : null}
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
