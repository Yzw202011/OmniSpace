// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.3.1 —— Hash 路由配置（10 个一级路由，规格 §6.1.2）
 * --------------------------------------------------------------------------
 * 路由（规格 §6.1.2 左侧导航栏，无"首页"；2026-08-21 新增 logs、
 * 2026-09-05 新增 novel）：
 *   chat / paint / storyboard / novel / learning / models / style / settings / logs / help
 * 根路径 `/` 与非法路径 `*` 均重定向到 /chat（Navigate replace，不留历史栈）。
 * 使用 react-router-dom 7 的 createHashRouter（Hash 路由，离线/静态可用，COM-001）。
 * 根路由挂载 AppShell（左侧导航 + 主内容 + 右侧面板 + 底部状态栏 + Toast）。
 *
 * 页面组件接入说明（页面实现由组B负责，此处仅挂载路由）：
 *   - chat       → components/dialog/DialogPage.tsx
 *   - paint      → components/comic/ComicPage.tsx（2026-09-07 漫画模块 M1：AI漫画替代AI绘画）
 *   - storyboard → components/manga/MangaPage.tsx
 *   - learning   → components/learning/LearningPage.tsx
 *   - models     → components/model/ModelManager.tsx
 *   - style      → components/style/StylePage.tsx
 *   - settings   → components/Settings.tsx
 *   - logs       → components/logs/LogsPage.tsx（2026-08-21 日志可视化面板）
 *   - help       → components/help/HelpPage.tsx
 * ========================================================================== */

import React, { Suspense, lazy } from 'react';
import {
  createHashRouter,
  Navigate,
  type RouteObject,
} from 'react-router-dom';
import {
  MessageSquare,
  LayoutGrid,
  Clapperboard,
  Feather,
  BookOpen,
  Package,
  Video,
  Settings,
  ScrollText,
  CircleQuestionMark,
  type LucideIcon,
} from 'lucide-react';
import { PageStub, AppShell } from './App';

/** 懒加载封装（页面就绪后启用） */
export function lazyPage(loader: () => Promise<{ default: React.ComponentType }>) {
  const Comp = lazy(loader);
  return (
    <Suspense fallback={<PageStub title="加载中…" loading />}>
      <Comp />
    </Suspense>
  );
}

/** 路由表（9 个一级路由，根布局 AppShell；/ 与 * 重定向 /chat） */
export const routes: RouteObject[] = [
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/chat" replace /> },
      { path: 'chat', element: lazyPage(() => import('@/components/dialog/DialogPage')) },
      { path: 'paint', element: lazyPage(() => import('@/components/comic/ComicPage')) },
      { path: 'storyboard', element: lazyPage(() => import('@/components/manga/MangaPage')) },
      { path: 'novel', element: lazyPage(() => import('@/components/novel/NovelPage')) },
      { path: 'learning', element: lazyPage(() => import('@/components/learning/LearningPage')) },
      { path: 'models', element: lazyPage(() => import('@/components/model/ModelManager')) },
      { path: 'style', element: lazyPage(() => import('@/components/style/StylePage')) },
      { path: 'settings', element: lazyPage(() => import('@/components/Settings')) },
      { path: 'logs', element: lazyPage(() => import('@/components/logs/LogsPage')) },
      { path: 'help', element: lazyPage(() => import('@/components/help/HelpPage')) },
      // 非法路由重定向到 AI 对话（规格 §6.1.2 无首页，默认落地对话页）
      { path: '*', element: <Navigate to="/chat" replace /> },
    ],
  },
];

/** Hash 路由实例（COM-001：离线/静态可用） */
export const router = createHashRouter(routes);

/**
 * 导航项契约（规格 §6.1.2 基础 8 项 + 系统日志；图标为 Lucide 组件，规格 §6.3.4 功能图标统一线条风格）。
 * 顺序即侧栏渲染顺序；设置/帮助固定在列表末尾。
 */
export interface NavItemMeta {
  /** 路由名（不含前导斜杠） */
  route: string;
  /** 完整路径（NavLink to） */
  path: string;
  /** Lucide 图标组件 */
  icon: LucideIcon;
  /** 中文标签 */
  label: string;
}

export const NAV_ITEMS: NavItemMeta[] = [
  { route: 'chat', path: '/chat', icon: MessageSquare, label: 'AI对话' },
  { route: 'paint', path: '/paint', icon: LayoutGrid, label: 'AI漫画' },
  { route: 'storyboard', path: '/storyboard', icon: Clapperboard, label: '漫剧创作' },
  { route: 'novel', path: '/novel', icon: Feather, label: '写作台' },
  { route: 'learning', path: '/learning', icon: BookOpen, label: '知识学习' },
  { route: 'models', path: '/models', icon: Package, label: '模型管理' },
  { route: 'style', path: '/style', icon: Video, label: '视频风格' },
  { route: 'settings', path: '/settings', icon: Settings, label: '设置' },
  { route: 'logs', path: '/logs', icon: ScrollText, label: '系统日志' },
  { route: 'help', path: '/help', icon: CircleQuestionMark, label: '帮助' },
];

/* ────────────────── 侧栏自定义摆放（2026-09-05 用户需求）──────────────────
 * 拖拽重排导航项，顺序持久化 localStorage（key: omni.sidebar.order）。
 * 健壮性：保存里含已下线路由则过滤、缺失的路由按默认序追加尾部；
 * localStorage 不可用（隐私模式/无 DOM）时静默回退默认序。
 * ─────────────────────────────────────────────────────────────────────── */
const SIDEBAR_ORDER_KEY = 'omni.sidebar.order';

/** 读取保存的导航顺序（route 名数组）；无记录/损坏返回空数组 */
export function loadSidebarOrder(): string[] {
  try {
    const raw = localStorage.getItem(SIDEBAR_ORDER_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr)
      ? arr.filter((x): x is string => typeof x === 'string')
      : [];
  } catch {
    return [];
  }
}

/** 保存导航顺序；传空数组 = 恢复默认序 */
export function saveSidebarOrder(routes: string[]): void {
  try {
    localStorage.setItem(SIDEBAR_ORDER_KEY, JSON.stringify(routes));
  } catch {
    /* 写入失败静默（隐私模式） */
  }
}

/** 按保存顺序排列导航项；无记录回退默认序，缺失项按默认序追加尾部 */
export function arrangedNavItems(): NavItemMeta[] {
  const saved = loadSidebarOrder();
  if (saved.length === 0) {
    return [...NAV_ITEMS];
  }
  const byRoute = new Map(NAV_ITEMS.map((it) => [it.route, it]));
  const arranged: NavItemMeta[] = [];
  for (const route of saved) {
    const it = byRoute.get(route);
    if (it) {
      arranged.push(it);
      byRoute.delete(route);
    }
  }
  for (const it of NAV_ITEMS) {
    if (byRoute.has(it.route)) {
      arranged.push(it);
      byRoute.delete(it.route);
    }
  }
  return arranged;
}

export default router;
