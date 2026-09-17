// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.5.0 —— Hash 路由配置（10 个一级路由，规格 §6.1.2）
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
import { PageStub, AppShell } from './App';
// 导航元数据/分组/自定义序（P0 拆至纯模块 nav.ts，本模块原位再导出
// 保兼容——App/TopBar 等既有 import('./router') 路径零改动）
export {
  NAV_GROUPS,
  NAV_ITEMS,
  arrangedNavItems,
  loadSidebarOrder,
  regroupNavItems,
  saveSidebarOrder,
} from './nav';
export type { NavGroup, NavItemMeta } from './nav';

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

export default router;
