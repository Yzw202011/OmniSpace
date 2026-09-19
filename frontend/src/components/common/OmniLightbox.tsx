// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * OmniLightbox.tsx —— 通用大图预览灯箱（批1 P2，2026-09-19）
 * --------------------------------------------------------------------------
 * 全屏遮罩点图关闭（Esc 同）；顶部工具条：下载 / 关闭；图片右击=下载。
 * 与右击下载配套的 downloadImage 助手一并导出（漫画/漫剧资产与分格图用）。
 * ========================================================================== */

import { useEffect } from 'react';
import { Download, X } from 'lucide-react';

/** 触发浏览器下载（同源 URL / data: 均可） */
export function downloadImage(src: string, filename: string): void {
  const a = document.createElement('a');
  a.href = src;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** 从 URL 推一个像样的文件名（取最后一段，去参数；失败回退 image.png） */
export function guessFilename(src: string, fallbackPrefix = 'image'): string {
  try {
    const clean = src.split('?')[0].split('#')[0];
    const seg = clean.substring(clean.lastIndexOf('/') + 1);
    return seg || `${fallbackPrefix}.png`;
  } catch {
    return `${fallbackPrefix}.png`;
  }
}

export interface OmniLightboxProps {
  src: string;
  /** 无障碍/工具条标题 */
  title?: string;
  onClose: () => void;
}

export default function OmniLightbox({ src, title, onClose }: OmniLightboxProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const filename = guessFilename(src, title || 'image');

  return (
    <div
      role="dialog"
      aria-label={title ? `${title}·大图预览` : '大图预览'}
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 2000,
        background: 'rgba(0,0,0,0.78)',
        display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center',
        cursor: 'zoom-out', padding: 'var(--space-4)',
      }}
    >
      <div
        className="flex items-center justify-between"
        style={{ width: 'min(92vw, 960px)', marginBottom: 'var(--space-2)' }}
        onClick={(e) => e.stopPropagation()}
      >
        <span className="text-sm" style={{ color: 'rgba(255,255,255,0.85)' }}>
          {title ?? ''}（右击图片可下载）
        </span>
        <span className="flex items-center gap-2">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-label="下载图片"
            onClick={() => downloadImage(src, filename)}
          >
            <Download size={14} aria-hidden="true" /> 下载
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-label="关闭预览"
            onClick={onClose}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </span>
      </div>
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <img
        src={src}
        alt={title ?? '预览大图'}
        onClick={(e) => e.stopPropagation()}
        onContextMenu={(e) => {
          e.preventDefault();
          downloadImage(src, filename);
        }}
        style={{
          maxWidth: '92vw', maxHeight: '84vh',
          objectFit: 'contain',
          borderRadius: 'var(--radius-lg)',
          boxShadow: '0 12px 48px rgba(0,0,0,0.6)',
          cursor: 'context-menu',
        }}
      />
    </div>
  );
}
