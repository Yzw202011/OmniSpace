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
import { RealtimePayloadSchema } from '@/services/schema';
import { reportBgError } from '@/utils/errors';

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
  _wsBackoffTimer: ReturnType<typeof setInterval> | null;
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
let _synergyVisHandler: (() => void) | null = null;

// 性能优化：硬件 synergy 轮询前台 5s / 页面隐藏 15s（实时遥测已由 WS 每 2s
// 推送，HTTP 轮询非关键路径，隐藏时降频即可，避免无意义请求叠加）。
const SYNERGY_POLL_FG_MS = 5000;
const SYNERGY_POLL_BG_MS = 15000;
function _synergyDelay(): number {
  return typeof document !== 'undefined' && document.hidden
    ? SYNERGY_POLL_BG_MS : SYNERGY_POLL_FG_MS;
}

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
  _wsBackoffTimer: null,
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

    // system_status：实时遥测推送（WS 内置桥接）。Zod 信封校验（批 3-3b）：
    // 非法帧 reportBgError 后丢弃保持末值，等断线兜底轮询自愈，不崩页
    const offRealtime = conn.on<unknown>('system_status', (data) => {
      const parsed = RealtimePayloadSchema.safeParse(data);
      if (!parsed.success) {
        reportBgError('hardware.realtime', parsed.error.issues[0] ?? new Error('遥测帧格式非法'));
        return;
      }
      if (data) {
        set({ realtime: hardwareApi.normalizeRealtime(data) });
      }
    });

    wsUnsubscribers = [offStatus, offRealtime];

    // 3. 轮询 /hardware/synergy（C-1/C-2）：前台 5s / 后台 15s，
    //    可见性切换时重排间隔（性能优化）
    get().fetchSynergy();
    const schedule = () => {
      if (get()._synergyTimer) {
        clearInterval(get()._synergyTimer as ReturnType<typeof setInterval>);
      }
      const timer = setInterval(() => {
        get().fetchSynergy();
      }, _synergyDelay());
      set({ _synergyTimer: timer });
    };
    schedule();
    _synergyVisHandler = () => schedule();
    document.addEventListener('visibilitychange', _synergyVisHandler);

    // 4. WS 断连兜底（#7 2026-09-02 实测复盘）：后端重启窗口内 WS
    //    指数退避重连（最长 30s），期间实时遥测冻结在末值——徽标
    //    「不刷新」观感即来自此空档（WS 正常时实测 2s 跟手无误）。
    //    断连时降频 5s HTTP 拉取遥测保活，重连成功即自动停拉。
    const backoffTimer = setInterval(async () => {
      if (useHardwareStore.getState().wsStatus === 'open') {
        return;
      }
      try {
        const rt = await hardwareApi.getRealtime();
        if (rt) {
          set({ realtime: rt });
        }
      } catch {
        /* silent-intent: 后端未就绪保持末值 */
      }
    }, 5000);
    set({ _wsBackoffTimer: backoffTimer });
  },

  destroy: () => {
    if (get()._synergyTimer) {
      clearInterval(get()._synergyTimer as ReturnType<typeof setInterval>);
    }
    if (get()._wsBackoffTimer) {
      clearInterval(get()._wsBackoffTimer as ReturnType<typeof setInterval>);
    }
    if (_synergyVisHandler) {
      document.removeEventListener('visibilitychange', _synergyVisHandler);
      _synergyVisHandler = null;
    }
    wsUnsubscribers.forEach((off) => {
      try {
        off();
      } catch {
        /* silent-intent: 忽略 */
      }
    });
    wsUnsubscribers = [];
    set({
      _synergyTimer: null,
      _wsBackoffTimer: null,
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
      /* silent-intent: 后端未就绪时静默，不阻塞界面 */
    }
  },
}));

export default useHardwareStore;
// 本项目仅供学习使用，商业授权请+Q 3559331368
