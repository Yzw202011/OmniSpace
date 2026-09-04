/* ==========================================================================
 * PaintWarmupBar —— 绘画页右栏的绘画模型冷启动进度条（2026-08-31）
 * --------------------------------------------------------------------------
 * 与 DialogWarmupBar 同构（用户需求：绘画页右栏同样常驻可见）：以
 * /draw/status 真实状态驱动（state=loading 或预热进行中 → 显示），
 * 就绪绿色 100% 残留 2s 收起。PaintWarmupModal 被「后台继续」关掉后
 * 本条仍持续显示。paint_engine.load_model 起手置 state=loading（后端
 * 2026-08-31 补的状态位），空闲 unloaded 不误显示。
 * ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import { getStatus } from '@/services/paintApi';
import { useWarmupStore } from '@/stores/useWarmupStore';
import { reportBgError } from '@/utils/errors';

/** 冷启动预估总时长（ms）：diffusers 管线实测 10~60s，文案口径「0.5-1 分钟」 */
const EXPECTED_MS = 60_000;
/** 轮询间隔（ms） */
const POLL_MS = 2_000;
/** 进度 ticker 间隔（ms） */
const TICK_MS = 250;
/** ready 后绿色残留时长（ms） */
const READY_LINGER_MS = 2_000;

/** 时间渐近进度（%）：0-4s 线性到 6%，随后指数逼近 90% 上限（绘画档参数） */
function calcProgress(elapsedMs: number): number {
  if (elapsedMs < 4_000) return (elapsedMs / 4_000) * 6;
  const t = (elapsedMs - 4_000) / 1_000;
  return Math.min(90, 6 + 84 * (1 - Math.exp(-t / 30)));
}

export default function PaintWarmupBar() {
  const [phase, setPhase] = useState<'idle' | 'loading' | 'ready'>('idle');
  const [now, setNow] = useState(() => Date.now());
  const startRef = useRef(0);
  const readyAtRef = useRef(0);

  /* 状态轮询：/draw/status loading 状态位 + 预热弹窗进行中双信号 */
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const st = await getStatus().catch((err: unknown) => {
        reportBgError('PaintWarmupBar.status', err);
        return null;
      });
      if (cancelled) return;
      const ws = useWarmupStore.getState();
      const state = (st as { state?: string } | null)?.state;
      const isReady = state === 'ready';
      const warmupActive = ws.visible && ws.kind === 'paint' && !ws.done;
      const active = !isReady && (state === 'loading' || warmupActive);
      setPhase((prev) => {
        if (isReady) {
          if (prev === 'loading') readyAtRef.current = Date.now();
          return prev === 'loading' ? 'ready' : 'idle';
        }
        if (active) {
          if (prev !== 'loading') {
            // 预热弹窗先于本条挂载时沿用其起点（进度连续，不重跳）
            startRef.current = ws.visible && ws.kind === 'paint' && ws.startedAt > 0
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
  // 起点晚于它会产生负耗时（曾实测首帧显示 -52%）
  const elapsed = done ? EXPECTED_MS : Math.max(0, now - startRef.current);
  const progress = done ? 100 : calcProgress(elapsed);
  const secs = Math.max(0, Math.floor(elapsed / 1_000));

  return (
    <div
      className="rp-row"
      style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}
      data-testid="paint-warmup-bar"
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
        aria-label="绘画模型冷启动进度"
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
          ? '绘画模型已就绪，可以开始创作'
          : '正在加载绘画模型（约 0.5-1 分钟），期间提交的生成请求将自动排队，就绪后立即开始绘制'}
      </div>
    </div>
  );
}
