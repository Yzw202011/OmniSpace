// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * useNotificationStore.ts —— 通知历史中心（批1 P25，2026-09-19）
 * --------------------------------------------------------------------------
 * 痛点：toast 弹完即逝，失败/完成/降级通知无处回看。
 * 方案：showToast 发射 error/warning/success 级 toast 时同步留痕到本仓
 * （info 级为高频噪声不留）；顶栏铃铛下拉「历史通知」区展示，可已读/清空。
 * 会话内存态（不落盘——历史通知的价值在本次使用期内，重启清零合理）。
 * ========================================================================== */

import { create } from 'zustand';

export type NotificationLevel = 'success' | 'warning' | 'error';

export interface NotificationItem {
  id: number;
  level: NotificationLevel;
  text: string;
  /** 记录时间戳（ms） */
  ts: number;
  /** 是否未读（新纪录默认未读；打开面板即全读） */
  unread: boolean;
}

/** 留痕上限（超出丢弃最旧） */
const MAX_ITEMS = 100;

interface NotificationState {
  items: NotificationItem[];
  /** 留痕（showToast 镜像调用；仅 error/warning/success） */
  record: (level: NotificationLevel, text: string) => void;
  /** 全部标记已读（打开面板时调用） */
  markAllRead: () => void;
  /** 清空历史 */
  clear: () => void;
}

let seq = 0;

export const useNotificationStore = create<NotificationState>((set) => ({
  items: [],
  record: (level, text) => {
    if (!text.trim()) return;
    seq += 1;
    set((state) => ({
      items: [
        { id: seq, level, text: text.trim(), ts: Date.now(), unread: true },
        ...state.items,
      ].slice(0, MAX_ITEMS),
    }));
  },
  markAllRead: () => {
    set((state) => ({
      items: state.items.map((it) => ({ ...it, unread: false })),
    }));
  },
  clear: () => set({ items: [] }),
}));

/** 通知等级显示名 */
export const NOTIFICATION_LEVEL_LABELS: Record<NotificationLevel, string> = {
  success: '完成',
  warning: '注意',
  error: '失败',
};
