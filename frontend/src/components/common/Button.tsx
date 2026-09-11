/**
 * Button 通用按钮组件
 * OmniSpace AI v2.3.1 — Sakura 暗色主题（主色 var(--color-primary-500)，规格 §6.3.1）
 * --------------------------------------------------------------------------
 * 支持 4 种样式变体（primary / secondary / danger / ghost）、3 种尺寸
 * （sm / md / lg）以及 loading 加载态（显示旋转图标并禁用交互）。
 * 规格 §6.3.2 按钮规范 + §6.3.3 点击缩放 0.95 / 100ms 动效。
 */
import type { ButtonHTMLAttributes, ReactNode } from 'react';

/** 按钮样式变体 */
export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost';

/** 按钮尺寸 */
export type ButtonSize = 'sm' | 'md' | 'lg';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** 样式变体，默认 primary */
  variant?: ButtonVariant;
  /** 尺寸，默认 md */
  size?: ButtonSize;
  /** 是否处于加载中（显示旋转图标并禁用交互） */
  loading?: boolean;
  /** 按钮内容 */
  children?: ReactNode;
}

/**
 * 各变体的 Tailwind 类名（§6.3.2 规格表）。
 * sakura-* / mint-* 色阶由 sakura-theme.css @theme 注册（暗色取值）；
 * 语义色直接引用 tokens.css 的 CSS 变量。
 */
const VARIANT_CLASS: Record<ButtonVariant, string> = {
  // 按钮-主要：背景 var(--color-primary-500)，文字白色，hover 加深 10%
  primary: 'bg-sakura-500 text-white hover:bg-sakura-600 active:bg-sakura-700 shadow-sm',
  // 按钮-次要：背景透明，边框 var(--color-info)，文字 var(--color-info)
  secondary:
    'bg-transparent text-mint-500 border border-mint-500 hover:bg-mint-100 active:bg-mint-100',
  // 按钮-危险：背景 var(--color-error)，文字白色
  danger:
    'bg-[var(--color-error)] text-white hover:brightness-95 active:brightness-90 shadow-sm',
  // 幽灵按钮：透明底 + 浅樱粉文字（暗色底可读性，hover 樱粉 8% 铺底）
  ghost: 'bg-transparent text-sakura-300 hover:bg-sakura-50 active:bg-sakura-100',
};

/** 各尺寸的 Tailwind 类名（圆角统一 8px，§6.3.1 按钮圆角规格）
 * 尺寸节奏（4px 栅格）：sm=32 / md=36 / lg=44，控件文字统一 text-sm——
 * 与表单控件（h-9）同高对齐，消除高度动物园。
 */
const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-sm rounded-lg gap-1.5',
  md: 'h-9 px-4 text-sm rounded-lg gap-1.5',
  lg: 'h-11 px-5 text-sm rounded-lg gap-2',
};

/** 加载旋转图标 */
function LoadingIcon() {
  return (
    <svg className="animate-spin h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
      />
    </svg>
  );
}

/** 通用按钮组件 */
export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled,
  className = '',
  children,
  ...rest
}: ButtonProps) {
  const isDisabled = disabled || loading;
  return (
    <button
      type="button"
      disabled={isDisabled}
      aria-busy={loading || undefined}
      className={[
        // 按钮点击缩放 0.95 / 100ms（§6.3.3 动效规范）
        'inline-flex items-center justify-center font-medium transition-all duration-150',
        'active:scale-95 active:duration-100',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-sakura-300 focus-visible:ring-offset-1',
        'disabled:opacity-50 disabled:cursor-not-allowed select-none',
        VARIANT_CLASS[variant],
        SIZE_CLASS[size],
        className,
      ].join(' ')}
      {...rest}
    >
      {loading && <LoadingIcon />}
      {children}
    </button>
  );
}

export default Button;
// 本项目仅供学习使用，商业授权请+Q 3559331368
