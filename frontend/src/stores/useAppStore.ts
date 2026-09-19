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
import { mirrorPref } from '@/services/uiPrefs';
import { reportBgError } from '@/utils/errors';
import { trackBehavior } from '@/services/learningApi';
import { useNotificationStore } from '@/stores/useNotificationStore';

/** 主题（三主题体系 × 亮暗双模式，2026-08-20 脱离文档 COM-009；
 *  2026-09-11 增补第三族 Dali 风花雪月治愈主题）：
 *  - sakura     Sakura · 夜樱（暗色，默认）
 *  - light      Sakura · 拂晓（亮色）
 *  - tech       Nebula · 深空（高科技暗色）
 *  - tech-light Nebula · 晨辉（高科技亮色）
 *  - dali       Dali · 洱海月（治愈系暮金暗色）
 *  - dali-light Dali · 苍山雪（治愈系晨金亮色）
 * DOM 契约：<html data-family="tech|dali"?> + <html data-theme="light"?>；
 * sakura 暗色 = 双属性皆缺省（向后兼容存量 CSS）。 */
export type Theme = 'sakura' | 'light' | 'tech' | 'tech-light' | 'dali' | 'dali-light';

/** 全部合法主题值（loadTheme 白名单 + 后端回放校验共用） */
const THEME_VALUES: ReadonlyArray<Theme> = [
  'sakura',
  'light',
  'tech',
  'tech-light',
  'dali',
  'dali-light',
];

/** 主题本地持久化键 */
const THEME_KEY = 'omnispace.theme';

/** 本次会话用户是否主动换过主题（true 时禁用后端主题回放，防竞态覆盖） */
let userChangedTheme = false;

/** 读取持久化主题（异常/旧值回退 Sakura 暗色；旧 'light' 值平滑迁移） */
function loadTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v && (THEME_VALUES as readonly string[]).includes(v)) {
      return v as Theme;
    }
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
    /* silent-intent: 隐私模式写入失败静默 */
  }
  if (typeof document !== 'undefined') {
    const el = document.documentElement;
    const family = theme.startsWith('tech')
      ? 'tech'
      : theme.startsWith('dali')
        ? 'dali'
        : 'sakura';
    const isLight = theme === 'light' || theme === 'tech-light' || theme === 'dali-light';
    if (family === 'sakura') {
      // sakura 族 = 双属性缺省（存量 CSS 的默认作用域，不可改为 setAttribute）
      el.removeAttribute('data-family');
    } else {
      el.setAttribute('data-family', family);
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
  // 字号持久化（2026-09-15 审计修复）：此前只写内存+DOM，每次重启回
  // md——与 970a608「选型重启回退」同病。localStorage 即时生效 + setFontSize 侧镜像
  fontSize: (() => {
    try {
      const v = localStorage.getItem('omnispace.fontSize');
      return v === 'sm' || v === 'lg' ? v : 'md';
    } catch {
      return 'md';
    }
  })(),
  activeFeature: null,
  featureBlockedMessage: null,
  settings: {},
  settingsLoaded: false,
  toasts: [],

  /* ------------------------------ 主题与字号 ------------------------------ */
  setTheme: (theme) => {
    set({ theme });
    userChangedTheme = true; // 本次会话用户主动选择：禁用后端回放，防竞态覆盖
    applyTheme(theme);
    // 治愈系主题 v1.4：回写后端 system.settings.theme，供下次启动 boot 读库
    // 给启动页换肤（方案 §1.4 联动链路）。守卫（防整包替换 clobber）：
    // settings 未加载或为空时先静默 loadSettings 补齐，再全量发送。
    // fire-and-forget 静默失败：主题即时生效走 localStorage，回写只服务启动页。
    const writeBack = (): void => {
      const s = get().settings;
      if (Object.keys(s).length > 0) {
        updateSettings({ ...s, theme }).catch((e: unknown) =>
          reportBgError('theme-writeback', e),
        );
      }
    };
    const { settings, settingsLoaded, loadSettings } = get();
    if (!settingsLoaded || Object.keys(settings).length === 0) {
      loadSettings()
        .then(writeBack)
        .catch((e: unknown) => reportBgError('theme-writeback', e));
    } else {
      writeBack();
    }
  },
  setFontSize: (size) => {
    set({ fontSize: size });
    // 同步到 <html> data-font-size 供 CSS 变量消费
    if (typeof document !== 'undefined') {
      document.documentElement.setAttribute('data-font-size', size);
    }
    // 持久化（2026-09-15 审计修复）：localStorage 即时 + 后端镜像防丢
    try {
      localStorage.setItem('omnispace.fontSize', size);
      mirrorPref('omnispace.fontSize', size);
    } catch {
      /* silent-intent: 隐私模式写入失败：内存态仍生效（本次会话内） */
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
      // 主题回放（2026-09-12 用户报障修复：选洱海月重启变夜樱）：
      // localStorage 会因换端口启动（5800 被占顺延）/壳配置/清缓存丢失，
      // 后端 system.settings.theme 才是跨会话可靠真源（启动页同源）。
      // 后端存有合法主题且与当前不符 → 回放（顺带刷新 localStorage）。
      // 用户本次会话主动换过主题则跳过（防拉取竞态覆盖新选择）。
      const t = (data || {}).theme;
      if (
        !userChangedTheme &&
        typeof t === 'string' &&
        (THEME_VALUES as readonly string[]).includes(t) &&
        t !== get().theme
      ) {
        const theme = t as Theme;
        set({ theme });
        applyTheme(theme);
      }
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
    // 批1 P25（2026-09-19）：重要级 toast 同步留痕通知历史（info 噪声不留）
    if (level === 'success' || level === 'warning' || level === 'error') {
      useNotificationStore.getState().record(level, text);
    }
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
// 本项目仅供学习使用，商业授权请+Q 3559331368
