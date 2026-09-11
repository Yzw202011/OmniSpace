/* ==========================================================================
 * TopicManager.tsx —— 学习主题管理（TASK-036）
 * --------------------------------------------------------------------------
 * 主题卡片列表（名称 / 进度条 / 知识数 / 开始学习 / 删除）
 * [+ 添加新主题] 弹窗输入、[🤖 让AI自己决定学什么] 一键自主学习
 * 数据：POST /v1/learn/topic/create、GET /v1/learn/topic/list、
 *       DELETE /v1/learn/topic/delete、POST /v1/learn/session/start
 * ========================================================================== */

import React, { useEffect, useRef, useState } from 'react';
import { BookMarked, Bot, Plus } from 'lucide-react';
import { useLearningStore } from '@/stores/useLearningStore';
import Modal from '@/components/common/Modal';

/** 删除二次确认超时（首次点击后 3 秒未确认自动恢复） */
const CONFIRM_TIMEOUT = 3000;

export const TopicManager: React.FC = () => {
  const topics = useLearningStore((s) => s.topics);
  const topicsLoaded = useLearningStore((s) => s.topicsLoaded);
  const fetchTopics = useLearningStore((s) => s.fetchTopics);
  const createTopic = useLearningStore((s) => s.createTopic);
  const deleteTopic = useLearningStore((s) => s.deleteTopic);
  const startSession = useLearningStore((s) => s.startSession);
  const session = useLearningStore((s) => s.session);

  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  /** 待二次确认删除的主题 id（首次点击进入确认态，超时自动恢复） */
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const confirmTimer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
    };
  }, []);

  /** 删除：行内二次确认（首次点击变确认态，3 秒超时恢复） */
  const handleDelete = (id: string) => {
    if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
    if (confirmDeleteId === id) {
      setConfirmDeleteId(null);
      deleteTopic(id);
      return;
    }
    setConfirmDeleteId(id);
    confirmTimer.current = window.setTimeout(() => setConfirmDeleteId(null), CONFIRM_TIMEOUT);
  };

  useEffect(() => {
    fetchTopics();
  }, [fetchTopics]);

  const sessionActive = session?.status === 'running' || session?.status === 'paused';

  /* 实时刷新（2026-08-23）：学习会话运行期间每 5s 轮询主题列表，
     「知识 X 条 · 已浏览 X 页」与进度条随会话推进实时更新
     （后端 /learn/topic/list 每次调用都会叠加活跃会话的实时进度）；
     会话结束（active→inactive）时补一次拉取，展示最终计数 */
  const wasActiveRef = useRef(false);
  useEffect(() => {
    if (sessionActive) {
      wasActiveRef.current = true;
      const timer = window.setInterval(() => void fetchTopics(), 5000);
      return () => window.clearInterval(timer);
    }
    if (wasActiveRef.current) {
      wasActiveRef.current = false;
      void fetchTopics();
    }
    return undefined;
  }, [sessionActive, fetchTopics]);

  const handleAdd = async () => {
    const name = newName.trim();
    if (!name) return;
    setSubmitting(true);
    const ok = await createTopic(name);
    setSubmitting(false);
    if (ok) {
      setShowAdd(false);
      setNewName('');
    }
  };

  return (
    <section className="card hoverable" aria-label="学习主题管理">
      <div className="flex items-center justify-between mb-3">
        <h3 className="card-title" style={{ marginBottom: 0 }}><BookMarked size={16} aria-hidden="true" /> 学习主题管理</h3>
        <div className="flex gap-2">
          <button className="btn btn-secondary btn-sm inline-flex items-center gap-1.5" onClick={() => startSession()} disabled={sessionActive}>
            <Bot size={13} aria-hidden="true" />
            让AI自己决定学什么
          </button>
          <button className="btn btn-primary inline-flex items-center gap-1.5" onClick={() => setShowAdd(true)}>
            <Plus size={14} aria-hidden="true" />
            添加新主题
          </button>
        </div>
      </div>

      {topics.length === 0 ? (
        <div className="flex flex-col items-center justify-center text-center border border-dashed rounded-lg" style={{ padding: 'var(--space-8) var(--space-4)', borderColor: 'rgba(155, 163, 181, 0.4)' }}>
          {topicsLoaded ? (
            <>
              <BookMarked size={28} strokeWidth={1.5} style={{ color: 'var(--color-text-tertiary)', marginBottom: 'var(--space-3)' }} aria-hidden="true" />
              <div className="text-sm text-secondary" style={{ marginBottom: 'var(--space-1)' }}>暂无学习主题</div>
              <div className="text-xs text-tertiary">点击右上角「添加新主题」创建，或让 AI 自主探索学习</div>
            </>
          ) : (
            <div className="text-sm text-tertiary">加载中…</div>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {topics.map((t) => (
            <div key={t.id} className="card hoverable" style={{ padding: 'var(--space-4)' }}>
              <div className="flex items-center justify-between mb-2">
                <strong className="text-sm">{t.name}</strong>
                {session?.topic_id === t.id && sessionActive && (
                  <span className="badge success">学习中</span>
                )}
              </div>
              <div className="progress mb-2" aria-label="学习进度">
                <div
                  className="progress-bar"
                  style={{ width: `${Math.round((t.progress ?? 0) * 100)}%` }}
                />
              </div>
              <div className="flex items-center justify-between">
                <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
                  知识 {t.knowledge_count ?? 0} 条 · 已浏览 {t.pages_visited ?? 0} 页
                </span>
                <div className="flex gap-2">
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={sessionActive}
                    onClick={() => startSession(t.id)}
                  >
                    开始学习
                  </button>
                  <button
                    className={`btn btn-sm ${confirmDeleteId === t.id ? 'btn-secondary' : 'btn-ghost'}`}
                    onClick={() => handleDelete(t.id)}
                  >
                    {confirmDeleteId === t.id ? '确认删除？' : '删除'}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {showAdd && (
        <Modal
          title="添加学习主题"
          onClose={() => setShowAdd(false)}
          footer={(
            <>
              <button className="btn btn-ghost" onClick={() => setShowAdd(false)}>取消</button>
              <button className="btn btn-primary" onClick={handleAdd} disabled={submitting || !newName.trim()}>
                {submitting ? '创建中…' : '创建'}
              </button>
            </>
          )}
        >
          <div className="form-row">
            <label className="form-label">主题名称</label>
            <input
              className="input"
              value={newName}
              autoFocus
              placeholder="如：樱花绘画技巧、漫剧分镜构图…"
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleAdd(); }}
            />
            <div className="form-hint">AI 将围绕该主题联网搜索、阅读并提取知识点。</div>
          </div>
        </Modal>
      )}
    </section>
  );
};

export default TopicManager;
// 本项目仅供学习使用，商业授权请+Q 3559331368
