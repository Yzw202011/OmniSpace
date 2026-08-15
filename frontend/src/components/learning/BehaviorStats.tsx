/* ==========================================================================
 * BehaviorStats.tsx —— 行为学习面板（TASK-036）
 * --------------------------------------------------------------------------
 * 显示：已记录操作数 / 偏好模型状态 / 下次微调时间 / 最近学习摘要
 * 操作：[查看日志] [立即微调] [重置]
 * 数据：GET /v1/behavior/stats、POST /v1/behavior/clear、POST /v1/learn/train
 * ========================================================================== */

import React, { useEffect, useState } from 'react';
import { useLearningStore } from '@/stores/useLearningStore';
import { useLearnStore } from '@/stores/useLearnStore';
import { useAppStore } from '@/stores/useAppStore';
import { formatNumber, formatDateTime } from '@utils/format';
import Modal from '@/components/common/Modal';

export const BehaviorStatsPanel: React.FC = () => {
  const stats = useLearningStore((s) => s.behaviorStats);
  const fetchBehaviorStats = useLearningStore((s) => s.fetchBehaviorStats);
  const clearBehavior = useLearningStore((s) => s.clearBehavior);
  const logs = useLearningStore((s) => s.logs);
  const fetchLogs = useLearningStore((s) => s.fetchLogs);
  const createTrain = useLearnStore((s) => s.createTrain);
  const showToast = useAppStore((s) => s.showToast);

  const [showLogs, setShowLogs] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);

  useEffect(() => {
    fetchBehaviorStats();
    const timer = setInterval(fetchBehaviorStats, 10000);
    return () => clearInterval(timer);
  }, [fetchBehaviorStats]);

  /** 立即微调：触发一次 knowledge_lora 训练（功能互斥由 useLearnStore 处理） */
  const handleFinetune = async () => {
    const ok = await createTrain({
      type: 'knowledge_lora',
      name: `行为偏好微调 ${formatDateTime(Date.now())}`,
      priority: 'manual',
    });
    if (ok) showToast('微调任务已加入队列', 'success');
  };

  return (
    <section className="card hoverable" aria-label="行为学习">
      <div className="flex items-center justify-between mb-3">
        <h3 className="card-title" style={{ marginBottom: 0 }}>🧠 行为学习</h3>
        <div className="flex gap-2">
          <button className="btn btn-ghost btn-sm" onClick={() => { setShowLogs(true); fetchLogs(); }}>
            📋 查看日志
          </button>
          <button className="btn btn-secondary btn-sm" onClick={handleFinetune}>⚡ 立即微调</button>
          <button className="btn btn-danger btn-sm" onClick={() => setConfirmReset(true)}>🗑 重置</button>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div>
          <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>已记录操作数</div>
          <div className="mono" style={{ fontSize: 'var(--font-size-lg)' }}>
            {stats ? formatNumber(stats.event_count) : '--'}
          </div>
        </div>
        <div>
          <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>偏好模型状态</div>
          <div className="text-sm">{stats?.preference_status || '学习中'}</div>
        </div>
        <div>
          <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>下次微调</div>
          <div className="text-sm">{stats?.next_finetune_at ? formatDateTime(stats.next_finetune_at) : '按计划自动'}</div>
        </div>
        <div>
          <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>最近学习摘要</div>
          <div className="text-sm" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {stats?.recent_summary || '暂无摘要'}
          </div>
        </div>
      </div>
      {!stats && (
        <div className="text-tertiary mt-3" style={{ fontSize: 'var(--font-size-xs)' }}>
          行为统计接口（/v1/behavior/stats）未就绪时显示默认值，不影响其余功能。
        </div>
      )}

      {showLogs && (
        <Modal title="行为学习日志" onClose={() => setShowLogs(false)} width={640}>
          {logs.length === 0 ? (
            <div className="text-secondary text-sm">暂无日志记录</div>
          ) : (
            <div className="flex flex-col gap-1 mono" style={{ fontSize: 'var(--font-size-xs)' }}>
              {logs.map((log, i) => <div key={i}>{log.message}</div>)}
            </div>
          )}
        </Modal>
      )}

      {confirmReset && (
        <Modal
          title="重置行为数据"
          onClose={() => setConfirmReset(false)}
          footer={(
            <>
              <button className="btn btn-ghost" onClick={() => setConfirmReset(false)}>取消</button>
              <button
                className="btn btn-danger"
                onClick={() => { clearBehavior(); setConfirmReset(false); }}
              >
                确认重置
              </button>
            </>
          )}
        >
          <p className="text-sm text-secondary">
            将清空全部行为事件记录与偏好模型，该操作不可恢复。确定继续吗？
          </p>
        </Modal>
      )}
    </section>
  );
};

export default BehaviorStatsPanel;
