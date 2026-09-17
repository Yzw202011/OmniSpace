/* ==========================================================================
 * TrainingPage.tsx —— 训练中心（P1，2026-09-17 用户拍板 2A）
 * --------------------------------------------------------------------------
 * 所有训练一个屋檐下（终结 LoRA 能力散在知识学习页/风格页两处的割裂）：
 *   1. 统一任务队列（顶部）：GET /training/tasks 聚合知识+风格两源，
 *      5s 轮询 + 卸载清理；单源故障 fail-soft 如实披露（不伪装空队）；
 *   2. 页签：知识训练（LearnView + LoRA 版本管理）/ 风格训练（StylePage
 *      embedded）/ 人物训练（P3 落地，当前占位如实现状）。
 * 知识学习页随本页上线瘦身（训练区块整体迁入，会话/主题留守原页）。
 * ========================================================================== */
import React, { useCallback, useEffect, useState } from 'react';
import { BrainCircuit, RefreshCw } from 'lucide-react';

import CharacterTrainPanel from '@/components/training/CharacterTrainPanel';
import LearnView from '@/components/learn/LearnView';
import LoRAVersionManager from '@/components/learning/LoRAVersionManager';
import { StylePage } from '@/components/style/StylePage';
import {
  fetchUnifiedTasks,
  type UnifiedQueue,
} from '@/services/trainingApi';

type Tab = 'knowledge' | 'style' | 'character';

/** 统一队列显示上限（防历史任务刷屏；完整历史在各页签内） */
const QUEUE_DISPLAY_CAP = 8;

const TABS: { key: Tab; label: string; hint: string }[] = [
  { key: 'knowledge', label: '知识训练', hint: '喂文档，让对话更懂你（QLoRA）' },
  { key: 'style', label: '风格训练', hint: '喂素材，练视频画风 LoRA' },
  { key: 'character', label: '人物训练', hint: '喂角色图（主图+四视图）练专属脸蛋 LoRA' },
];

/** 状态 → 语义色令牌（芯片左条） */
const STATUS_COLOR: Record<string, string> = {
  running: 'var(--color-success)',
  training: 'var(--color-success)',
  evaluating: 'var(--color-success)',
  queued: 'var(--color-warning)',
  pending: 'var(--color-warning)',
  done: 'var(--color-primary)',
  error: 'var(--color-error)',
};

const STATUS_TEXT: Record<string, string> = {
  running: '训练中', training: '训练中', evaluating: '评估中',
  queued: '排队中', pending: '排队中', done: '已完成', error: '失败',
};

const UnifiedQueueCard: React.FC<{
  queue: UnifiedQueue | null;
  error: string | null;
  loading: boolean;
  onRefresh: () => void;
}> = ({ queue, error, loading, onRefresh }) => {
  const items = queue?.items ?? [];
  const sources = queue?.sources ?? {};
  const downSource = Object.entries(sources).find(([, v]) => !v.ok);
  return (
    <section className="card" aria-label="统一训练队列">
      <div className="flex items-center justify-between mb-2">
        <h3 className="card-title">
          <BrainCircuit size={16} aria-hidden="true" /> 统一任务队列
          {items.length > 0 && (
            <span className="text-xs ml-2" style={{ color: 'var(--color-text-tertiary)' }}>
              {items.length} 项
            </span>
          )}
        </h3>
        <button
          type="button"
          className="btn-ghost btn-sm"
          onClick={onRefresh}
          disabled={loading}
          aria-label="刷新训练队列"
        >
          <RefreshCw size={14} aria-hidden="true" /> 刷新
        </button>
      </div>

      {error && (
        <p className="text-sm" style={{ color: 'var(--color-error)' }} role="alert">
          队列拉取失败：{error}
        </p>
      )}
      {downSource && (
        <p className="text-sm" style={{ color: 'var(--color-warning)' }}>
          「{downSource[0] === 'style' ? '风格' : '知识'}」源暂不可用（{downSource[1].error ?? '未知原因'}），
          其余任务照常显示。
        </p>
      )}
      {!error && items.length === 0 && (
        <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
          暂无训练任务。选一个页签，上传材料开始训练。
        </p>
      )}
      <ul className="training-queue">
        {items.slice(0, QUEUE_DISPLAY_CAP).map((t) => (
          <li key={`${t.kind}:${t.id}`} className="training-queue-item">
            <span
              className="training-kind-chip"
              style={{
                borderLeftColor:
                  t.kind === 'knowledge'
                    ? 'var(--color-primary)'
                    : 'var(--color-accent)',
              }}
            >
              {t.kindLabel}
            </span>
            <span className="training-queue-name" title={t.name}>{t.name}</span>
            <span
              className="training-queue-status"
              style={{ color: STATUS_COLOR[t.status] ?? 'var(--color-text-tertiary)' }}
            >
              {STATUS_TEXT[t.status] ?? t.status}
            </span>
            <span
              className="training-queue-progress"
              role="progressbar"
              aria-valuenow={Math.round(t.progress * 100)}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={`${t.name} 进度`}
            >
              <span
                className="training-queue-progress-fill"
                style={{
                  transform: `scaleX(${Math.min(Math.max(t.progress, 0), 1)})`,
                  background:
                    STATUS_COLOR[t.status] ?? 'var(--color-primary)',
                }}
              />
            </span>
            <span className="training-queue-pct">{Math.round(t.progress * 100)}%</span>
          </li>
        ))}
      </ul>
      {items.length > QUEUE_DISPLAY_CAP && (
        <p className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
          仅显示最近 {QUEUE_DISPLAY_CAP} 项（共 {items.length} 项；完整历史在对应页签里看）
        </p>
      )}
    </section>
  );
};

export const TrainingPage: React.FC = () => {
  const [tab, setTab] = useState<Tab>('knowledge');
  const [queue, setQueue] = useState<UnifiedQueue | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const q = await fetchUnifiedTasks();
      setQueue(q);
      setQueueError(null);
    } catch (e) {
      setQueueError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer); // 卸载清理（useEffect 铁律）
  }, [refresh]);

  return (
    <div className="page">
      <h1 className="page-title">
        <BrainCircuit size={20} aria-hidden="true" /> 训练中心
      </h1>
      <p className="page-subtitle">
        所有训练一个屋檐下：知识 · 风格 · 人物
      </p>

      <UnifiedQueueCard
        queue={queue}
        error={queueError}
        loading={loading}
        onRefresh={() => void refresh()}
      />

      <div className="training-tabs" role="tablist" aria-label="训练类型">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            disabled={false}
            title={t.hint}
            className={`training-tab${tab === t.key ? ' active' : ''}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}

          </button>
        ))}
      </div>

      <div className="training-tab-body" role="tabpanel">
        {tab === 'knowledge' && (
          <>
            <LearnView />
            <LoRAVersionManager />
          </>
        )}
        {tab === 'style' && <StylePage embedded />}
        {tab === 'character' && <CharacterTrainPanel />}
      </div>
    </div>
  );
};

export default TrainingPage;
