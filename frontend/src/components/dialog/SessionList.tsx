// 本项目仅供学习使用，商业授权请+Q 3559331368
/**
 * SessionList 会话列表
 * OmniSpace AI v2.1 — Sakura 主题
 * --------------------------------------------------------------------------
 * 新建 / 删除（二次确认）/ 切换会话，显示最后消息预览与更新时间。
 * 置顶优先、再按更新时间倒序排列。规格 §9.2 对话模块。
 */
import { useState } from 'react';
import type { KeyboardEvent } from 'react';
import { Pin, Pencil, Trash2, Flower2, ListChecks, X } from 'lucide-react';

/** 会话项 */
export interface ChatSession {
  /** 会话 ID */
  id: string;
  /** 会话标题 */
  title: string;
  /** 最后消息预览 */
  preview?: string;
  /** 更新时间戳（毫秒） */
  updatedAt?: number;
  /** 是否置顶 */
  pinned?: boolean;
}

export interface SessionListProps {
  /** 会话列表 */
  sessions: ChatSession[];
  /** 当前激活会话 ID */
  activeId?: string | null;
  /** 切换会话 */
  onSelect: (id: string) => void;
  /** 新建会话 */
  onCreate: () => void;
  /** 删除会话 */
  onDelete: (id: string) => void;
  /** 批量删除会话（批量管理模式勾选后触发） */
  onBatchDelete?: (ids: string[]) => void;
  /** 重命名会话 */
  onRename?: (id: string, title: string) => void;
  /** 置顶/取消置顶 */
  onTogglePin?: (id: string, pinned: boolean) => void;
  /** 会话搜索（关键词变化回调，空串为清除） */
  onSearch?: (keyword: string) => void;
  /** 列表加载中 */
  loading?: boolean;
}

/** 格式化日期 M/D（后端秒级时间戳，防秒被当毫秒——UAT 2026-09-10 1/22 日期 bug） */
function formatDate(ts?: number): string {
  if (!ts) return '';
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export function SessionList({
  sessions,
  activeId,
  onSelect,
  onCreate,
  onDelete,
  onBatchDelete,
  onRename,
  onTogglePin,
  onSearch,
  loading = false,
}: SessionListProps) {
  const [confirmId, setConfirmId] = useState<string | null>(null);
  /** 行内重命名编辑态 */
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');
  /** 会话搜索关键词（受控于父级搜索回调） */
  const [keyword, setKeyword] = useState('');
  /** 批量管理模式：勾选集 / 删除中 / 批量删除二次确认 */
  const [batchMode, setBatchMode] = useState(false);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [batchConfirming, setBatchConfirming] = useState(false);
  const [batchDeleting, setBatchDeleting] = useState(false);

  function toggleChecked(id: string) {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function exitBatchMode() {
    setBatchMode(false);
    setChecked(new Set());
    setBatchConfirming(false);
    setBatchDeleting(false);
  }

  async function handleBatchDelete() {
    if (!onBatchDelete || checked.size === 0 || batchDeleting) return;
    setBatchDeleting(true);
    try {
      await onBatchDelete(Array.from(checked));
      exitBatchMode();
    } catch {
      setBatchDeleting(false);
      setBatchConfirming(false);
    }
  }

  function handleSearchChange(v: string) {
    setKeyword(v);
    onSearch?.(v.trim());
  }

  function commitRename(id: string) {
    const title = editingTitle.trim();
    if (title) {
      onRename?.(id, title);
    }
    setEditingId(null);
    setEditingTitle('');
  }

  // 置顶优先，再按更新时间倒序
  const sorted = [...sessions].sort((a, b) => {
    const pa = a.pinned ? 1 : 0;
    const pb = b.pinned ? 1 : 0;
    if (pa !== pb) return pb - pa;
    return (b.updatedAt || 0) - (a.updatedAt || 0);
  });

  function handleKeyDown(e: KeyboardEvent, id: string) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelect(id);
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* 新建 + 搜索（同一区块，统一 36px 控件节奏） */}
      <div className="p-3 flex flex-col gap-2 border-b border-[var(--color-divider)]">
        {batchMode ? (
          /* 批量管理工具栏：全选 / 计数 / 退出 */
          <div className="flex items-center gap-1.5 h-9">
            <button
              type="button"
              onClick={() => setChecked((prev) =>
                prev.size >= sorted.length ? new Set() : new Set(sorted.map((s) => s.id)))}
              className="h-9 px-2.5 rounded-lg text-xs font-medium text-[var(--color-text-secondary)] hover:bg-sakura-50 transition-colors inline-flex items-center gap-1"
            >
              <ListChecks size={14} aria-hidden="true" />
              {checked.size >= sorted.length && sorted.length > 0 ? '取消全选' : '全选'}
            </button>
            <span className="flex-1 text-xs text-[var(--color-text-tertiary)] truncate">
              已选 {checked.size} / {sorted.length} 个会话
            </span>
            <button
              type="button"
              onClick={exitBatchMode}
              aria-label="退出批量管理"
              title="退出批量管理"
              className="w-9 h-9 rounded-lg text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)] hover:bg-sakura-50 transition-colors flex items-center justify-center"
            >
              <X size={15} aria-hidden="true" />
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={onCreate}
            className="w-full h-9 inline-flex items-center justify-center gap-1.5 rounded-lg bg-sakura-500 text-white font-medium text-sm hover:bg-sakura-600 transition-colors"
          >
            <span aria-hidden="true">＋</span>
            <span>新建对话</span>
          </button>
        )}
        {onSearch ? (
          <input
            type="text"
            value={keyword}
            onChange={(e) => handleSearchChange(e.target.value)}
            placeholder="搜索会话…"
            aria-label="搜索会话"
            className="w-full h-9 px-3 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus:outline-none focus:ring-2 focus:ring-sakura-300"
          />
        ) : null}
        {/* 批量管理入口（无会话时隐藏） */}
        {onBatchDelete && !batchMode && sorted.length > 0 ? (
          <button
            type="button"
            onClick={() => {
              setEditingId(null);
              setEditingTitle('');
              setBatchMode(true);
            }}
            className="w-full h-8 inline-flex items-center justify-center gap-1.5 rounded-lg text-xs text-[var(--color-text-tertiary)] hover:text-sakura-600 hover:bg-sakura-50 transition-colors"
          >
            <ListChecks size={13} aria-hidden="true" />
            <span>批量管理</span>
          </button>
        ) : null}
      </div>

      {/* 批量模式底部操作栏：删除选中（二次确认含张数） */}
      {batchMode ? (
        <div className="p-3 border-t border-[var(--color-divider)]">
          {batchConfirming ? (
            <div className="flex flex-col gap-2">
              <p className="text-xs text-[var(--color-text-secondary)]">
                确定删除选中的 <span className="font-semibold text-[var(--color-error)]">{checked.size}</span> 个对话吗？对话及全部消息将不可恢复。
              </p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={batchDeleting}
                  onClick={handleBatchDelete}
                  className="flex-1 h-8 text-xs rounded-lg bg-[var(--color-error)] text-white hover:brightness-90 disabled:opacity-60"
                >
                  {batchDeleting ? '删除中…' : `确认删除 ${checked.size} 个`}
                </button>
                <button
                  type="button"
                  disabled={batchDeleting}
                  onClick={() => setBatchConfirming(false)}
                  className="h-8 px-2.5 text-xs rounded-lg bg-[var(--color-divider)] text-[var(--color-text-secondary)] hover:bg-[var(--color-border-light)] disabled:opacity-60"
                >
                  取消
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              disabled={checked.size === 0}
              onClick={() => setBatchConfirming(true)}
              className="w-full h-9 inline-flex items-center justify-center gap-1.5 rounded-lg text-sm font-medium text-white bg-[var(--color-error)] hover:brightness-90 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Trash2 size={14} aria-hidden="true" />
              删除选中{checked.size > 0 ? `（${checked.size}）` : ''}
            </button>
          )}
        </div>
      ) : null}

      {/* 列表 */}
      <div className="flex-1 overflow-y-auto p-2">
        {loading ? (
          <div className="flex items-center justify-center py-8 text-sm text-[var(--color-text-tertiary)]">
            <span className="inline-block w-4 h-4 border-2 border-sakura-300 border-t-transparent rounded-full animate-spin mr-2" />
            加载中…
          </div>
        ) : sorted.length === 0 ? (
          <div className="text-center py-10 text-[var(--color-text-tertiary)]">
            <div className="flex justify-center mb-2 text-[var(--color-primary)]"><Flower2 size={28} strokeWidth={1.5} aria-hidden="true" /></div>
            <div className="text-sm">暂无对话</div>
            <div className="text-xs mt-1">点击上方「新建对话」开始</div>
          </div>
        ) : (
          <ul className="space-y-1">
            {sorted.map((s) => {
              const active = s.id === activeId;
              const confirming = confirmId === s.id;
              const isChecked = checked.has(s.id);
              return (
                <li key={s.id}>
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => (batchMode ? toggleChecked(s.id)
                      : confirming ? undefined : onSelect(s.id))}
                    onKeyDown={(e) => (batchMode
                      ? (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), toggleChecked(s.id))
                      : confirming ? undefined : handleKeyDown(e, s.id))}
                    aria-pressed={batchMode ? isChecked : active}
                    className={[
                      'group relative px-3 py-2.5 rounded-lg cursor-pointer transition-colors',
                      batchMode && isChecked ? 'bg-sakura-100'
                        : active && !batchMode ? 'bg-sakura-100' : 'hover:bg-sakura-50',
                    ].join(' ')}
                  >
                    {/* 批量模式勾选框 */}
                    {batchMode ? (
                      <span
                        aria-hidden="true"
                        className={[
                          'absolute top-2 right-2 w-4 h-4 rounded border flex items-center justify-center transition-colors',
                          isChecked
                            ? 'bg-sakura-500 border-sakura-500 text-white'
                            : 'border-[var(--color-input-border)] bg-[var(--color-input-bg)]',
                        ].join(' ')}
                      >
                        {isChecked ? (
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
                        ) : null}
                      </span>
                    ) : null}

                    {/* 置顶标记 */}
                    {s.pinned && !batchMode ? (
                      <Pin className="absolute top-1.5 right-1.5 w-3 h-3 text-sakura-500" aria-hidden="true" />
                    ) : null}

                    <div className="flex items-center gap-2">
                      {editingId === s.id ? (
                        <input
                          type="text"
                          value={editingTitle}
                          autoFocus
                          onChange={(e) => setEditingTitle(e.target.value)}
                          onBlur={() => commitRename(s.id)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              e.preventDefault();
                              commitRename(s.id);
                            } else if (e.key === 'Escape') {
                              setEditingId(null);
                              setEditingTitle('');
                            }
                          }}
                          onClick={(e) => e.stopPropagation()}
                          aria-label="重命名会话"
                          className="flex-1 h-8 px-2 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] focus:outline-none focus:ring-2 focus:ring-sakura-300"
                        />
                      ) : (
                        <span
                          className={[
                            'flex-1 text-sm font-medium truncate',
                            active ? 'text-sakura-700' : 'text-[var(--color-text-primary)]',
                          ].join(' ')}
                          title={s.title}
                        >
                          {s.title || '未命名对话'}
                        </span>
                      )}
                      {s.updatedAt ? (
                        <span className="text-xs text-[var(--color-text-tertiary)] shrink-0">{formatDate(s.updatedAt)}</span>
                      ) : null}
                    </div>

                    {/* 最后消息预览 */}
                    {s.preview ? (
                      <div className="text-xs text-[var(--color-text-tertiary)] truncate mt-0.5">{s.preview}</div>
                    ) : null}

                    {/* 删除：二次确认 */}
                    {confirming ? (
                      <div className="flex items-center gap-1 mt-1.5">
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            onDelete(s.id);
                            setConfirmId(null);
                          }}
                          className="flex-1 h-8 text-xs rounded-lg bg-[var(--color-error)] text-white hover:brightness-90"
                        >
                          确认删除
                        </button>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setConfirmId(null);
                          }}
                          className="h-8 px-2.5 text-xs rounded-lg bg-[var(--color-divider)] text-[var(--color-text-secondary)] hover:bg-[var(--color-border-light)]"
                        >
                          取消
                        </button>
                      </div>
                    ) : batchMode ? null : (
                      /* 行内操作：置顶 / 重命名 / 删除（悬停显示；批量模式下隐藏） */
                      <div className="absolute bottom-1 right-1 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                        {onTogglePin ? (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              onTogglePin(s.id, !s.pinned);
                            }}
                            aria-label={s.pinned ? `取消置顶 ${s.title}` : `置顶对话 ${s.title}`}
                            title={s.pinned ? '取消置顶' : '置顶'}
                            className="w-7 h-7 rounded-md text-[var(--color-text-tertiary)] hover:text-sakura-600 hover:bg-sakura-50 flex items-center justify-center"
                          >
                            <Pin className="w-3.5 h-3.5" />
                          </button>
                        ) : null}
                        {onRename ? (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              setEditingId(s.id);
                              setEditingTitle(s.title);
                            }}
                            aria-label={`重命名对话 ${s.title}`}
                            title="重命名"
                            className="w-7 h-7 rounded-md text-[var(--color-text-tertiary)] hover:text-sakura-600 hover:bg-sakura-50 flex items-center justify-center"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                        ) : null}
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setConfirmId(s.id);
                          }}
                          aria-label={`删除对话 ${s.title}`}
                          title="删除"
                          className="w-7 h-7 rounded-md text-[var(--color-text-tertiary)] hover:text-[var(--color-error)] hover:bg-[var(--color-error-bg)] flex items-center justify-center"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

export default SessionList;
