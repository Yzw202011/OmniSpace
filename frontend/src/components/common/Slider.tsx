// 本项目仅供学习使用，商业授权请+Q 3559331368
/**
 * Slider 滑块组件
 * OmniSpace AI v2.3.1 — Sakura 暗色主题
 * --------------------------------------------------------------------------
 * 受控滑块，支持 min / max / step / value / onChange，
 * 轨道以 Sakura 主色（var(--color-primary-500)）填充、底色 var(--color-input-bg)，并在标签行展示当前数值。
 * 规格 §6.3.2 输入控件。底层样式见 styles/sakura-theme.css 的 .omni-slider。
 */
import type { ChangeEvent } from 'react';

export interface SliderProps {
  /** 最小值，默认 0 */
  min?: number;
  /** 最大值，默认 100 */
  max?: number;
  /** 步长，默认 1 */
  step?: number;
  /** 当前值 */
  value: number;
  /** 值变化回调 */
  onChange?: (value: number) => void;
  /** 标签文案 */
  label?: string;
  /** 是否显示数值（默认显示） */
  showValue?: boolean;
  /** 附加类名 */
  className?: string;
  /** 是否禁用 */
  disabled?: boolean;
}

export function Slider({
  min = 0,
  max = 100,
  step = 1,
  value,
  onChange,
  label,
  showValue = true,
  className = '',
  disabled = false,
}: SliderProps) {
  // 已填充百分比，用于轨道渐变着色
  const percent = max > min ? ((value - min) / (max - min)) * 100 : 0;

  function handleChange(e: ChangeEvent<HTMLInputElement>) {
    onChange?.(parseFloat(e.target.value));
  }

  return (
    <div className={['w-full', className].join(' ')}>
      {(label || showValue) && (
        <div className="flex items-center justify-between mb-1.5">
          {label ? <span className="text-sm font-medium text-[var(--color-text-primary)]">{label}</span> : <span />}
          {showValue ? (
            <span className="text-sm font-medium text-[var(--color-primary)] tabular-nums">{value}</span>
          ) : null}
        </div>
      )}
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={handleChange}
        aria-label={label}
        className="omni-slider w-full"
        style={{
          // 已填充百分比为数据驱动值（唯一样式内联点）；
          // 填充色用主色 var(--color-primary-500)，未填充轨道用专用令牌（暗底浅灰/亮底深灰，
          // 与 --color-input-bg 解耦，避免 var(--color-input-bg) 在深蓝卡片上辨识度不足）
          background: `linear-gradient(to right, var(--color-primary) ${percent}%, var(--color-slider-track) ${percent}%)`,
        }}
      />
    </div>
  );
}

export default Slider;
