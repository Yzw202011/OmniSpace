/* ==========================================================================
 * themePalette —— 主题六态预览色板（W1 令牌化 2026-09-13）
 * --------------------------------------------------------------------------
 * 唯一真源：Settings 页主题卡片预览色（原内联在 Settings.tsx 的 JS 字面量）。
 * 用途 = 画布粒子预览，需要字面色值（CSS var() 无法同步喂给 canvas），
 * 故走 constants 单源而非 tokens.css（登记：审计 G-B13）。
 * 主题值域与 useAppStore.Theme 保持一致（新增主题须同步两处）。
 * ========================================================================== */
import type { Theme } from '@/stores/useAppStore';

/** 色板：[主色, 辅助色, 背景色, 顶部辉光] */
export type ThemePalette = [string, string, string, string];

export const THEME_PALETTE: Record<Theme, ThemePalette> = {
  sakura: ['#FF6B9D', '#4ECDC4', '#1A1A2E', 'rgba(255,107,157,0.45)'],
  light: ['#FF6B9D', '#4ECDC4', '#F8F9FC', 'rgba(255,107,157,0.35)'],
  tech: ['#22D3EE', '#8B7CF8', '#060B18', 'rgba(34,211,238,0.45)'],
  'tech-light': ['#0891B2', '#7C6BE8', '#EEF4FB', 'rgba(8,145,178,0.35)'],
  dali: ['#7CC0EC', '#F5A795', '#0D1628', 'rgba(124,192,236,0.45)'],
  'dali-light': ['#2E6FAE', '#D9755F', '#F1F5FB', 'rgba(46,111,174,0.35)'],
};
