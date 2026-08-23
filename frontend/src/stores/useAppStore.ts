/* ==========================================================================
 * OmniSpace AI v2.1 —— 全局应用状态（规格 §3.1 / §6.1 功能互斥状态机）
 * --------------------------------------------------------------------------
 * 职责：
 *   - 主题（Sakura 唯一主题，COM-009）与字号
 *   - activeFeature 功能互斥切换（规格 §6.1，检查 FEATURE_SWITCH_RULES）
 *   - 全局设置（持久化到后端 /system/settings）
 *   - 全局 Toast 通知队列
 * ========================================================================== */

import { create } from 'zustand';
import type { ActiveFeature } from '@/types';
import { canSwitchFeature } from '@/types';
import { getSettings, updateSettings } from '@/services/systemApi';
import { trackBehavior } from '@/services/learningApi';

/** 主题（双主题体系 × 亮暗双模式，2026-08-20 用户裁定脱离文档 COM-009）：
 *  - sakura     Sakura · 夜樱（暗色，默认）
 *  - light      Sakura · 拂晓（亮色）
 *  - tech       Nebula · 深空（高科技暗色）
 *  - tech-light Nebula · 晨辉（高科技亮色）
 * DOM 契约：<html data-family="tech"?> + <html data-theme="light"?>；
 * sakura 暗色 = 双属性皆缺省（向后兼容存量 CSS）。 */
export type Theme = 'sakura' | 'light' | 'tech' | 'tech-light';

/** 主题本地持久化键 */
const THEME_KEY = 'omnispace.theme';

/** 读取持久化主题（异常/旧值回退 Sakura 暗色；旧 'light' 值平滑迁移） */
function loadTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === 'light' || v === 'tech' || v === 'tech-light') return v;
    return 'sakura';
  } catch {
    return 'sakura';
  }
}

/** 应用主题到 <html>（data-family + data-theme 双属性）并持久化 */
function applyTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* 隐私模式写入失败静默 */
  }
  if (typeof document !== 'undefined') {
    const el = document.documentElement;
    const family = theme.startsWith('tech') ? 'tech' : 'sakura';
    const isLight = theme === 'light' || theme === 'tech-light';
    if (family === 'tech') {
      el.setAttribute('data-family', 'tech');
    } else {
      el.removeAttribute('data-family');
    }
    if (isLight) {
      el.setAttribute('data-theme', 'light');
    } else {
      el.removeAttribute('data-theme');
    }
  }
}

/** 字号档位 */
export type FontSize = 'sm' | 'md' | 'lg';

/** Toast 等级 */
export type ToastLevel = 'success' | 'warning' | 'error' | 'info';

/** 全局通知项 */
export interface ToastItem {
  id: number;
  text: string;
  level: ToastLevel;
  /** 是否正在退出（播放退出动画） */
  leaving?: boolean;
}

/** 全局应用状态 */
export interface AppState {
  /* ------------------------------ 主题与字号 ------------------------------ */
  /** 当前主题（sakura 暗色默认 / light 亮色可选） */
  theme: Theme;
  /** 字号档位 */
  fontSize: FontSize;

  /* ------------------------------ 功能互斥 ------------------------------ */
  /** 当前活跃功能（null 表示空闲，规格 §6.1） */
  activeFeature: ActiveFeature;
  /** 功能切换被阻断时的提示文案（null 表示无阻断） */
  featureBlockedMessage: string | null;

  /* ------------------------------ 设置 ------------------------------ */
  /** 全局设置（后端 /system/settings） */
  settings: Record<string, unknown>;
  /** 设置是否已加载 */
  settingsLoaded: boolean;

  /* ------------------------------ Toast ------------------------------ */
  /** Toast 通知队列（≤5 条） */
  toasts: ToastItem[];

  /* ------------------------------ 动作 ------------------------------ */
  /** 设置主题（切换 <html data-theme> 并持久化） */
  setTheme: (theme: Theme) => void;
  /** 设置字号 */
  setFontSize: (size: FontSize) => void;
  /**
   * 切换活跃功能（规格 §6.1 功能互斥）。
   * @param feature 目标功能（null 释放）
   * @returns true 放行 / false 被阻断（已自动 toast 提示）
   */
  setActiveFeature: (feature: Exclude<ActiveFeature, null>) => boolean;
  /** 释放活跃功能（回到空闲） */
  releaseActiveFeature: () => void;
  /** 检查功能是否可切换（不修改状态，前置检查） */
  canSwitch: (feature: Exclude<ActiveFeature, null>) => boolean;

  /** 加载全局设置 */
  loadSettings: () => Promise<void>;
  /** 更新全局设置（同步到后端） */
  saveSettings: (patch: Record<string, unknown>) => Promise<void>;

  /** 显示 Toast */
  showToast: (text: string, level?: ToastLevel) => void;
  /** 移除 Toast */
  removeToast: (id: number) => void;
}

/** Toast 自增 ID */
let toastSeq = 0;

export const useAppStore = create<AppState>((set, get) => ({
  /* ------------------------------ 初始状态 ------------------------------ */
  theme: loadTheme(),
  fontSize: 'md',
  activeFeature: null,
  featureBlockedMessage: null,
  settings: {},
  settingsLoaded: false,
  toasts: [],

  /* ------------------------------ 主题与字号 ------------------------------ */
  setTheme: (theme) => {
    set({ theme });
    applyTheme(theme);
  },
  setFontSize: (size) => {
    set({ fontSize: size });
    // 同步到 <html> data-font-size 供 CSS 变量消费
    if (typeof document !== 'undefined') {
      document.documentElement.setAttribute('data-font-size', size);
    }
  },

  /* ------------------------------ 功能互斥（规格 §6.1） ------------------------------ */
  canSwitch: (feature) => {
    const { activeFeature } = get();
    return canSwitchFeature(activeFeature, feature) === null;
  },

  setActiveFeature: (feature) => {
    const { activeFeature } = get();
    const blocked = canSwitchFeature(activeFeature, feature);
    if (blocked) {
      // 被阻断：自动 toast 提示，不修改状态
      get().showToast(blocked, 'warning');
      set({ featureBlockedMessage: blocked });
      return false;
    }
    set({ activeFeature: feature, featureBlockedMessage: null });
    // 行为学习埋点（fire-and-forget，失败静默）
    trackBehavior('module_switch', {
      content: feature,
      context: activeFeature ?? 'start',
      feature,
    });
    return true;
  },

  releaseActiveFeature: () => {
    set({ activeFeature: null, featureBlockedMessage: null });
  },

  /* ------------------------------ 设置 ------------------------------ */
  loadSettings: async () => {
    try {
      const data = await getSettings();
      set({ settings: data || {}, settingsLoaded: true });
    } catch {
      // 后端未就绪时静默，不阻塞界面
      set({ settingsLoaded: true });
    }
  },

  saveSettings: async (patch) => {
    const next = { ...get().settings, ...patch };
    set({ settings: next });
    try {
      await updateSettings(patch);
    } catch {
      // 持久化失败不回滚本地状态，保证 UI 可用
      get().showToast('设置保存失败，请检查后端服务', 'warning');
    }
  },

  /* ------------------------------ Toast ------------------------------ */
  showToast: (text, level = 'info') => {
    const id = ++toastSeq;
    set((state) => ({
      toasts: [...state.toasts, { id, text, level }].slice(-5),
    }));
    // 3.6s 后触发退出动画，150ms 后真正移除
    setTimeout(() => {
      set((state) => ({
        toasts: state.toasts.map((t) =>
          t.id === id ? { ...t, leaving: true } : t,
        ),
      }));
      setTimeout(() => {
        set((state) => ({
          toasts: state.toasts.filter((t) => t.id !== id),
        }));
      }, 160);
    }, 3600);
  },

  removeToast: (id) => {
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    }));
  },
}));

/* 启动时恢复持久化主题到 <html data-theme>（在 React 挂载前执行，避免主题闪烁） */
applyTheme(useAppStore.getState().theme);

export default useAppStore;
