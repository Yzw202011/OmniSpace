// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * useSystemHealthStore.ts —— 功能舱壁健康态（批2 P31/P32/P33，2026-09-19）
 * --------------------------------------------------------------------------
 * 数据源双通道：WS module_health/self_heal 实时推 + 初拉
 * /system/modules/health 兜底。状态栏芯片与恢复指南卡共用本仓。
 * ========================================================================== */

import { create } from 'zustand';

export interface ModuleHealthState {
  state: 'ok' | 'degraded';
  label: string;
  consecutive_failures: number;
  last_error: string;
  degraded_since: number;
}

export interface SelfHealGuide {
  module: string;
  label: string;
  message: string;
  /** 自愈失败时间戳（ms） */
  ts: number;
}

interface SystemHealthState {
  modules: Record<string, ModuleHealthState>;
  guide: SelfHealGuide | null;
  /** WS module_health 事件落地（状态栏芯片） */
  applyHealth: (payload: Partial<ModuleHealthState> & { module: string }) => void;
  /** 批量落地（初拉 /system/modules/health） */
  applySnapshot: (modules: Record<string, ModuleHealthState>) => void;
  /** 自愈失败 → 打开恢复指南卡 */
  openGuide: (g: SelfHealGuide) => void;
  closeGuide: () => void;
}

export const useSystemHealthStore = create<SystemHealthState>((set) => ({
  modules: {},
  guide: null,
  applyHealth: (p) => {
    if (!p?.module) return;
    set((s) => ({
      modules: {
        ...s.modules,
        [p.module]: {
          state: p.state === 'degraded' ? 'degraded' : 'ok',
          label: p.label ?? p.module,
          consecutive_failures: p.consecutive_failures ?? 0,
          last_error: p.last_error ?? '',
          degraded_since: p.degraded_since ?? 0,
        },
      },
    }));
  },
  applySnapshot: (modules) => set({ modules: modules ?? {} }),
  openGuide: (g) => set({ guide: g }),
  closeGuide: () => set({ guide: null }),
}));
