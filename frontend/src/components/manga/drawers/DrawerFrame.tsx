// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 漫剧工作台抽屉框架（参考 demo 右侧抽屉）
 * 统一头部（标题 + 选中镜号副标题 + 关闭按钮）+ 滚动体。
 * ========================================================================== */

import type { ReactNode } from 'react';
import { X } from 'lucide-react';

interface DrawerFrameProps {
  /** 抽屉标题 */
  title: string;
  /** 副标题（如「· 镜 3」） */
  subtitle?: string;
  /** 宽抽屉（420px，默认 340px） */
  wide?: boolean;
  onClose: () => void;
  children: ReactNode;
}

export function DrawerFrame({ title, subtitle, wide, onClose, children }: DrawerFrameProps) {
  return (
    <aside className={`manga-wb-drawer${wide ? ' wide' : ''}`}>
      <div className="manga-wb-drawer-header">
        <h3 className="manga-wb-drawer-title">
          {title}
          {subtitle && <span className="manga-wb-sub"> {subtitle}</span>}
        </h3>
        <button type="button" className="btn-icon" onClick={onClose} aria-label="关闭抽屉">
          <X size={16} />
        </button>
      </div>
      <div className="manga-wb-drawer-body">{children}</div>
    </aside>
  );
}

export default DrawerFrame;
