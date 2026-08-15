/**
 * Input 输入框组件
 * OmniSpace AI v2.3.1 — Sakura 暗色主题
 * --------------------------------------------------------------------------
 * 支持前缀 / 后缀图标节点与 error 错误状态（红色边框 + 提示文案）。
 * 规格 §6.3.2 输入框：背景 #0F3460 / 边框 #4ECDC4 30% / 圆角 6px。
 */
import { forwardRef } from 'react';
import type { InputHTMLAttributes, ReactNode } from 'react';

// Omit 'prefix'：HTMLInputElement 原生 prefix 属性为 string，此处需 ReactNode
export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'prefix'> {
  /** 前缀图标节点 */
  prefix?: ReactNode;
  /** 后缀图标节点 */
  suffix?: ReactNode;
  /** 是否为错误状态（显示红色边框） */
  error?: boolean;
  /** 错误提示文案（error 为 true 时展示于输入框下方） */
  errorText?: string;
}

/** 输入框基础类名（无前缀/后缀时直接使用；圆角 6px = rounded-md，§6.3.2） */
const BASE_CLASS = [
  'w-full h-10 px-3 text-base rounded-md border bg-[var(--color-input-bg)] text-[var(--color-text-primary)]',
  'placeholder:text-[var(--color-text-tertiary)] transition-colors duration-150',
  'focus:outline-none focus:ring-2 focus:ring-sakura-300',
].join(' ');

/** 错误/常规边框类名（常规边框 #4ECDC4 30%，规格值；focus 转薄荷绿实色） */
function borderClassOf(error: boolean, focusPrefix: 'focus' | 'focus-within'): string {
  if (error) {
    return 'border-[var(--color-error)]';
  }
  return focusPrefix === 'focus'
    ? 'border-[var(--color-input-border)] focus:border-mint-500'
    : 'border-[var(--color-input-border)] focus-within:border-mint-500';
}

/** 错误提示文字类名 */
const ERROR_TEXT_CLASS = 'mt-1 text-xs text-[var(--color-error)]';

/** 输入框组件（支持 ref 转发，便于表单聚焦） */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { prefix, suffix, error = false, errorText, className = '', ...rest },
  ref,
) {
  // 无前缀/后缀：直接渲染 input
  if (!prefix && !suffix) {
    return (
      <div className="w-full">
        <input
          ref={ref}
          className={[BASE_CLASS, borderClassOf(error, 'focus'), className].join(' ')}
          {...rest}
        />
        {error && errorText ? <p className={ERROR_TEXT_CLASS}>{errorText}</p> : null}
      </div>
    );
  }

  // 有前缀/后缀：用包装层承载图标
  return (
    <div className="w-full">
      <div
        className={[
          'flex items-center w-full rounded-md border bg-[var(--color-input-bg)] transition-colors duration-150',
          'focus-within:ring-2 focus-within:ring-sakura-300',
          borderClassOf(error, 'focus-within'),
          className,
        ].join(' ')}
      >
        {prefix ? <span className="pl-3 text-[var(--color-text-tertiary)] flex items-center">{prefix}</span> : null}
        <input
          ref={ref}
          className="flex-1 min-w-0 h-10 px-3 text-base bg-transparent text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus:outline-none"
          {...rest}
        />
        {suffix ? <span className="pr-3 text-[var(--color-text-tertiary)] flex items-center">{suffix}</span> : null}
      </div>
      {error && errorText ? <p className={ERROR_TEXT_CLASS}>{errorText}</p> : null}
    </div>
  );
});

export default Input;
