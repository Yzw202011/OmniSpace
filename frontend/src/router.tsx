/* ==========================================================================
 * OmniSpace AI v2.3.1 —— Hash 路由配置（8 个一级路由，规格 §6.1.2）
 * --------------------------------------------------------------------------
 * 路由（规格 §6.1.2 左侧导航栏，无"首页"）：
 *   chat / paint / storyboard / learning / models / style / settings / help
 * 根路径 `/` 与非法路径 `*` 均重定向到 /chat（Navigate replace，不留历史栈）。
 * 使用 react-router-dom 7 的 createHashRouter（Hash 路由，离线/静态可用，COM-001）。
 * 根路由挂载 AppShell（左侧导航 + 主内容 + 右侧面板 + 底部状态栏 + Toast）。
 *
 * 页面组件接入说明（页面实现由组B负责，此处仅挂载路由）：
 *   - chat       → components/dialog/DialogPage.tsx
 *   - paint      → components/paint/PaintPage.tsx
 *   - storyboard → components/manga/MangaPage.tsx
 *   - learning   → components/learning/LearningPage.tsx
 *   - models     → components/model/ModelManager.tsx
 *   - style      → components/style/StylePage.tsx
 *   - settings   → components/Settings.tsx
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
  Palette,
  Clapperboard,
  BookOpen,
  Package,
  Video,
  Settings,
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

/** 路由表（8 个一级路由，根布局 AppShell；/ 与 * 重定向 /chat） */
export const routes: RouteObject[] = [
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/chat" replace /> },
      { path: 'chat', element: lazyPage(() => import('@/components/dialog/DialogPage')) },
      { path: 'paint', element: lazyPage(() => import('@/components/paint/PaintPage')) },
      { path: 'storyboard', element: lazyPage(() => import('@/components/manga/MangaPage')) },
      { path: 'learning', element: lazyPage(() => import('@/components/learning/LearningPage')) },
      { path: 'models', element: lazyPage(() => import('@/components/model/ModelManager')) },
      { path: 'style', element: lazyPage(() => import('@/components/style/StylePage')) },
      { path: 'settings', element: lazyPage(() => import('@/components/Settings')) },
      { path: 'help', element: lazyPage(() => import('@/components/help/HelpPage')) },
      // 非法路由重定向到 AI 对话（规格 §6.1.2 无首页，默认落地对话页）
      { path: '*', element: <Navigate to="/chat" replace /> },
    ],
  },
];

/** Hash 路由实例（COM-001：离线/静态可用） */
export const router = createHashRouter(routes);

/**
 * 导航项契约（规格 §6.1.2：8 项；图标为 Lucide 组件，规格 §6.3.4 功能图标统一线条风格）。
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
  { route: 'paint', path: '/paint', icon: Palette, label: 'AI绘画' },
  { route: 'storyboard', path: '/storyboard', icon: Clapperboard, label: '漫剧创作' },
  { route: 'learning', path: '/learning', icon: BookOpen, label: '知识学习' },
  { route: 'models', path: '/models', icon: Package, label: '模型管理' },
  { route: 'style', path: '/style', icon: Video, label: '视频风格' },
  { route: 'settings', path: '/settings', icon: Settings, label: '设置' },
  { route: 'help', path: '/help', icon: CircleQuestionMark, label: '帮助' },
];

export default router;
