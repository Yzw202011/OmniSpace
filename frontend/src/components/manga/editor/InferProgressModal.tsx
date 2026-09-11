// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * InferProgressModal.tsx —— 漫剧编辑器·角色推理进度弹窗（2026-08-24）
 * --------------------------------------------------------------------------
 * 点击「角色推理」后弹出：告知用户推理已启动 + 真实进度条 + 预计时间。
 * 后端 infer-entities 各推理阶段（聚合 → 逐块提取实体名 → 时代背景 →
 * 角色/场景/道具四层设定 → 写入资产库）实时上报进度注册表，
 * 本组件 1s 轮询 GET /comic/asset/infer-progress 展示。
 * ETA：percent≥10 线性外推（自适应实际速度），早期用静态估算。
 * 运行中可「后台运行」（推理继续，完成回调仍触发父组件刷新）。
 * ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import { Check, Circle, Clock, Loader2, TriangleAlert, Users } from 'lucide-react';
import { Modal } from '../../common/Modal';
import { getInferProgress, inferEntities } from '@/services/mangaApi';
import type { InferProgress } from '@/services/mangaApi';
import { getErrorMessage, reportBgError } from '@/utils/errors';

/** 弹窗阶段 */
type Phase = 'running' | 'done' | 'error';

/** 推理阶段定义（顺序 = 后端 stage key 流转顺序；跳过的阶段自动越过） */
const STAGES: { key: string; label: string }[] = [
  { key: 'aggregate', label: '聚合分镜实体列' },
  { key: 'extract_names', label: '提取实体名称（角色/场景/道具）' },
  { key: 'vet_props', label: '裁定道具资产价值（过滤无效道具）' },
  { key: 'era', label: '提取时代背景' },
  { key: 'char_settings', label: '生成角色四层设定' },
  { key: 'scene_settings', label: '生成场景四层设定' },
  { key: 'prop_settings', label: '生成道具四层设定' },
  { key: 'save', label: '写入资产库' },
];

/** 推理结果（与 inferEntities 响应同形） */
type InferResult = Awaited<ReturnType<typeof inferEntities>>;

export interface InferProgressModalProps {
  projectId: string;
  /** 推理成功回调（父组件刷新资产/分镜行；弹窗保留显示结果） */
  onComplete: (res: InferResult) => void;
  /** 推理失败回调（父组件复位防重入标记） */
  onError: (message: string) => void;
  onClose: () => void;
}

/** 秒数 → 中文时长 */
function fmtSec(s: number): string {
  if (s < 60) return `${Math.round(s)} 秒`;
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return sec > 0 ? `${m} 分 ${sec} 秒` : `${m} 分钟`;
}

export default function InferProgressModal({
  projectId,
  onComplete,
  onError,
  onClose,
}: InferProgressModalProps) {
  const [phase, setPhase] = useState<Phase>('running');
  const [prog, setProg] = useState<InferProgress | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [counts, setCounts] = useState({ character: 0, scene: 0, prop: 0 });
  /** 卸载保护：后台运行关闭弹窗后，promise 回调不再 setState 本组件 */
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    // 主推理请求（后台运行时弹窗卸载，promise 仍在，回调照常驱动父组件）
    inferEntities(projectId)
      .then((res) => {
        if (!alive.current) return;
        const c = { character: 0, scene: 0, prop: 0 };
        for (const it of res.items) {
          if (it.kind === 'character' || it.kind === 'scene' || it.kind === 'prop') c[it.kind] += 1;
        }
        setCounts(c);
        setPhase('done');
        onComplete(res);
      })
      .catch((err) => {
        const msg = getErrorMessage(err, '角色推理失败');
        if (!alive.current) return;
        setErrorMsg(msg);
        setPhase('error');
        onError(msg);
      });
    // 进度轮询（1s；单次失败静默上报，主 promise 负责成败判定）
    const timer = setInterval(() => {
      if (!alive.current) return;
      getInferProgress(projectId)
        .then((p) => {
          if (alive.current) setProg(p);
        })
        .catch((err) => reportBgError('InferProgressModal.poll', err));
    }, 1000);
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
    // onComplete/onError 由父组件 useCallback 稳定；projectId 变化即重开
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  /** 阶段列表状态：当前及之前 = 完成，当前高亮，之后待办 */
  const currentIdx = prog ? STAGES.findIndex((s) => s.key === prog.stage) : -1;
  const effIdx = phase === 'done' ? STAGES.length - 1 : currentIdx;
  const percent = phase === 'done' ? 100 : Math.floor(prog?.percent ?? 0);
  const etaText =
    prog?.eta_seconds != null && phase === 'running'
      ? `预计剩余 ${fmtSec(prog.eta_seconds)}`
      : phase === 'running'
        ? '预计剩余 估算中…'
        : '';
  const totalNew = counts.character + counts.scene + counts.prop;

  return (
    <Modal
      title={
        <span className="flex items-center gap-2">
          <Users size={16} style={{ color: 'var(--color-primary)' }} />
          角色推理
          {phase === 'running' && (
            <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', fontWeight: 400 }}>
              正在从剧本系统性提取资产，请勿关闭页面
            </span>
          )}
        </span>
      }
      onClose={() => {
        if (phase === 'running') return; // 运行中点 × 不关（走「后台运行」）
        onClose();
      }}
      maskClosable={phase !== 'running'}
      width={480}
      footer={
        phase === 'running' ? (
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            后台运行
          </button>
        ) : (
          <button type="button" className="btn btn-primary" onClick={onClose}>
            {phase === 'done' ? '完成' : '关闭'}
          </button>
        )
      }
    >
      <div className="flex flex-col gap-3">
        {/* ① 进度头：百分比 + 当前阶段 */}
        <div className="infer-progress-head">
          <div className="flex flex-col" style={{ gap: 2 }}>
            <span className="infer-stage-label">{phase === 'running' ? prog?.stage_label ?? '启动中…' : phase === 'done' ? '推理完成' : '推理失败'}</span>
            <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
              {phase === 'running' ? prog?.detail ?? '正在准备…' : phase === 'done' ? (prog?.message ?? '') : errorMsg}
            </span>
          </div>
          <span className={`infer-percent${phase === 'error' ? ' error' : ''}`}>{percent}%</span>
        </div>

        {/* ② 进度条（CSS transition 平滑推进，单块推理停顿期不僵死） */}
        <div className="manga-video-prog infer-prog-bar">
          <span className="manga-video-prog-bar" style={{ width: '100%', transform: `scaleX(${Math.min(100, Math.max(0, percent)) / 100})`, transformOrigin: 'left center', transition: 'transform .8s ease' }} />
          <span className="manga-video-prog-text">{percent}%</span>
        </div>

        {/* ③ 时间信息：已用 + 预计剩余 */}
        <div className="infer-meta">
          <span className="flex items-center gap-1">
            <Clock size={12} />
            已用 {fmtSec(prog?.elapsed_seconds ?? 0)}
          </span>
          {etaText && <span>{etaText}</span>}
        </div>

        {/* ④ 阶段清单 */}
        <div className="infer-stage-list">
          {STAGES.map((s, i) => {
            const done = phase === 'done' || (effIdx >= 0 && i < effIdx);
            const active = phase === 'running' && i === effIdx;
            return (
              <div key={s.key} className={`infer-stage-item${done ? ' done' : ''}${active ? ' active' : ''}`}>
                <span className="stage-ico">
                  {done ? (
                    <Check size={14} style={{ color: 'var(--color-success)' }} />
                  ) : active ? (
                    <Loader2 size={14} style={{ animation: 'spin 1s linear infinite', color: 'var(--color-primary-300)' }} />
                  ) : (
                    <Circle size={10} style={{ opacity: 0.35 }} />
                  )}
                </span>
                <span className="ellipsis">{s.label}</span>
                {active && prog?.detail && <span className="text-tertiary stage-detail ellipsis">{prog.detail}</span>}
              </div>
            );
          })}
        </div>

        {/* ⑤ 结果汇总 / 错误 */}
        {phase === 'done' && (
          <div className="infer-result">
            {totalNew > 0 ? (
              <>
                <span className="badge success">新增角色 {counts.character}</span>
                <span className="badge success">新增场景 {counts.scene}</span>
                <span className="badge success">新增道具 {counts.prop}</span>
              </>
            ) : (
              <span className="text-tertiary" style={{ fontSize: 'var(--font-size-sm)' }}>
                未发现新实体，资产清单已是最新
              </span>
            )}
          </div>
        )}
        {phase === 'error' && (
          <div className="infer-result">
            <span className="flex items-center gap-2" style={{ color: 'var(--color-error)', fontSize: 'var(--font-size-sm)' }}>
              <TriangleAlert size={14} />
              {errorMsg}
            </span>
          </div>
        )}
      </div>
    </Modal>
  );
}
