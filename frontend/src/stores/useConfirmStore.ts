// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * useConfirmStore.ts —— 全局确认弹窗服务（批4 P9 删除分级规范）
 * --------------------------------------------------------------------------
 * 三级模板（对象+去向+可否反悔三要素）：
 *   light  轻级：删单条小对象（消息/单格）——一次普通确认
 *   medium 中级：删会话/章节/资产——确认弹窗列清「将删除什么」
 *   heavy  重级：删整作品/项目/权重——列清「删什么、留什么、可否恢复」
 *            +红色强确认按钮
 * 用法：const go = await confirmDialog({ level:'heavy', title:'删除作品《X》',
 *   lines:['将删除：分镜 12 行、关键帧 8 张、视频 2 条','保留：角色资产（转入全局库）'],
 *   confirmText:'永久删除' });
 * ========================================================================== */

import { create } from 'zustand';

export type ConfirmLevel = 'light' | 'medium' | 'heavy';

export interface ConfirmOptions {
  level: ConfirmLevel;
  title: string;
  /** 正文行（三要素：将删除什么/保留什么/可否反悔） */
  lines?: string[];
  confirmText?: string;
  cancelText?: string;
}

interface PendingConfirm extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

interface ConfirmState {
  pending: PendingConfirm | null;
  /** 弹出确认框，返回 Promise（true=确认） */
  ask: (opts: ConfirmOptions) => Promise<boolean>;
  _settle: (ok: boolean) => void;
}

export const useConfirmStore = create<ConfirmState>((set, get) => ({
  pending: null,
  ask: (opts) => new Promise<boolean>((resolve) => {
    set({ pending: { ...opts, resolve } });
  }),
  _settle: (ok) => {
    const p = get().pending;
    set({ pending: null });
    p?.resolve(ok);
  },
}));

/** 函数式便捷入口（组件外/事件回调里用） */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  return useConfirmStore.getState().ask(opts);
}
