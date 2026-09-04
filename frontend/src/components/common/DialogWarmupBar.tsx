/* ==========================================================================
 * DialogWarmupBar —— 右栏「当前会话」内的对话模型冷启动进度条（2026-08-31）
 * --------------------------------------------------------------------------
 * 用户需求：右栏当前会话区常驻可见的冷启动进度。与 WarmupModal 互补：
 * 弹窗可被「后台继续」关掉，本进度条以引擎真实状态驱动（/dialog/status
 * 非 ready 且 vLLM 运行/启动中，或预热进行中 → 显示），ready 后绿色 100%
 * 残留 2s 收起。进度 = 时间渐近曲线（与 WarmupModal 同源，后端无细粒度
 * 加载进度接口，不谎报）。
 * ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import { useWarmupStore } from '@/stores/useWarmupStore';
import { getDialogEngineStatus } from '@/services/dialogApi';
import { getVllmStatus } from '@/services/modelApi';
import { reportBgError } from '@/utils/errors';

/** 冷启动预估总时长（ms）：vLLM 实测 ~157s，文案口径「2-3 分钟」 */
const EXPECTED_MS = 150_000;
/** 轮询间隔（ms） */
const POLL_MS = 2_000;
/** 进度 ticker 间隔（ms） */
const TICK_MS = 250;
/** ready 后绿色残留时长（ms） */
const READY_LINGER_MS = 2_000;

/** 时间渐近进度（%）：0-4s 线性到 6%，随后指数逼近 90% 上限 */
function calcProgress(elapsedMs: number): number {
  if (elapsedMs < 4_000) return (elapsedMs / 4_000) * 6;
  const t = (elapsedMs - 4_000) / 1_000;
  return Math.min(90, 6 + 84 * (1 - Math.exp(-t / 80)));
}

export default function DialogWarmupBar() {
  const [phase, setPhase] = useState<'idle' | 'loading' | 'ready'>('idle');
  const [now, setNow] = useState(() => Date.now());
  const startRef = useRef(0);
  const readyAtRef = useRef(0);

  /* 状态轮询：dialog 状态主信号 + vllm 进程阶段 + 预热弹窗进行中 */
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const [st, v] = await Promise.all([
        getDialogEngineStatus().catch((err: unknown) => {
          reportBgError('DialogWarmupBar.dialogStatus', err);
          return null;
        }),
        getVllmStatus().catch((err: unknown) => {
          reportBgError('DialogWarmupBar.vllmStatus', err);
          return null;
        }),
      ]);
      if (cancelled) return;
      const ws = useWarmupStore.getState();
      const isReady = Boolean(st && (st as { state?: string }).state === 'ready');
      const vllmBusy = Boolean(
        v && ((v as { running?: boolean }).running
          || (v as { booting?: boolean }).booting),
      );
      const warmupActive = ws.visible && ws.kind === 'dialog' && !ws.done;
      const active = !isReady
        && (vllmBusy || warmupActive
          || (st as { state?: string } | null)?.state === 'booting');
      setPhase((prev) => {
        if (isReady) {
          if (prev === 'loading') readyAtRef.current = Date.now();
          return prev === 'loading' ? 'ready' : 'idle';
        }
        if (active) {
          if (prev !== 'loading') {
            // 预热弹窗先于本条挂载时沿用其起点（进度连续，不重跳）
            startRef.current = ws.visible && ws.kind === 'dialog' && ws.startedAt > 0
              ? ws.startedAt
              : Date.now();
          }
          return 'loading';
        }
        return 'idle';
      });
    }
    void poll();
    const id = window.setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  /* 进度 ticker：加载期间 250ms 重算已耗时 */
  useEffect(() => {
    if (phase !== 'loading') return;
    const id = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, [phase]);

  /* ready 残留 2s 后收起 */
  useEffect(() => {
    if (phase !== 'ready') return;
    const id = window.setTimeout(() => setPhase('idle'), READY_LINGER_MS);
    return () => window.clearTimeout(id);
  }, [phase]);

  if (phase === 'idle') return null;

  const done = phase === 'ready';
  // Math.max 钳制：进入 loading 的首帧 now 可能还是挂载时的旧值，
  // 起点晚于它会产生负耗时（PaintWarmupBar 曾实测首帧显示 -52%）
  const elapsed = done ? EXPECTED_MS : Math.max(0, now - startRef.current);
  const progress = done ? 100 : calcProgress(elapsed);
  const secs = Math.max(0, Math.floor(elapsed / 1_000));

  return (
    <div
      className="rp-row"
      style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}
      data-testid="dialog-warmup-bar"
    >
      <div className="flex items-center justify-between" style={{ gap: 8 }}>
        <span className="rp-label">模型冷启动</span>
        <span
          className="rp-value"
          style={{ fontVariantNumeric: 'tabular-nums', fontSize: 11 }}
        >
          {done ? '就绪' : `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')} · ${Math.round(progress)}%`}
        </span>
      </div>
      <div
        className="w-full"
        style={{ height: 6, borderRadius: 999, background: 'var(--color-input-bg)', overflow: 'hidden' }}
        role="progressbar"
        aria-label="对话模型冷启动进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(progress)}
      >
        <div
          style={{
            width: '100%',
            height: '100%',
            borderRadius: 999,
            backgroundColor: done ? 'var(--color-success)' : 'var(--color-primary)',
            transform: `scaleX(${Math.min(100, Math.max(0, progress)) / 100})`,
            transformOrigin: 'left center',
            transition: 'transform .3s ease-out',
          }}
        />
      </div>
      <div className="rp-hint" style={{ fontSize: 11 }}>
        {done
          ? '对话模型已就绪，可以开始对话'
          : '正在加载对话模型（约 2-3 分钟），期间发送的消息将自动排队，就绪后立即回复'}
      </div>
    </div>
  );
}
