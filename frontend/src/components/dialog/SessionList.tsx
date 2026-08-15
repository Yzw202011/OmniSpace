/**
 * SessionList 会话列表
 * OmniSpace AI v2.1 — Sakura 主题
 * --------------------------------------------------------------------------
 * 新建 / 删除（二次确认）/ 切换会话，显示最后消息预览与更新时间。
 * 置顶优先、再按更新时间倒序排列。规格 §9.2 对话模块。
 */
import { useState } from 'react';
import type { KeyboardEvent } from 'react';

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
  /** 重命名会话 */
  onRename?: (id: string, title: string) => void;
  /** 置顶/取消置顶 */
  onTogglePin?: (id: string, pinned: boolean) => void;
  /** 会话搜索（关键词变化回调，空串为清除） */
  onSearch?: (keyword: string) => void;
  /** 列表加载中 */
  loading?: boolean;
}

/** 格式化日期 M/D */
function formatDate(ts?: number): string {
  if (!ts) return '';
  const d = new Date(ts);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export function SessionList({
  sessions,
  activeId,
  onSelect,
  onCreate,
  onDelete,
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
      {/* 新建按钮 */}
      <div className="p-3 border-b border-[var(--color-divider)]">
        <button
          type="button"
          onClick={onCreate}
          className="w-full h-10 inline-flex items-center justify-center gap-1.5 rounded-lg bg-sakura-500 text-white font-medium hover:bg-sakura-600 transition-colors"
        >
          <span aria-hidden="true">＋</span>
          <span>新建对话</span>
        </button>
      </div>

      {/* 会话搜索框（接后端 keyword 过滤） */}
      {onSearch ? (
        <div className="px-3 py-2 border-b border-[var(--color-divider)]">
          <input
            type="text"
            value={keyword}
            onChange={(e) => handleSearchChange(e.target.value)}
            placeholder="搜索会话…"
            aria-label="搜索会话"
            className="w-full h-8 px-2.5 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-xs text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus:outline-none focus:ring-2 focus:ring-sakura-300"
          />
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
            <div className="text-3xl mb-2">🌸</div>
            <div className="text-sm">暂无对话</div>
            <div className="text-xs mt-1">点击上方「新建对话」开始</div>
          </div>
        ) : (
          <ul className="space-y-1">
            {sorted.map((s) => {
              const active = s.id === activeId;
              const confirming = confirmId === s.id;
              return (
                <li key={s.id}>
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => (confirming ? undefined : onSelect(s.id))}
                    onKeyDown={(e) => (confirming ? undefined : handleKeyDown(e, s.id))}
                    aria-pressed={active}
                    className={[
                      'group relative px-3 py-2.5 rounded-lg cursor-pointer transition-colors',
                      active ? 'bg-sakura-100' : 'hover:bg-sakura-50',
                    ].join(' ')}
                  >
                    {/* 置顶标记 */}
                    {s.pinned ? (
                      <span className="absolute top-1 right-1 text-xs" title="已置顶" aria-hidden="true">
                        📌
                      </span>
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
                          className="flex-1 h-7 px-2 rounded border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] focus:outline-none focus:ring-2 focus:ring-sakura-300"
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
                          className="flex-1 h-7 text-xs rounded bg-[var(--color-error)] text-white hover:brightness-90"
                        >
                          确认删除
                        </button>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setConfirmId(null);
                          }}
                          className="h-7 px-2 text-xs rounded bg-[var(--color-divider)] text-[var(--color-text-secondary)] hover:bg-[var(--color-border-light)]"
                        >
                          取消
                        </button>
                      </div>
                    ) : (
                      /* 行内操作：置顶 / 重命名 / 删除（悬停显示） */
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
                            className="w-6 h-6 rounded text-[var(--color-text-tertiary)] hover:text-sakura-600 hover:bg-sakura-50 flex items-center justify-center text-xs"
                          >
                            📌
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
                            className="w-6 h-6 rounded text-[var(--color-text-tertiary)] hover:text-sakura-600 hover:bg-sakura-50 flex items-center justify-center text-xs"
                          >
                            ✏️
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
                          className="w-6 h-6 rounded text-[var(--color-text-tertiary)] hover:text-[var(--color-error)] hover:bg-[var(--color-error-bg)] flex items-center justify-center text-xs"
                        >
                          🗑️
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
