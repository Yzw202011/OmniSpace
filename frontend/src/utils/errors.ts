/* ==========================================================================
 * errors.ts —— 前端错误呈现三分法（TASK-P2-08，审计 P16）
 * --------------------------------------------------------------------------
 * 策略（写入 docs/design/frontend-error-policy.md，本模块为代码载体）：
 *
 *  1. TOAST（用户必有感知）——用户主动发起的写操作失败：
 *     保存/生成/导入/提交/取消等。调 reportActionError()。
 *  2. CONSOLE（不打扰但可排查）——后台刷新/缓存回写失败：
 *     预取关键帧、行序回滚重拉、本地状态同步等。调 reportBgError()。
 *  3. SILENT（结构性静默）——仅限四种白名单场景：localStorage 隐私模式
 *     写失败、AbortError 类主动取消、竞态守护丢弃过期响应（isBenignError）、
 *     下层已 toast 呈现后仅为控制流收敛返回值的 catch（须带注释说明）。
 *
 * 铁律：组件/store 内禁止再写 `.catch(() => undefined)`；
 *      每个 catch 必须落到本模块两个入口之一（或显式注释白名单理由）；
 *      违例由 errors.test.ts 静态扫描兜底（TASK-P2-08 结构性防回归）。
 * ========================================================================== */

import { useAppStore } from '@/stores/useAppStore';

/** 常见错误对象 → 用户可读文案（无 message 时用 fallback） */
export function getErrorMessage(err: unknown, fallback: string): string {
  let msg = fallback;
  if (err && typeof err === 'object' && 'message' in err) {
    const m = (err as { message?: unknown }).message;
    if (typeof m === 'string' && m.trim()) msg = m;
  } else if (typeof err === 'string' && err.trim()) {
    msg = err;
  }
  // 后端语义化 suggestion（如「到模型管理手动加载」）随错误一并呈现：
  // 只报现象不给出路 = 用户面对「未加载」却不知道怎么办
  // （2026-08-31 漫剧生图未加载弹窗诉求）
  const suggestion =
    err && typeof err === 'object'
      ? (err as { suggestion?: unknown }).suggestion
      : undefined;
  if (typeof suggestion === 'string' && suggestion.trim() && !msg.includes(suggestion)) {
    msg = `${msg}。${suggestion}`;
  }
  return msg;
}

/** 入口①：用户动作失败 → toast（store 直取 getState，错误路径低频无需订阅） */
export function reportActionError(err: unknown, action: string): void {
  useAppStore.getState().showToast(getErrorMessage(err, `${action}失败`), 'error');
}

/** 入口②：后台刷新/回写失败 → console.warn（不打扰用户，留排查线索） */
export function reportBgError(scope: string, err: unknown): void {
  // eslint-disable-next-line no-console -- CONSOLE 级呈现的唯一合法出口（三分法第 2 条）
  console.warn(`[${scope}]`, err);
}

/** 白名单判定：主动取消/竞态丢弃类错误可结构性静默 */
export function isBenignError(err: unknown): boolean {
  return (
    (err instanceof DOMException && err.name === 'AbortError') ||
    (err instanceof Error && err.name === 'AbortError')
  );
}
