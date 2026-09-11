// 本项目仅供学习使用，商业授权请+Q 3559331368
/**
 * Progress 进度条组件
 * OmniSpace AI v2.3.1 — Sakura 暗色主题
 * --------------------------------------------------------------------------
 * 0~100 百分比进度条，颜色随进度变化（语义色令牌，§6.3.1）：
 *   成功绿 --color-success（<60%）/ 警告黄 --color-warning（60~85%）/
 *   错误红 --color-error（>85%）
 * 轨道底色沿用输入底 var(--color-input-bg)（与 Slider 未填充段一致，§6.3.2）。
 * 规格 §9 通用组件。
 */
import { useMemo } from 'react';

export interface ProgressProps {
  /** 当前进度 0~100 */
  value?: number;
  /** 是否为不确定进度（动画扫动，不显示具体百分比） */
  indeterminate?: boolean;
  /** 是否显示百分比文字 */
  showLabel?: boolean;
  /** 进度条高度（px），默认 8 */
  height?: number;
  /** 附加类名 */
  className?: string;
}

/** 负载颜色规则（语义色令牌）：成功绿<60% / 警告黄60-85% / 错误红>85% */
export function progressColor(value: number): string {
  if (value >= 85) return 'var(--color-error)';
  if (value >= 60) return 'var(--color-warning)';
  return 'var(--color-success)';
}

export function Progress({
  value = 0,
  indeterminate = false,
  showLabel = false,
  height = 8,
  className = '',
}: ProgressProps) {
  const clamped = useMemo(() => Math.max(0, Math.min(100, value)), [value]);
  const color = progressColor(clamped);

  return (
    <div className={['w-full', className].join(' ')}>
      <div
        className="w-full rounded-full bg-[var(--color-input-bg)] overflow-hidden"
        // height 为数据驱动属性（调用方可配，默认 8），属数据驱动样式
        style={{ height }}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={indeterminate ? undefined : clamped}
        aria-valuetext={indeterminate ? undefined : `${Math.round(clamped)}%`}
      >
        {indeterminate ? (
          <div
            className="h-full rounded-full animate-pulse bg-sakura-500"
            style={{ width: '40%' }}
          />
        ) : (
          <div
            className="h-full rounded-full transition-all duration-300 ease-out"
            // width/backgroundColor 为数据驱动值（进度百分比 + 负载语义色）
            style={{ width: `${clamped}%`, backgroundColor: color }}
          />
        )}
      </div>
      {showLabel ? (
        <div className="mt-1 text-xs text-[var(--color-text-tertiary)] text-right tabular-nums">{Math.round(clamped)}%</div>
      ) : null}
    </div>
  );
}

export default Progress;
