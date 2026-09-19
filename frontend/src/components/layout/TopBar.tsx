/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 顶层导航栏（文档D §1.1.1：48px 通栏，Z 层级最高）
 * --------------------------------------------------------------------------
 * 布局（文档D §1.1.1）：
 *   左侧 Logo 区域 → 居中全局搜索触发器 → 右侧 通知中心(Bell) / 用户头像下拉 / 主题切换
 * 行为：
 *   - 全局搜索：点击展开输入框，实时过滤一级导航（NAV_ITEMS），Enter/点击跳转
 *   - 通知中心：展示全局任务（useTaskStore.tasks），活跃任务数作角标
 *   - 主题切换：暗色 Sakura ⇄ 亮色（useAppStore.setTheme，持久化 localStorage）
 *   - 用户菜单：设置 / 帮助入口
 * ========================================================================== */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Search,
  Bell,
  Sun,
  Moon,
  User,
  Settings,
  CircleQuestionMark,
  Loader,
  CircleCheck,
  CircleX,
  Flower2,
} from 'lucide-react';
import { NAV_ITEMS } from '@/router';
import { useAppStore } from '@/stores/useAppStore';
import type { Theme } from '@/stores/useAppStore';
import { useTaskStore, TASK_TYPE_LABELS } from '@/stores/useTaskStore';
import {
  useNotificationStore,
  NOTIFICATION_LEVEL_LABELS,
} from '@/stores/useNotificationStore';
import { TRAIN_STATUS_LABELS, type TrainStatusKey } from '@/constants/statusLabels';
import type { OmniTask } from '@/types';

/** 主题六态循环顺序（夜樱→拂晓→深空→晨辉→洱海月→苍山雪→夜樱；2026-09-11 增补 Dali 族） */
const NEXT_THEME: Record<Theme, Theme> = {
  sakura: 'light',
  light: 'tech',
  tech: 'tech-light',
  'tech-light': 'dali',
  dali: 'dali-light',
  'dali-light': 'sakura',
};

/** 主题显示名（与 Settings 主题选择器保持一致） */
const THEME_LABEL: Record<Theme, string> = {
  sakura: 'Sakura · 夜樱',
  light: 'Sakura · 拂晓',
  tech: 'Nebula · 深空',
  'tech-light': 'Nebula · 晨辉',
  dali: 'Dali · 洱海月',
  'dali-light': 'Dali · 苍山雪',
};

/** 点击元素外部时触发（下拉面板关闭用） */
function useClickOutside(onOutside: () => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onOutside();
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [onOutside]);
  return ref;
}

/** 任务状态图标 */
function TaskStatusIcon({ status }: { status: OmniTask['status'] }) {
  if (status === 'running' || status === 'pending') {
    return <Loader size={14} className="spin" aria-hidden="true" />;
  }
  if (status === 'done') {
    return <CircleCheck size={14} aria-hidden="true" />;
  }
  return <CircleX size={14} aria-hidden="true" />;
}

export default function TopBar() {
  const navigate = useNavigate();
  const theme = useAppStore((s) => s.theme);
  const setTheme = useAppStore((s) => s.setTheme);
  const tasks = useTaskStore((s) => s.tasks);

  /* ------------------------------ 全局搜索 ------------------------------ */
  const [searchOpen, setSearchOpen] = useState(false);
  const [keyword, setKeyword] = useState('');
  const searchRef = useClickOutside(() => {
    setSearchOpen(false);
    setKeyword('');
  });
  const inputRef = useRef<HTMLInputElement | null>(null);

  const matches = useMemo(() => {
    const kw = keyword.trim();
    if (!kw) return NAV_ITEMS;
    return NAV_ITEMS.filter((it) => it.label.includes(kw) || it.route.includes(kw.toLowerCase()));
  }, [keyword]);

  const openSearch = () => {
    setSearchOpen(true);
    // 展开后聚焦输入框
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const goto = (path: string) => {
    setSearchOpen(false);
    setKeyword('');
    navigate(path);
  };

  /* ------------------------------ 通知中心 ------------------------------ */
  const [bellOpen, setBellOpen] = useState(false);
  const bellRef = useClickOutside(() => setBellOpen(false));
  const activeCount = tasks.filter((t) => t.status === 'running' || t.status === 'pending').length;
  // 最近 6 条任务（新的在前）
  const recentTasks = useMemo(() => [...tasks].slice(-6).reverse(), [tasks]);
  // 批1 P25（2026-09-19）：通知历史（重要 toast 留痕可回看）
  const notifications = useNotificationStore((s) => s.items);
  const markAllRead = useNotificationStore((s) => s.markAllRead);
  const clearNotifications = useNotificationStore((s) => s.clear);
  const unreadCount = notifications.filter((n) => n.unread).length;
  const recentNotifications = useMemo(() => notifications.slice(0, 8), [notifications]);
  const bellBadge = activeCount + unreadCount;
  const toggleBell = () => {
    setBellOpen((v) => {
      if (!v) markAllRead(); // 打开即全读
      return !v;
    });
  };

  /* ------------------------------ 用户菜单 ------------------------------ */
  const [userOpen, setUserOpen] = useState(false);
  const userRef = useClickOutside(() => setUserOpen(false));

  return (
    <header className="topbar" role="banner">
      {/* 左侧 Logo 区 */}
      <div className="topbar-logo">
        <Flower2 size={18} aria-hidden="true" />
        <span className="logo-text">OmniSpace</span>
      </div>

      {/* 居中全局搜索触发器 */}
      <div className="topbar-search" ref={searchRef}>
        {searchOpen ? (
          <div className="topbar-search-box">
            <Search size={14} aria-hidden="true" />
            <input
              ref={inputRef}
              className="topbar-search-input"
              value={keyword}
              placeholder="搜索功能页面…"
              aria-label="全局搜索"
              onChange={(e) => setKeyword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && matches.length > 0) {
                  goto(matches[0].path);
                } else if (e.key === 'Escape') {
                  setSearchOpen(false);
                  setKeyword('');
                }
              }}
            />
            {matches.length > 0 && (
              <div className="topbar-search-suggest" role="listbox">
                {matches.map((it) => {
                  const Icon = it.icon;
                  return (
                    <button
                      key={it.route}
                      type="button"
                      className="topbar-search-item"
                      role="option"
                      aria-selected="false"
                      onClick={() => goto(it.path)}
                    >
                      <Icon size={14} aria-hidden="true" />
                      <span>{it.label}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        ) : (
          <button
            type="button"
            className="topbar-search-trigger"
            onClick={openSearch}
            aria-label="打开全局搜索"
          >
            <Search size={14} aria-hidden="true" />
            <span>搜索功能…</span>
          </button>
        )}
      </div>

      {/* 右侧操作区 */}
      <div className="topbar-actions">
        {/* 通知中心 */}
        <div className="topbar-pop" ref={bellRef}>
          <button
            type="button"
            className="topbar-icon-btn"
            onClick={toggleBell}
            title="通知中心（任务 + 历史通知）"
            aria-label={`通知中心，${activeCount} 个进行中任务，${unreadCount} 条未读通知`}
            aria-expanded={bellOpen}
          >
            <Bell size={17} aria-hidden="true" />
            {bellBadge > 0 && <span className="topbar-badge">{bellBadge}</span>}
          </button>
          {bellOpen && (
            <div className="topbar-dropdown" role="menu" aria-label="任务与历史通知">
              <div className="topbar-dropdown-title">任务通知</div>
              {recentTasks.length === 0 ? (
                <div className="topbar-dropdown-empty">暂无任务</div>
              ) : (
                recentTasks.map((t) => (
                  <div key={t.id} className={`topbar-task ${t.status}`}>
                    <TaskStatusIcon status={t.status} />
                    <span className="topbar-task-name">
                      {t.name || TASK_TYPE_LABELS[t.type] || t.type}
                    </span>
                    <span className="topbar-task-progress">
                      {t.status === 'pending'
                        ? `排队中${t.queue_position && t.queue_position > 1
                            ? `·前${t.queue_position - 1}` : ''}`
                        : t.status === 'running'
                          ? `${Math.round((t.progress || 0) * 100)}%`
                          : TRAIN_STATUS_LABELS[t.status as TrainStatusKey] ?? t.status}
                    </span>
                  </div>
                ))
              )}
              {/* 批1 P25：历史通知（失败/完成/注意级 toast 留痕回看） */}
              <div className="topbar-dropdown-title" style={{ marginTop: 8 }}>
                历史通知
                {notifications.length > 0 && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    style={{ marginLeft: 'auto', padding: '0 6px', fontSize: 11 }}
                    onClick={clearNotifications}
                  >
                    清空
                  </button>
                )}
              </div>
              {recentNotifications.length === 0 ? (
                <div className="topbar-dropdown-empty">暂无历史通知（失败/完成/降级提醒会留痕在这里）</div>
              ) : (
                recentNotifications.map((n) => (
                  <div
                    key={n.id}
                    className={`topbar-task ${n.level === 'error' ? 'error' : n.level === 'warning' ? 'pending' : 'done'}`}
                    title={new Date(n.ts).toLocaleString()}
                  >
                    {n.level === 'error' ? (
                      <CircleX size={14} aria-hidden="true" />
                    ) : n.level === 'warning' ? (
                      <Loader size={14} aria-hidden="true" />
                    ) : (
                      <CircleCheck size={14} aria-hidden="true" />
                    )}
                    <span
                      className="topbar-task-name"
                      style={{ fontWeight: n.unread ? 600 : 400 }}
                    >
                      {NOTIFICATION_LEVEL_LABELS[n.level]} · {n.text}
                    </span>
                    <span className="topbar-task-progress">
                      {new Date(n.ts).toLocaleTimeString()}
                    </span>
                  </div>
                ))
              )}
            </div>
          )}
        </div>

        {/* 主题切换（三主题体系六态循环：夜樱→拂晓→深空→晨辉→洱海月→苍山雪） */}
        <button
          type="button"
          className="topbar-icon-btn"
          onClick={() => setTheme(NEXT_THEME[theme])}
          title={`切换为 ${THEME_LABEL[NEXT_THEME[theme]]}`}
          aria-label={`切换为 ${THEME_LABEL[NEXT_THEME[theme]]}`}
        >
          {NEXT_THEME[theme] === 'light' ||
          NEXT_THEME[theme] === 'tech-light' ||
          NEXT_THEME[theme] === 'dali-light' ? (
            <Sun size={17} aria-hidden="true" />
          ) : (
            <Moon size={17} aria-hidden="true" />
          )}
        </button>

        {/* 用户头像下拉 */}
        <div className="topbar-pop" ref={userRef}>
          <button
            type="button"
            className="topbar-avatar"
            onClick={() => setUserOpen((v) => !v)}
            title="用户菜单"
            aria-label="用户菜单"
            aria-expanded={userOpen}
          >
            <User size={16} aria-hidden="true" />
          </button>
          {userOpen && (
            <div className="topbar-dropdown right" role="menu" aria-label="用户菜单">
              <button
                type="button"
                className="topbar-menu-item"
                role="menuitem"
                onClick={() => {
                  setUserOpen(false);
                  navigate('/settings');
                }}
              >
                <Settings size={14} aria-hidden="true" />
                <span>设置</span>
              </button>
              <button
                type="button"
                className="topbar-menu-item"
                role="menuitem"
                onClick={() => {
                  setUserOpen(false);
                  navigate('/help');
                }}
              >
                <CircleQuestionMark size={14} aria-hidden="true" />
                <span>帮助</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
