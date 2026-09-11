/* ==========================================================================
 * AssetLightbox.tsx —— 漫剧资产图片放大灯箱（复用于资产详情/生成记录）
 * --------------------------------------------------------------------------
 * 居中展示大图 + 底部多视图缩略图条；红 X / ESC / 点背景关闭。
 * 支持单图（src）或多视图（srcs + activeIndex）。
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { X } from 'lucide-react';

export interface AssetLightboxProps {
  /** 单图或多视图 URL 列表 */
  srcs: string[];
  /** 初始展示索引（默认 0） */
  initialIndex?: number;
  /** 关闭回调 */
  onClose: () => void;
}

export default function AssetLightbox({ srcs, initialIndex = 0, onClose }: AssetLightboxProps) {
  const [idx, setIdx] = useState(initialIndex);

  useEffect(() => {
    setIdx(initialIndex);
  }, [initialIndex]);

  // ESC 关闭
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  if (!srcs.length) return null;

  return (
    <div
      className="manga-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
      onClick={onClose}
    >
      <button
        type="button"
        className="manga-lightbox-close"
        aria-label="关闭预览"
        onClick={onClose}
      >
        <X size={20} />
      </button>

      <div className="manga-lightbox-imgwrap" onClick={(e) => e.stopPropagation()}>
        <img src={srcs[idx]} alt={`预览 ${idx + 1}/${srcs.length}`} />
      </div>

      {srcs.length > 1 && (
        <div className="manga-lightbox-thumbs">
          {srcs.map((s, i) => (
            <button
              key={i}
              type="button"
              className={`manga-lightbox-thumb${i === idx ? ' active' : ''}`}
              aria-label={`查看第 ${i + 1} 张`}
              onClick={(e) => {
                e.stopPropagation();
                setIdx(i);
              }}
            >
              <img src={s} alt={`${i + 1}`} />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
