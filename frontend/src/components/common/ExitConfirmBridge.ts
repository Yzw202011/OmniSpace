// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * ExitConfirmBridge.ts —— 壳关窗确认桥（2026-09-22 卡死根修版）
 * --------------------------------------------------------------------------
 * launcher/shell.py 关壳二次确认走本页品牌弹窗（useConfirmStore）。
 * 协议（结果槽，非 Promise 回传——壳端 evaluate_js 对 Promise 等待行为
 * 不确定，2026-09-21 版曾因此与 UI 线程互相等死）：
 *   1. 壳端置 window.__omniExitResult = null 后调 __omniExitConfirm(hint)；
 *   2. 用户作答后本侧把 true/false 写回 __omniExitResult；
 *   3. 壳端后台线程 0.2s 轮询该槽；超时/无钩子时壳端回退系统框，
 *      并调 __omniExitCancel() 收起本页弹窗（诚实取消，不替用户作答）。
 * ========================================================================== */

import { confirmDialog, useConfirmStore } from '@/stores/useConfirmStore';

declare global {
  interface Window {
    __omniExitConfirm?: (tasksHint: string) => void;
    __omniExitCancel?: () => void;
    __omniExitResult?: boolean | null;
    /** 激活门页直退标志（LicenseGate 置位）：壳关窗跳过二次确认（2026-10-29） */
    __omniExitBypass?: boolean;
  }
}

/** 注册壳关窗确认桥（App 挂载时调用；返回卸载函数） */
export function registerExitConfirmBridge(): () => void {
  window.__omniExitResult = null;
  window.__omniExitConfirm = (tasksHint: string): void => {
    const hint = (tasksHint || '').trim();
    window.__omniExitResult = null;
    void confirmDialog({
      // 有在跑任务时升重级（红按钮 + 提示徽标），普通关窗走中级
      level: hint ? 'heavy' : 'medium',
      title: '确定要完全退出 OmniSpace 吗？',
      lines: [
        ...(hint ? [hint] : []),
        '选择「退出」：保存并正常关闭全部组件；选择「继续使用」：取消关窗。',
      ],
      confirmText: '退出',
      cancelText: '继续使用',
      badge: hint ? '有任务在跑' : undefined,
    }).then((ok) => {
      window.__omniExitResult = ok;
    });
  };
  window.__omniExitCancel = () => {
    useConfirmStore.getState()._settle(false);
  };
  return () => {
    delete window.__omniExitConfirm;
    delete window.__omniExitCancel;
    delete window.__omniExitResult;
  };
}
