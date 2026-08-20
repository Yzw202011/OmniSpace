/* ==========================================================================
 * LearningDashboard.tsx —— 实时学习状态卡片（TASK-036）
 * --------------------------------------------------------------------------
 * 显示：正在学习主题 / 当前页面 / 已浏览页数 / 已提取知识点 / 已用时
 * 操作：暂停 / 恢复 / 停止 / 查看浏览器 / 查看日志
 * 数据：GET /v1/learn/session/status（运行中 3 秒轮询）
 * ========================================================================== */

import React, { useEffect, useState } from 'react';
import { useLearningStore } from '@/stores/useLearningStore';
import { formatDuration, formatDateTime } from '@utils/format';
import { LEARN_SESSION_STATUS_LABELS } from '@/constants/statusLabels';
import Modal from '@/components/common/Modal';

import {
  Radar,
  FileText,
  Timer,
  PenLine,
  Clock,
  Bot,
  Pause,
  Play,
  Square,
  Eye,
  ScrollText,
} from 'lucide-react';

/** 会话状态 → 状态点颜色（标签文案走 LEARN_SESSION_STATUS_LABELS） */
const STATUS_DOT: Record<string, string> = {
  idle: 'var(--color-text-tertiary)',
  running: 'var(--color-success)',
  paused: 'var(--color-warning)',
  stopped: 'var(--color-text-tertiary)',
};

/** 状态点（替代 emoji 圆点，颜色随主题） */
const StatusDot: React.FC<{ color: string }> = ({ color }) => (
  <span
    aria-hidden="true"
    style={{
      display: 'inline-block',
      width: 8,
      height: 8,
      borderRadius: '50%',
      background: color,
      flexShrink: 0,
    }}
  />
);

export const LearningDashboard: React.FC = () => {
  const session = useLearningStore((s) => s.session);
  const logs = useLearningStore((s) => s.logs);
  const fetchSessionStatus = useLearningStore((s) => s.fetchSessionStatus);
  const fetchLogs = useLearningStore((s) => s.fetchLogs);
  const pauseSession = useLearningStore((s) => s.pauseSession);
  const resumeSession = useLearningStore((s) => s.resumeSession);
  const stopSession = useLearningStore((s) => s.stopSession);
  const toggleBrowser = useLearningStore((s) => s.toggleBrowser);
  const isBrowserVisible = useLearningStore((s) => s.isBrowserVisible);

  const [showLogs, setShowLogs] = useState(false);

  const status = session?.status ?? 'idle';
  const dotColor = STATUS_DOT[status] ?? STATUS_DOT.idle;
  const statusLabel = LEARN_SESSION_STATUS_LABELS[status] ?? status;
  const active = status === 'running' || status === 'paused';

  /* 运行/暂停中每 3 秒轮询一次会话状态 */
  useEffect(() => {
    fetchSessionStatus();
    if (!active) return;
    const timer = setInterval(fetchSessionStatus, 3000);
    return () => clearInterval(timer);
  }, [active, fetchSessionStatus]);

  const openLogs = () => {
    setShowLogs(true);
    fetchLogs();
  };

  return (
    <section className="card hoverable" aria-label="实时学习状态">
      <div className="flex items-center justify-between mb-3">
        <h3 className="card-title" style={{ marginBottom: 0 }}><Radar size={16} aria-hidden="true" /> 实时学习状态</h3>
        <span className="badge info"><StatusDot color={dotColor} /> {statusLabel}</span>
      </div>

      {active && session ? (
        <div className="flex flex-col gap-2 text-sm">
          <div className="inline-flex items-center gap-2">
            <StatusDot color={dotColor} /> 正在学习：<strong>{session.topic || '未命名主题'}</strong>
          </div>
          <div className="text-secondary inline-flex items-center gap-1.5">
            <FileText size={14} aria-hidden="true" className="shrink-0" /> 当前页面：
            {session.current_page?.title
              ? `${session.current_page.site ?? ''} - ${session.current_page.title}`
              : '（等待页面加载）'}
          </div>
          <div className="flex flex-wrap gap-4 text-secondary">
            <span className="inline-flex items-center gap-1.5"><Timer size={14} aria-hidden="true" /> 已浏览：{session.pages_visited ?? 0}页/{session.max_pages ?? '--'}页</span>
            <span className="inline-flex items-center gap-1.5"><PenLine size={14} aria-hidden="true" /> 已提取：{session.knowledge_extracted ?? 0}个知识点</span>
            <span className="inline-flex items-center gap-1.5">
              <Clock size={14} aria-hidden="true" /> 已用时：{formatDuration(session.elapsed_sec ?? 0)}
              {session.max_duration_sec ? ` / ${formatDuration(session.max_duration_sec)}` : ''}
            </span>
          </div>
          {session.current_action && (
            <div className="text-tertiary inline-flex items-center gap-1.5" style={{ fontSize: 'var(--font-size-xs)' }}>
              <Bot size={13} aria-hidden="true" /> AI 操作：{session.current_action}
            </div>
          )}
        </div>
      ) : (
        <div className="text-secondary text-sm">
          当前没有进行中的学习会话。可在下方「学习主题管理」中选择主题开始学习，或让 AI 自主决定学习内容。
        </div>
      )}

      <div className="flex flex-wrap gap-2 mt-4">
        {status === 'running' && (
          <button className="btn btn-secondary btn-sm" onClick={pauseSession}><Pause size={14} aria-hidden="true" /> 暂停</button>
        )}
        {status === 'paused' && (
          <button className="btn btn-primary btn-sm" onClick={resumeSession}><Play size={14} aria-hidden="true" /> 恢复</button>
        )}
        {active && (
          <button className="btn btn-danger btn-sm" onClick={stopSession}><Square size={14} aria-hidden="true" /> 停止</button>
        )}
        <button className="btn btn-ghost btn-sm" onClick={toggleBrowser}>
          <Eye size={14} aria-hidden="true" /> {isBrowserVisible ? '收起浏览器' : '查看浏览器'}
        </button>
        <button className="btn btn-ghost btn-sm" onClick={openLogs}><ScrollText size={14} aria-hidden="true" /> 查看日志</button>
      </div>

      {showLogs && (
        <Modal title="学习日志" onClose={() => setShowLogs(false)} width={640}>
          {logs.length === 0 ? (
            <div className="text-secondary text-sm">暂无日志（后端日志接口未就绪或无记录）</div>
          ) : (
            <div className="flex flex-col gap-1 mono" style={{ fontSize: 'var(--font-size-xs)' }}>
              {logs.map((log, i) => (
                <div key={i}>
                  <span className="text-tertiary">
                    {log.time ? `[${formatDateTime(log.time, true)}] ` : ''}
                  </span>
                  <span>{log.message}</span>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}
    </section>
  );
};

export default LearningDashboard;
