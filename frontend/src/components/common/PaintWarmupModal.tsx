/* ==========================================================================
 * OmniSpace AI v2.3.1 —— AI 绘画模型冷启动进度弹窗（2026-08-31）
 * --------------------------------------------------------------------------
 * 与对话模块 WarmupModal 同构（用户需求「跟 AI 对话一样的冷启动弹窗」）：
 * 进入 AI 绘画页点火预热（POST /models/warmup feature=paint）后弹出，
 * 本地 diffusers 管线冷启动约 10~60s。进度 = 已耗时曲线逼近 90%，
 * GET /draw/status loaded=true 时校正跳 100%（诚实进度，不谎报）。
 * 行为契约（对齐 WarmupModal）：
 *   - 「后台继续」/ESC/右上角×：仅关弹窗，后台加载不中断
 *   - 就绪：成功样式 1.5s 后自动关闭 + toast 提醒
 *   - 引擎进入 error 态且持续（t>20s 连续 3 次）：静默关闭（生成时报错）
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { Loader2, CircleCheck, Hourglass } from 'lucide-react';
import Modal from './Modal';
import { useWarmupStore } from '../../stores/useWarmupStore';
import { useAppStore } from '../../stores/useAppStore';
import { getStatus as getDrawStatus } from '../../services/paintApi';

/** 冷启动预估总时长（ms）：diffusers 管线实测 10~60s，文案口径「0.5-1 分钟」 */
const EXPECTED_MS = 60_000;
/** 超出预期阈值（ms）：文案切换为「较慢仍在进行」，不关闭 */
const SLOW_MS = 150_000;
/** 引擎 error 态判定的最早时刻（ms）：此后连续 3 次仍 error → 静默关闭 */
const ABORT_AFTER_MS = 20_000;
/** 轮询间隔（ms） */
const POLL_MS = 2_000;
/** 进度 ticker 间隔（ms） */
const TICK_MS = 250;

/** 时间渐近进度（%）：0-4s 线性到 6%，随后指数逼近 90% 上限 */
function calcProgress(elapsedMs: number): number {
  if (elapsedMs < 4_000) return (elapsedMs / 4_000) * 6;
  const t = (elapsedMs - 4_000) / 1_000;
  return Math.min(90, 6 + 84 * (1 - Math.exp(-t / 30)));
}

interface PaintEngineStatus {
  loaded?: boolean;
  state?: string;
  model?: string;
}

export function PaintWarmupModal() {
  const { visible, kind, done, startedAt, modelId, finish, dismiss } = useWarmupStore();
  const showToast = useAppStore((s) => s.showToast);
  const [now, setNow] = useState(() => Date.now());
  const [engineError, setEngineError] = useState(false);

  /* 进度 ticker：挂载期间 250ms 重算已耗时 */
  useEffect(() => {
    if (!visible || kind !== 'paint' || done) return;
    const id = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, [visible, kind, done]);

  /* 就绪轮询：/draw/status loaded=true 主信号；error 态静默退出 */
  useEffect(() => {
    if (!visible || kind !== 'paint' || done) return;
    let cancelled = false;
    let errorStreak = 0;
    async function poll() {
      const elapsed = Date.now() - startedAt;
      try {
        const st = (await getDrawStatus()) as PaintEngineStatus;
        if (cancelled) return;
        if (st && (st.loaded === true || st.state === 'ready')) {
          finish();
          showToast('绘画模型已就绪，可以开始创作', 'success');
          return;
        }
        /* 加载被终止判定：引擎 error 态持续（模块切换释放/加载失败）：
         * t>20s 后连续 3 次 → 预热已终止，静默关闭（生成时会如实报错） */
        const errored = st ? st.state === 'error' : false;
        setEngineError(Boolean(errored));
        if (elapsed > ABORT_AFTER_MS && errored) {
          errorStreak += 1;
          if (errorStreak >= 3) {
            dismiss();
            return;
          }
        } else {
          errorStreak = 0;
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
  }, [visible, kind, done, startedAt, finish, dismiss, showToast]);

  /* 就绪后 1.5s 自动关闭 */
  useEffect(() => {
    if (!done) return;
    const id = window.setTimeout(dismiss, 1_500);
    return () => window.clearTimeout(id);
  }, [done, dismiss]);

  if (!visible || kind !== 'paint') return null;

  const elapsed = done ? EXPECTED_MS : now - startedAt;
  const progress = done ? 100 : calcProgress(elapsed);
  const secs = Math.floor(elapsed / 1_000);
  const phaseText = done
    ? '模型已就绪，可以开始创作'
    : elapsed > SLOW_MS
      ? '本次加载较慢（显存紧张或磁盘繁忙），仍在后台进行…'
      : elapsed < 5_000
        ? '正在清理显存资源…'
        : engineError
          ? '加载遇到错误，正在确认状态…'
          : '正在加载绘画模型权重到显存（约 0.5-1 分钟）';

  return (
    <Modal
      title="AI 绘画模型冷启动"
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
                : '首次加载约需 0.5-1 分钟。现在提交的生成请求将自动排队，就绪后立即开始绘制。'}
            </p>
            {modelId ? (
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
                目标模型：{modelId}
              </p>
            ) : null}
          </div>
        </div>

        {/* 主色进度条（加载进度恒主色，区别于负载语义色） */}
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

export default PaintWarmupModal;
// 本项目仅供学习使用，商业授权请+Q 3559331368
