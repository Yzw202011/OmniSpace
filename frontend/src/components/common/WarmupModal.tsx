/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 对话模型冷启动进度弹窗（2026-08-23）
 * --------------------------------------------------------------------------
 * 替代原一次性 toast：进入对话页点火预热后弹出，含时间渐近进度条。
 * 后端无细粒度加载进度接口 → 进度 = 已耗时曲线逼近 90% 上限，
 * GET /dialog/status state=ready 时校正跳 100%（诚实进度，不谎报）。
 * 阶段文案细化参考 /models/vllm/status（running/booting）。
 * 行为契约：
 *   - 「后台继续」/ESC/右上角×：仅关弹窗，后台加载不中断
 *   - 就绪：成功样式 1.5s 后自动关闭 + toast 提醒
 *   - 被模块切换终止（state 非 ready 且 vllm 进程消失且 t>45s）：静默关闭
 * ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import { Loader2, CircleCheck, Hourglass } from 'lucide-react';
import Modal from './Modal';
import { useWarmupStore } from '../../stores/useWarmupStore';
import { useAppStore } from '../../stores/useAppStore';
import { getDialogEngineStatus } from '../../services/dialogApi';
import { getVllmStatus } from '../../services/modelApi';

/** 冷启动预估总时长下限（ms）：vLLM 实测 ~157s，文案口径「2-3 分钟」 */
const EXPECTED_MS = 150_000;
/** 超出预期阈值（ms）：文案切换为「较慢仍在进行」，不关闭 */
const SLOW_MS = 360_000;
/** 判定预热被终止的最早时刻（ms）：state 非 ready 且 vllm 无进程 */
const ABORT_AFTER_MS = 45_000;
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

export function WarmupModal() {
  const { visible, kind, done, startedAt, modelId, finish, dismiss } = useWarmupStore();
  const showToast = useAppStore((s) => s.showToast);
  const [now, setNow] = useState(() => Date.now());
  /** vllm 进程存活（booting 或 running，阶段文案用） */
  const vllmAliveRef = useRef(false);
  const aliveStreakRef = useRef(0);
  /** 仅服务对话模块预热（paint 走 PaintWarmupModal，同 store 分流）。
   * 早退判定只能放在全部 hooks 之后：条件 return 提前会破坏 Hook
   * 恒定调用次序 → store 切到 paint 时整页崩（React #300，
   * 2026-08-31 绘画页崩溃根因） */
  const isPaint = kind === 'paint';

  /* 进度 ticker：挂载期间 250ms 重算已耗时 */
  useEffect(() => {
    if (!visible || done || isPaint) return;
    const id = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, [visible, done, isPaint]);

  /* 就绪轮询：/dialog/status 主信号 + /models/vllm/status 阶段细化 */
  useEffect(() => {
    if (!visible || done || isPaint) return;
    let cancelled = false;
    async function poll() {
      const elapsed = Date.now() - startedAt;
      try {
        const st = await getDialogEngineStatus();
        if (cancelled) return;
        if (st.state === 'ready') {
          finish();
          showToast('对话模型已就绪，可以开始对话', 'success');
          return;
        }
        /* vllm 阶段细化（失败不阻塞：非 vllm 后端无进程属正常） */
        try {
          const v = await getVllmStatus();
          vllmAliveRef.current = Boolean(v && (v.running || v.booting));
        } catch {
          vllmAliveRef.current = false;
        }
        /* 终止判定：state 停留非 ready 且 vllm 进程消失（模块切换释放/
         * 加载失败）：t>45s 后连续 3 次 → 预热已终止，静默关闭弹窗
         * （发消息时后端会如实报错，弹窗不谎报进度） */
        if (elapsed > ABORT_AFTER_MS && !vllmAliveRef.current) {
          aliveStreakRef.current += 1;
          if (aliveStreakRef.current >= 3) {
            dismiss();
            return;
          }
        } else {
          aliveStreakRef.current = 0;
        }
      } catch {
        /* 后端瞬时不可达：下轮重试 */
      }
    }
    void poll();
    const id = window.setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [visible, done, isPaint, startedAt, finish, dismiss, showToast]);

  /* 就绪后 1.5s 自动关闭 */
  useEffect(() => {
    if (!done) return;
    const id = window.setTimeout(dismiss, 1_500);
    return () => window.clearTimeout(id);
  }, [done, dismiss]);

  if (!visible || isPaint) return null;

  const elapsed = done ? EXPECTED_MS : now - startedAt;
  const progress = done ? 100 : calcProgress(elapsed);
  const secs = Math.floor(elapsed / 1_000);
  const phaseText = done
    ? '模型已就绪，可以开始对话'
    : elapsed > SLOW_MS
      ? '本次加载较慢（显存紧张或磁盘繁忙），仍在后台进行…'
      : elapsed < 5_000
        ? '正在清理显存资源…'
        : vllmAliveRef.current
          ? '正在加载模型权重、编译推理内核（耗时大头，约 2-3 分钟）'
          : '正在加载对话模型…';

  return (
    <Modal
      title="对话模型冷启动"
      width={440}
      closable={!done}
      maskClosable={false}
      onClose={dismiss}
      footer={
        done ? undefined : (
          <button
            type="button"
            onClick={dismiss}
            className="px-4 py-2 text-sm rounded-lg border border-[var(--color-border-light)] text-[var(--color-text-secondary)] hover:bg-[var(--color-divider)] transition-colors"
          >
            后台继续
          </button>
        )
      }
    >
      <div className="flex flex-col gap-4 py-1">
        <div className="flex items-start gap-3">
          {done ? (
            <CircleCheck size={22} className="shrink-0 text-[var(--color-success)]" aria-hidden="true" />
          ) : (
            <Loader2 size={22} className="shrink-0 animate-spin text-[var(--color-primary)]" aria-hidden="true" />
          )}
          <div className="flex-1">
            <p className="text-sm leading-relaxed text-[var(--color-text-primary)]">
              {done
                ? '模型加载完成，即将关闭…'
                : '首次加载约需 2-3 分钟，期间请勿切换页面（切换会中断加载）。现在发送的消息将自动排队，就绪后立即回复。'}
            </p>
            {modelId ? (
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
                目标模型：{modelId}
              </p>
            ) : null}
          </div>
        </div>

        {/* 主色进度条（区别于负载语义色 Progress 组件，加载进度恒主色） */}
        <div>
          <div
            className="w-full h-2 rounded-full bg-[var(--color-input-bg)] overflow-hidden"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress)}
          >
            <div
              className="h-full rounded-full transition-[width] duration-300 ease-out"
              style={{
                width: `${progress}%`,
                backgroundColor: done
                  ? 'var(--color-success)'
                  : 'var(--color-primary)',
              }}
            />
          </div>
          <div className="mt-1.5 flex items-center justify-between text-xs text-[var(--color-text-tertiary)] tabular-nums">
            <span>{phaseText}</span>
            <span className="flex items-center gap-1 shrink-0">
              <Hourglass size={12} aria-hidden="true" />
              {Math.floor(secs / 60)}:{String(secs % 60).padStart(2, '0')} · {Math.round(progress)}%
            </span>
          </div>
        </div>
      </div>
    </Modal>
  );
}

export default WarmupModal;
