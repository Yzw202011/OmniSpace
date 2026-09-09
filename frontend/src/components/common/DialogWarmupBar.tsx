/* ==========================================================================
 * DialogWarmupBar —— 右栏「当前会话」内的对话模型状态行（常驻三态）
 * --------------------------------------------------------------------------
 * 演进：2026-08-31 冷启动进度条（仅装载期显示）→ 2026-09-08 用户拍板
 * 升级为常驻三态：未加载（黄，防「误以为已就绪」）/ 装载中（进度条）/
 * 已就绪（绿，常驻不再 2 秒消失）。状态源 = /dialog/status 引擎真实态
 * （2s 轮询）+ vLLM 子进程忙标志 + 预热弹窗进行中（旧后端无 loading
 * 态时的兜底触发）。进度 = 时间渐近曲线（后端无细粒度加载进度，不谎报）。
 * ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import { useWarmupStore } from '@/stores/useWarmupStore';
import { useDialogStore } from '@/stores/useDialogStore';
import { getDialogEngineStatus } from '@/services/dialogApi';
import { getVllmStatus } from '@/services/modelApi';
import { reportBgError } from '@/utils/errors';

/** 轮询间隔（ms） */
const POLL_MS = 2_000;
/** 进度 ticker 间隔（ms） */
const TICK_MS = 250;

/** 时间渐近进度（%）：0-4s 线性到 6%，随后指数逼近 90% 上限 */
function calcProgress(elapsedMs: number): number {
  if (elapsedMs < 4_000) return (elapsedMs / 4_000) * 6;
  const t = (elapsedMs - 4_000) / 1_000;
  return Math.min(90, 6 + 84 * (1 - Math.exp(-t / 80)));
}

/** notloaded: 未加载态 | loading: 装载中 | ready: 已就绪（常驻） */
type Phase = 'notloaded' | 'loading' | 'ready';

interface EngineStatus {
  state?: string;
  model?: string;
}

export default function DialogWarmupBar() {
  const [phase, setPhase] = useState<Phase>('notloaded');
  const [engineState, setEngineState] = useState('');
  const [readyModel, setReadyModel] = useState('');
  const [now, setNow] = useState(() => Date.now());
  const startRef = useRef(0);

  /* 状态轮询：dialog 引擎状态主信号 + vllm 进程阶段 + 预热弹窗进行中 */
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
      const est = (st as EngineStatus | null)?.state || '';
      const isReady = est === 'ready';
      const vllmBusy = Boolean(
        v && ((v as { running?: boolean }).running
          || (v as { booting?: boolean }).booting),
      );
      const warmupActive = ws.visible && ws.kind === 'dialog' && !ws.done;
      // 2026-09-08 用户报「切换模型进度条不出来」：vl/text/gguf 后端装载
      // 不经过 vLLM 子进程（vllmBusy 恒 false），/dialog/status 此时返回
      // "loading"（预热装载态）而非 vLLM 专属的 "booting"——loading 一并
      // 纳入装载判定
      const loading = !isReady
        && (vllmBusy || warmupActive || est === 'booting' || est === 'loading');

      setEngineState(est);
      if (isReady) {
        setReadyModel((st as EngineStatus).model || '');
        setPhase('ready');
      } else {
        setPhase((prev) => {
          if (loading) {
            if (prev !== 'loading') {
              // 预热弹窗先于本行挂载时沿用其起点（进度连续，不重跳）
              startRef.current = ws.visible && ws.kind === 'dialog'
                && ws.startedAt > 0 ? ws.startedAt : Date.now();
            }
            return 'loading';
          }
          return 'notloaded';
        });
      }
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

  /* 已就绪模型友好名（清单未拉到时退回裸 id） */
  const modelOptions = useDialogStore((s) => s.modelOptions);
  const readyName = readyModel
    ? modelOptions.find((o) => o.model_id === readyModel)?.name ?? readyModel
    : '';

  /* ── 未加载 / 已就绪：单行常驻态（无进度轨道） ── */
  if (phase !== 'loading') {
    const ready = phase === 'ready';
    let text: string;
    if (ready) {
      text = readyName ? `已就绪 · ${readyName}` : '已就绪';
    } else if (engineState === 'sleeping') {
      text = '已为其他功能让渡显存 · 发送消息时自动恢复';
    } else if (engineState === 'error') {
      text = '上次加载未成功 · 发送消息时将自动重试';
    } else {
      text = '模型未加载 · 发送消息时将自动加载';
    }
    return (
      <div
        className="rp-row"
        data-testid="dialog-warmup-bar"
        data-state={phase}
        style={{
          flexDirection: 'row', alignItems: 'center', gap: 8,
          padding: '6px 10px',
          borderRadius: 8,
          background: 'var(--color-input-bg)',
          fontSize: 12,
          color: ready ? 'var(--color-success)' : 'var(--color-warning)',
        }}
      >
        <span
          aria-hidden="true"
          style={{
            width: 8, height: 8, borderRadius: 999, flexShrink: 0,
            background: ready ? 'var(--color-success)' : 'var(--color-warning)',
          }}
        />
        <span className="rp-value" style={{ fontSize: 12 }}>
          {text}
        </span>
      </div>
    );
  }

  /* ── 装载中：进度条（时间渐近，不谎报） ── */
  // Math.max 钳制：进入 loading 的首帧 now 可能还是挂载时的旧值，
  // 起点晚于它会产生负耗时（PaintWarmupBar 曾实测首帧显示 -52%）
  const elapsed = Math.max(0, now - startRef.current);
  const progress = calcProgress(elapsed);
  const secs = Math.max(0, Math.floor(elapsed / 1_000));

  return (
    <div
      className="rp-row"
      style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}
      data-testid="dialog-warmup-bar"
      data-state="loading"
    >
      <div className="flex items-center justify-between" style={{ gap: 8 }}>
        <span className="rp-label">模型加载中</span>
        <span
          className="rp-value"
          style={{ fontVariantNumeric: 'tabular-nums', fontSize: 11 }}
        >
          {Math.floor(secs / 60)}:{String(secs % 60).padStart(2, '0')} · {Math.round(progress)}%
        </span>
      </div>
      <div
        className="w-full"
        style={{ height: 6, borderRadius: 999, background: 'var(--color-input-bg)', overflow: 'hidden' }}
        role="progressbar"
        aria-label="对话模型加载进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(progress)}
      >
        <div
          style={{
            width: `${Math.max(2, progress)}%`,
            height: '100%',
            borderRadius: 999,
            background: 'var(--color-primary)',
            transition: 'width 250ms linear',
          }}
        />
      </div>
      <div className="rp-hint" style={{ fontSize: 11 }}>
        正在加载对话模型（约 2-3 分钟），期间发送的消息将自动排队，就绪后立即回复
      </div>
    </div>
  );
}
