/* ==========================================================================
 * OmniSpace AI v2.1 —— 硬件状态（规格 §4.6 硬件API + §5.1 协同调度）
 * --------------------------------------------------------------------------
 * 职责：
 *   - hardwareProfile 硬件静态画像（首次加载 GET /hardware/info）
 *   - schedulerState 协同调度状态 + synergy 协同聚合状态
 *   - realtime 实时遥测（CPU/GPU/VRAM/RAM，WS ws://.../hardware/realtime 推送）
 *   - 每 2 秒轮询 /hardware/synergy（C-1 协同模式 + 功能锁 + 活动模型/缓存数）
 * 覆盖测试用例：COM-011（WS 断线重连灯）、C-1（StatusBar 协同模式）
 * ========================================================================== */

import { create } from 'zustand';
import type {
  HardwareProfile,
  HardwareRealtime,
  SynergyState,
  SynergyMode,
  SchedulerState,
  WsStatus,
  ActiveFeature,
} from '@/types';
import * as hardwareApi from '@/services/hardwareApi';
import { getHardwareRealtime } from '@/services/ws';

/** 协同模式中文文案映射（规格 §5.1，C-1：StatusBar 展示协同模式） */
export const SYNERGY_MODE_TEXT: Record<SynergyMode, string> = {
  gpu_primary: 'GPU主导',
  cpu_assist: 'GPU←CPU协助',
  gpu_assist_cpu: 'CPU←GPU协助',
  memory_pressure: '内存←GPU/CPU协助',
  all_tense: '智能降级中',
  all_idle: '预加载中',
};

/** 硬件状态 */
export interface HardwareState {
  /* ------------------------------ 静态画像 ------------------------------ */
  hardwareProfile: HardwareProfile | null;
  profileLoaded: boolean;

  /* ------------------------------ 协同调度 ------------------------------ */
  schedulerState: SchedulerState | null;
  synergy: SynergyState | null;
  /** 协同模式中文文案 */
  synergyModeText: string;
  /** 活动模型名称 */
  activeModel: string;
  /** 缓存模型数 */
  cachedCount: number;
  /** 功能锁当前持有功能（来自 feature_lock 快照） */
  lockedFeature: ActiveFeature;

  /* ------------------------------ 实时遥测 ------------------------------ */
  realtime: HardwareRealtime | null;
  wsStatus: WsStatus;

  /* ------------------------------ 内部计时器 ------------------------------ */
  _synergyTimer: ReturnType<typeof setInterval> | null;
  _subscribed: boolean;

  /* ------------------------------ 动作 ------------------------------ */
  /** 初始化：加载硬件画像 + 启动 WS + 启动 2s 轮询（幂等） */
  init: () => Promise<void>;
  /** 销毁：停止轮询与订阅 */
  destroy: () => void;
  /** 刷新硬件画像 */
  refreshProfile: () => Promise<void>;
  /** 手动拉取一次协同状态 */
  fetchSynergy: () => Promise<void>;
}

/** 解订函数集合 */
let wsUnsubscribers: Array<() => void> = [];

export const useHardwareStore = create<HardwareState>((set, get) => ({
  hardwareProfile: null,
  profileLoaded: false,

  schedulerState: null,
  synergy: null,
  synergyModeText: '--',
  activeModel: '--',
  cachedCount: 0,
  lockedFeature: null,

  realtime: null,
  wsStatus: 'closed',

  _synergyTimer: null,
  _subscribed: false,

  init: async () => {
    if (get()._subscribed) {
      return;
    }
    set({ _subscribed: true });

    // 1. 加载硬件画像
    get().refreshProfile();

    // 2. 启动硬件实时 WS
    const conn = getHardwareRealtime();
    conn.connect();
    set({ wsStatus: conn.status });

    const offStatus = conn.onStatus((status) => {
      set({ wsStatus: status });
    });

    // system_status：实时遥测推送（WS 内置桥接）
    const offRealtime = conn.on<Parameters<typeof hardwareApi.normalizeRealtime>[0]>('system_status', (data) => {
      if (data) {
        set({ realtime: hardwareApi.normalizeRealtime(data) });
      }
    });

    wsUnsubscribers = [offStatus, offRealtime];

    // 3. 启动 2s 轮询 /hardware/synergy（C-1/C-2）
    get().fetchSynergy();
    const timer = setInterval(() => {
      get().fetchSynergy();
    }, 2000);
    set({ _synergyTimer: timer });
  },

  destroy: () => {
    if (get()._synergyTimer) {
      clearInterval(get()._synergyTimer as ReturnType<typeof setInterval>);
    }
    wsUnsubscribers.forEach((off) => {
      try {
        off();
      } catch {
        /* 忽略 */
      }
    });
    wsUnsubscribers = [];
    set({
      _synergyTimer: null,
      _subscribed: false,
    });
  },

  refreshProfile: async () => {
    try {
      const profile = await hardwareApi.getHardwareInfo();
      set({ hardwareProfile: profile, profileLoaded: true });
    } catch {
      // 后端未就绪时静默，不阻塞界面
      set({ profileLoaded: true });
    }
  },

  fetchSynergy: async () => {
    try {
      const data = await hardwareApi.getSynergy();
      if (!data) {
        return;
      }
      const sched = data.scheduler || ({} as SchedulerState);
      const vram = data.vram || { resident_models: [], cached_models: [] };
      const fl = data.feature_lock || { active_feature: null };

      // 协同模式文案
      const mode = sched.current_mode;
      const modeText = mode ? SYNERGY_MODE_TEXT[mode] || mode : '--';

      // 活动模型：优先取 VRAM 常驻模型，其次功能锁持有者
      const resident = vram.resident_models || [];
      let activeModel: string;
      if (resident.length > 0) {
        activeModel = resident
          .map((m) => (typeof m === 'string' ? m : m.name || m.model || ''))
          .join('、');
      } else if (fl.active_feature) {
        activeModel = fl.active_feature;
      } else {
        activeModel = '--';
      }

      const cached = vram.cached_models || [];

      set({
        schedulerState: sched,
        synergy: data,
        synergyModeText: modeText,
        activeModel,
        cachedCount: cached.length,
        lockedFeature: fl.active_feature ?? null,
      });
    } catch {
      // 后端未就绪时静默，不阻塞界面
    }
  },
}));

export default useHardwareStore;
