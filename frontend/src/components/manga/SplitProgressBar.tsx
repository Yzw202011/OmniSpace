/* ==========================================================================
 * OmniSpace AI v2.5.0 —— AI 切分进度条（2026-08-23）
 * --------------------------------------------------------------------------
 * 长剧本 AI 镜头级切分耗时 1~5 分钟，disabled 按钮无反馈 → 加进度条。
 * 进度口径（诚实进度，不谎报）：
 *   - 多块剧本（>1500×2 字）：后端逐块上报真实段级进度（3s 轮询
 *     GET auto-split/progress），percent = 已完成块/总块
 *   - 单块剧本/后端无数据：时间渐近曲线逼近 90% 上限
 *     （同 WarmupModal 先例：0-4s 线性到 6%，此后指数逼近 90%）
 *   - 完成由父组件 splitting=false 卸载本组件（请求 resolve 驱动）
 * 视觉：主色恒定进度条（不复用 Progress 组件——其为负载语义色，
 * 60% 黄 85% 红会误导加载进度）。
 * ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import { Hourglass } from 'lucide-react';
import { useMangaStore } from '@/stores/useMangaStore';
import { getSplitProgress } from '@/services/mangaApi';

/** 轮询间隔（ms） */
const POLL_MS = 3000;
/** 计时 ticker 间隔（ms） */
const TICK_MS = 250;
/** 超出预期阈值（ms）：文案切换为「较慢仍在进行」 */
const SLOW_MS = 300_000;

/** 时间渐近进度（%）：0-4s 线性到 6%，随后指数逼近 90% 上限 */
function calcProgress(elapsedMs: number): number {
  if (elapsedMs < 4_000) return (elapsedMs / 4_000) * 6;
  const t = (elapsedMs - 4_000) / 1_000;
  return Math.min(90, 6 + 84 * (1 - Math.exp(-t / 80)));
}

interface SplitProgressBarProps {
  /** 切分进行中（true 时轮询+渲染，false 卸载） */
  active: boolean;
  /** 切分发起时刻（ms，父组件点击时 Date.now()） */
  startedAt: number;
}

export function SplitProgressBar({ active, startedAt }: SplitProgressBarProps) {
  const projectId = useMangaStore((s) => s.currentProject?.id ?? '');
  const [now, setNow] = useState(() => Date.now());
  /** 后端真实块级进度（active=false 时保留最后一次读数） */
  const [info, setInfo] = useState<{
    blocksDone: number;
    blocksTotal: number;
    mode: string;
    etaMinutes: number;
  } | null>(null);
  const infoRef = useRef(info);
  infoRef.current = info;

  // 计时 ticker（渐近曲线驱动）
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, [active]);

  // 后端进度轮询（多块剧本真实段级进度）
  useEffect(() => {
    if (!active || !projectId) return;
    let stopped = false;
    const poll = async () => {
      try {
        const res = await getSplitProgress(projectId);
        if (!stopped && res.active && res.blocksTotal > 1) {
          setInfo({
            blocksDone: res.blocksDone,
            blocksTotal: res.blocksTotal,
            mode: res.mode,
            etaMinutes: res.etaMinutes,
          });
        } else if (!stopped && res.active && res.mode === 'cpu') {
          // 单块剧本也上报 CPU 慢速路径警告
          setInfo({ blocksDone: 0, blocksTotal: 1, mode: res.mode, etaMinutes: res.etaMinutes });
        }
      } catch {
        /* silent-intent: 轮询失败静默（渐近曲线兜底） */
      }
    };
    void poll();
    const timer = setInterval(poll, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [active, projectId]);

  if (!active) return null;

  const elapsed = now - startedAt;
  const secs = Math.floor(elapsed / 1_000);
  const slowMode = info?.mode === 'cpu';
  const hasBlocks = info !== null && info.blocksTotal > 1;
  // 真实块进度为主（不低于 4% 起步感）；渐近曲线兜底；CPU 慢速路径按块推进
  const progress = hasBlocks && info
    ? Math.max((info.blocksDone / info.blocksTotal) * 100, 4)
    : calcProgress(elapsed);
  const phaseText = slowMode
    ? `显存不足 → CPU 慢速路径，预计约 ${info?.etaMinutes ?? '?'} 分钟（释放显存后重试可提速数倍：关闭占显存的大型软件，或重启后端清理模型驻留显存）`
    : hasBlocks && info
      ? `AI 切分中 · 已完成 ${info.blocksDone}/${info.blocksTotal} 段`
      : elapsed > SLOW_MS
        ? '本次切分较慢（GPU 繁忙），仍在进行…'
        : elapsed < 5_000
          ? '正在准备切分（模型加载/文本分块）…'
          : 'AI 镜头级切分中（模型推理，按剧本长度 1~5 分钟）…';

  return (
    <div className="mt-3" role="status" aria-label="AI 切分进度">
      <div
        className="w-full h-2 rounded-full bg-[var(--color-input-bg)] overflow-hidden"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(progress)}
      >
        <div
          className="h-full rounded-full transition-[width] duration-300 ease-out"
          style={{ width: `${progress}%`, backgroundColor: 'var(--color-primary)' }}
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
  );
}

export default SplitProgressBar;
// 本项目仅供学习使用，商业授权请+Q 3559331368
