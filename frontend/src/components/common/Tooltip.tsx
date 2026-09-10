/**
 * Tooltip 提示气泡组件
 * OmniSpace AI v2.3.1 — Sakura 暗色主题
 * --------------------------------------------------------------------------
 * 悬停 / 聚焦触发，在触发元素周围显示提示气泡，支持四个方向。
 * 气泡为暗色抬升面：卡片底 var(--color-card) + 薄荷绿描边 + 一级文字（§6.3.1 令牌），
 * 用于侧栏收起态悬停显示功能名称（§6.1.2）。
 * 规格 §9 通用组件。
 */
import { useRef, useState } from 'react';
import type { ReactNode } from 'react';

export type TooltipPlacement = 'top' | 'bottom' | 'left' | 'right';

export interface TooltipProps {
  /** 提示文案 */
  content: ReactNode;
  /** 触发元素 */
  children: ReactNode;
  /** 弹出方向，默认 top */
  placement?: TooltipPlacement;
  /** 延迟显示时间（ms），默认 100 */
  delay?: number;
}

/** 各方向定位类名 */
const PLACEMENT_CLASS: Record<TooltipPlacement, string> = {
  top: 'bottom-full left-1/2 -translate-x-1/2 mb-1.5',
  bottom: 'top-full left-1/2 -translate-x-1/2 mt-1.5',
  left: 'right-full top-1/2 -translate-y-1/2 mr-1.5',
  right: 'left-full top-1/2 -translate-y-1/2 ml-1.5',
};

export function Tooltip({ content, children, placement = 'top', delay = 100 }: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  function show() {
    timerRef.current = setTimeout(() => setVisible(true), delay);
  }
  function hide() {
    if (timerRef.current) clearTimeout(timerRef.current);
    setVisible(false);
  }

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      {visible ? (
        <span
          role="tooltip"
          className={[
            'absolute z-[500] whitespace-nowrap px-2.5 py-1 rounded-md',
            'text-xs text-[var(--color-text-primary)] bg-[var(--color-card)]',
            'border border-[var(--color-border)] shadow-[var(--shadow-md)] pointer-events-none',
            'animate-[fadeIn_150ms_ease-out]',
            PLACEMENT_CLASS[placement],
          ].join(' ')}
        >
          {content}
        </span>
      ) : null}
    </span>
  );
}

export default Tooltip;
