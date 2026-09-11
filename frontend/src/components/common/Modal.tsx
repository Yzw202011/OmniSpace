// 本项目仅供学习使用，商业授权请+Q 3559331368
/**
 * Modal 弹窗组件
 * OmniSpace AI v2.3.1 — Sakura 暗色主题
 * --------------------------------------------------------------------------
 * 支持 title / children / footer / onClose，遮罩层点击关闭、ESC 键关闭，
 * 并锁定背景滚动。规格 §6.3.2 弹窗：背景 var(--color-card) / 圆角 16px / 阴影加深；
 * §6.3.3 动效：缩放 0.9→1 + 淡入 250ms（modalIn keyframes）。
 */
import { useEffect } from 'react';
import type { ReactNode } from 'react';

export interface ModalProps {
  /** 标题（字符串时同时用作 aria-label） */
  title?: ReactNode;
  /** 内容区 */
  children?: ReactNode;
  /** 底部操作区 */
  footer?: ReactNode;
  /** 关闭回调（遮罩点击 / ESC / 关闭按钮触发） */
  onClose?: () => void;
  /** 是否显示右上角关闭按钮，默认 true */
  closable?: boolean;
  /** 是否允许点击遮罩层关闭，默认 true */
  maskClosable?: boolean;
  /** 自定义弹窗宽度（px），默认 480 */
  width?: number;
}

export function Modal({
  title,
  children,
  footer,
  onClose,
  closable = true,
  maskClosable = true,
  width = 480,
}: ModalProps) {
  // ESC 键关闭 + 锁定背景滚动
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && onClose) onClose();
    }
    document.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  function onMaskClick() {
    if (maskClosable && onClose) onClose();
  }

  return (
    <div
      className="fixed inset-0 z-[300] flex items-center justify-center p-4 bg-[var(--color-overlay)] backdrop-blur-sm"
      onClick={onMaskClick}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        className="relative bg-[var(--color-card)] border border-[var(--color-border-light)] rounded-2xl shadow-[var(--shadow-modal)] w-full max-h-[90vh] flex flex-col overflow-hidden animate-[modalIn_250ms_ease-out]"
        // maxWidth 为 width 属性驱动（调用方可配 480/640/720 等），属数据驱动样式
        style={{ maxWidth: width }}
        onClick={(e) => e.stopPropagation()}
      >
        {title ? (
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-divider)]">
            <h3 className="text-lg font-semibold text-[var(--color-text-primary)]">{title}</h3>
            {closable && onClose ? (
              <button
                type="button"
                onClick={onClose}
                aria-label="关闭"
                className="text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)] text-xl leading-none w-8 h-8 flex items-center justify-center rounded-md hover:bg-sakura-50 transition-colors"
              >
                ×
              </button>
            ) : null}
          </div>
        ) : null}
        <div className="px-6 py-5 overflow-y-auto flex-1 text-[var(--color-text-primary)]">{children}</div>
        {footer ? (
          <div className="px-6 py-4 border-t border-[var(--color-divider)] flex items-center justify-end gap-3">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default Modal;
