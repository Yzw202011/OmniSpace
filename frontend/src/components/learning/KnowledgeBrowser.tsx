/* ==========================================================================
 * KnowledgeBrowser.tsx —— 知识库管理（TASK-036 / TASK-055 图谱视图）
 * --------------------------------------------------------------------------
 * 条数统计 / 搜索 / 列表分页 / 删除 / 知识图谱视图切换（v2.3.1）
 * 数据：GET /v1/knowledge/stats、GET /v1/knowledge/list、DELETE /v1/knowledge/{id}
 *       GET /v1/knowledge/graph（图谱）
 * ========================================================================== */

import React, { useEffect, useRef, useState } from 'react';
import { Database, List, Network, Search } from 'lucide-react';
import { useLearningStore } from '@/stores/useLearningStore';
import { formatNumber, formatFileSize, formatRelativeTime, truncate } from '@utils/format';
import { KnowledgeGraphView } from './KnowledgeGraphView';

const PAGE_SIZE = 10;
/** 删除二次确认超时（首次点击后 3 秒未确认自动恢复） */
const CONFIRM_TIMEOUT = 3000;

export const KnowledgeBrowser: React.FC = () => {
  const stats = useLearningStore((s) => s.knowledgeStats);
  const items = useLearningStore((s) => s.knowledgeItems);
  const total = useLearningStore((s) => s.knowledgeTotal);
  const page = useLearningStore((s) => s.knowledgePage);
  const graph = useLearningStore((s) => s.knowledgeGraph);
  const graphLoading = useLearningStore((s) => s.knowledgeGraphLoading);
  const fetchKnowledgeStats = useLearningStore((s) => s.fetchKnowledgeStats);
  const fetchKnowledge = useLearningStore((s) => s.fetchKnowledge);
  const deleteKnowledge = useLearningStore((s) => s.deleteKnowledge);
  const batchDeleteKnowledge = useLearningStore((s) => s.batchDeleteKnowledge);
  const fetchKnowledgeGraph = useLearningStore((s) => s.fetchKnowledgeGraph);

  const [keyword, setKeyword] = useState('');
  const [view, setView] = useState<'list' | 'graph'>('list');
  /** 待二次确认删除的条目 id（首次点击进入确认态，超时自动恢复） */
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const confirmTimer = useRef<number | null>(null);
  // 批量管理（UAT 2026-09-10 4-6 缺口补齐）：勾选集 + 两段式确认
  const [batchMode, setBatchMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirmBatch, setConfirmBatch] = useState(false);
  const [batchDeleting, setBatchDeleting] = useState(false);

  useEffect(() => {
    return () => {
      if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
    };
  }, []);

  useEffect(() => {
    fetchKnowledgeStats();
    fetchKnowledge(1, '');
  }, [fetchKnowledgeStats, fetchKnowledge]);

  // 切换到图谱视图时按需拉取
  useEffect(() => {
    if (view === 'graph' && !graph && !graphLoading) {
      fetchKnowledgeGraph();
    }
  }, [view, graph, graphLoading, fetchKnowledgeGraph]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const handleSearch = () => {
    fetchKnowledge(1, keyword.trim());
  };

  /** 删除：行内二次确认（首次点击变确认态，3 秒超时恢复） */
  const handleDelete = (id: string) => {
    if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
    if (confirmDeleteId === id) {
      setConfirmDeleteId(null);
      deleteKnowledge(id);
      return;
    }
    setConfirmDeleteId(id);
    confirmTimer.current = window.setTimeout(() => setConfirmDeleteId(null), CONFIRM_TIMEOUT);
  };

  /** 批量管理（UAT 2026-09-10 4-6）：勾选→删除选中（两段确认） */
  const toggleBatch = () => {
    setBatchMode((v) => !v);
    setSelectedIds(new Set());
    setConfirmBatch(false);
  };
  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };
  const handleBatchDelete = async () => {
    if (!confirmBatch) { setConfirmBatch(true); return; }
    if (selectedIds.size === 0 || batchDeleting) return;
    setBatchDeleting(true);
    try {
      await batchDeleteKnowledge([...selectedIds]);
      setSelectedIds(new Set());
      setConfirmBatch(false);
      if (selectedIds.size >= items.length) setBatchMode(false);
    } finally {
      setBatchDeleting(false);
    }
  };

  return (
    <section className="card hoverable" aria-label="知识库管理">
      <div className="flex items-center justify-between mb-3">
        <h3 className="card-title" style={{ marginBottom: 0 }}><Database size={16} aria-hidden="true" /> 知识库</h3>
        <div className="flex items-center gap-2">
          {/* 视图切换（TASK-055） */}
          <div className="flex gap-1" role="tablist" aria-label="视图切换">
            <button
              className={`btn btn-sm ${view === 'list' ? 'btn-secondary' : 'btn-ghost'}`}
              onClick={() => setView('list')}
            >
              <List size={14} aria-hidden="true" /> 列表
            </button>
            <button
              className={`btn btn-sm ${view === 'graph' ? 'btn-secondary' : 'btn-ghost'}`}
              onClick={() => setView('graph')}
            >
              <Network size={14} aria-hidden="true" /> 图谱
            </button>
          </div>
          <span className="badge info">
            {stats ? `${formatNumber(stats.total)} 条` : '统计未就绪'}
            {stats?.disk_bytes ? ` · ${formatFileSize(stats.disk_bytes)}` : ''}
          </span>
        </div>
      </div>

      {view === 'graph' ? (
        <KnowledgeGraphView
          graph={graph}
          loading={graphLoading}
          onRefresh={() => fetchKnowledgeGraph()}
        />
      ) : (
        <>
          {/* 搜索栏 */}
          <div className="flex gap-2 mb-3">
            <input
              className="input flex-1"
              placeholder="搜索知识条目…"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleSearch(); }}
            />
            <button className="btn btn-secondary btn-sm" onClick={handleSearch}><Search size={14} aria-hidden="true" /> 搜索</button>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => toggleBatch()}
            >
              {batchMode ? '退出批量管理' : '批量管理'}
            </button>
          </div>

          {/* 批量管理工具条（UAT 2026-09-10 4-6） */}
          {batchMode && (
            <div className="flex items-center justify-between mb-3" style={{
              padding: 'var(--space-2) var(--space-3)',
              border: '1px solid var(--color-border-light)',
              borderRadius: 'var(--radius-lg)',
            }}>
              <span className="text-secondary text-sm">
                已选 {selectedIds.size} / {items.length} 条（勾选仅作用当前页）
              </span>
              <div className="flex items-center gap-2">
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() => setSelectedIds(new Set(items.map((it) => it.id)))}
                >
                  全选本页
                </button>
                <button className="btn btn-ghost btn-sm" onClick={() => setSelectedIds(new Set())}>
                  清空选择
                </button>
                <button
                  className={`btn btn-sm ${confirmBatch ? 'btn-secondary' : 'btn-primary'}`}
                  disabled={selectedIds.size === 0 || batchDeleting}
                  onClick={() => void handleBatchDelete()}
                >
                  {batchDeleting ? '删除中…' : confirmBatch ? `确认删除 ${selectedIds.size} 条` : '删除选中'}
                </button>
              </div>
            </div>
          )}

          {/* 列表 */}
          {items.length === 0 ? (
            <div className="text-secondary text-sm" style={{ padding: 'var(--space-4) 0' }}>
              暂无知识条目（后端 /v1/knowledge/list 未就绪或知识库为空）。
            </div>
          ) : (
            <div className="flex flex-col gap-2" style={{ maxHeight: 480, overflowY: 'auto' }}>
              {items.map((item) => (
                <div
                  key={item.id}
                  className="flex items-center gap-3"
                  style={{
                    padding: 'var(--space-3) var(--space-4)',
                    border: '1px solid var(--color-border-light)',
                    borderRadius: 'var(--radius-lg)',
                  }}
                >
                  {/* 批量勾选（UAT 2026-09-10 4-6） */}
                  {batchMode && (
                    <input
                      type="checkbox"
                      aria-label={`选中 ${item.title || truncate(item.content, 20)}`}
                      checked={selectedIds.has(item.id)}
                      onChange={() => toggleSelect(item.id)}
                    />
                  )}
                  <div className="flex-1" style={{ minWidth: 0 }}>
                    <div className="text-sm" style={{ fontWeight: 'var(--font-weight-medium)' }}>
                      {item.title || truncate(item.content, 40) || '（无标题）'}
                    </div>
                    <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
                      {item.source ? `来源：${item.source} · ` : ''}
                      {item.created_at ? formatRelativeTime(item.created_at) : ''}
                    </div>
                  </div>
                  {/* 查看该知识的图谱上下文（TASK-055） */}
                  <button
                    className="btn btn-ghost btn-sm"
                    title="查看该知识的图谱上下文"
                    onClick={() => {
                      setView('graph');
                      fetchKnowledgeGraph(item.id);
                    }}
                  >
                    <Network size={14} aria-hidden="true" />
                  </button>
                  <button
                    className={`btn btn-sm ${confirmDeleteId === item.id ? 'btn-secondary' : 'btn-ghost'}`}
                    onClick={() => handleDelete(item.id)}
                  >
                    {confirmDeleteId === item.id ? '确认删除？' : '删除'}
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* 分页 */}
          {total > PAGE_SIZE && (
            <div className="flex items-center justify-between mt-3">
              <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
                第 {page} / {totalPages} 页 · 共 {formatNumber(total)} 条
              </span>
              <div className="flex gap-2">
                <button
                  className="btn btn-ghost btn-sm"
                  disabled={page <= 1}
                  onClick={() => fetchKnowledge(page - 1)}
                >
                  ← 上一页
                </button>
                <button
                  className="btn btn-ghost btn-sm"
                  disabled={page >= totalPages}
                  onClick={() => fetchKnowledge(page + 1)}
                >
                  下一页 →
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
};

export default KnowledgeBrowser;
