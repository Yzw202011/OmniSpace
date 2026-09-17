// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * waitPaintReady.ts —— 等待绘画模型就绪（漫剧生图自动续跑配套，2026-08-31）
 * --------------------------------------------------------------------------
 * 轮询 GET /draw/status 直到 loaded/state=ready 或超时。后端瞬断时静默
 * 重试（下一轮），超时返回 false 由调用方给真实出路。
 * ========================================================================== */

import { getStatus } from '@/services/paintApi';

export async function waitPaintReady(
  timeoutMs: number,
  pollMs = 2_000,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const st = (await getStatus()) as { loaded?: boolean; state?: string };
      if (st && (st.loaded === true || st.state === 'ready')) return true;
    } catch {
      /* silent-intent: 后端瞬时不可达：下轮重试 */
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
  return false;
}
