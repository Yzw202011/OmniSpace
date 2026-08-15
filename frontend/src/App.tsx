/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 根组件（App Shell，规格 §6.1.1 主窗口结构）
 * --------------------------------------------------------------------------
 * 布局（文档D §1.1.1 五层结构）：
 *   ┌ 顶层导航栏（48px 通栏，components/layout/TopBar：Logo/全局搜索/通知/头像/主题切换）
 *   │ ┌ 左侧导航（64px 收起 / 260px 展开，折叠态持久化，tooltip 悬停显示名称）
 *   │ │ 主内容区（Hash 路由 Outlet）
 *   │ │ 右侧面板（320px，按路由动态切换，components/layout/RightPanel）
 *   └ └ 底部状态栏（28px 通栏，components/layout/BottomStatusBar）
 * 组成：
 *   - ErrorBoundary 错误边界包裹（捕获子树渲染异常，COM-002 不白屏）
 *   - RouterProvider 渲染 Hash 路由（8 个一级路由，规格 §6.1.2）
 *   - 全局 Toast 容器（订阅 useAppStore.toasts，Lucide 状态图标 §6.3.4）
 * 覆盖测试用例：COM-001/002（离线可用、不白屏）、COM-009（Sakura 唯一主题）、
 *              COM-011（WS 断线重连灯）、COM-013（响应式）、COM-016（全中文界面）
 * ========================================================================== */

import { Component, useEffect, useState } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { RouterProvider, NavLink, Outlet, useLocation } from 'react-router-dom';
import {
  PanelLeftClose,
  PanelLeftOpen,
  CircleCheck,
  TriangleAlert,
  CircleX, 
  Info,
  type LucideIcon,
} from 'lucide-react';
import { router, NAV_ITEMS } from './router';
import Tooltip from './components/common/Tooltip';
import TopBar from './components/layout/TopBar';
import RightPanel from './components/layout/RightPanel';
import BottomStatusBar from './components/layout/BottomStatusBar';
import { useAppStore, type ToastItem, type ToastLevel } from './stores/useAppStore';
import { useHardwareStore } from './stores/useHardwareStore';
import { useMangaStore } from './stores/useMangaStore';
import { useTaskStore } from './stores/useTaskStore';
import { canSwitchFeature, type ActiveFeature } from './types';

/**
 * 一级路由 → 重量级 AI 功能映射（规格 §6.1 功能互斥状态机）。
 * 某功能活跃时，其余重量级功能的导航入口视觉置灰（CROSS-002/003）；
 * 模型管理/设置/帮助为轻量页面，不参与互斥（永远可用）。
 * 注：置灰仅提示，导航仍可点击——真正的动作级阻断由页内 store
 * （setActiveFeature 返回 false + Toast）兜底，与帮助页文案口径一致。
 */
const ROUTE_FEATURE: Record<string, Exclude<ActiveFeature, null>> = {
  chat: 'dialog',
  paint: 'paint',
  storyboard: 'video_gen',
  learning: 'training',
  style: 'training',
};

/** 侧栏折叠态本地持久化键 */
const SIDEBAR_COLLAPSED_KEY = 'omnispace.layout.sidebarCollapsed';

/** 读取侧栏折叠态（异常回退展开） */
function loadSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

/* ============================== 错误边界（COM-002 不白屏） ============================== */
interface ErrorBoundaryProps {
  children: ReactNode;
}
interface ErrorBoundaryState {
  hasError: boolean;
  message: string;
}

class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, message: '' };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, message: error.message || '未知错误' };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 错误可追踪（COM-005）：输出到控制台，便于排障
    console.error('[OmniSpace] 渲染异常:', error, info);
  }

  handleReload = (): void => {
    this.setState({ hasError: false, message: '' });
    window.location.reload();
  };

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div className="error-boundary">
          <div className="error-boundary-card card">
            <div className="error-title">🌸 应用遇到问题</div>
            <p className="error-message">{this.state.message}</p>
            <button className="btn btn-primary" onClick={this.handleReload}>
              重新加载
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

/* ============================== 页面占位（懒加载兜底） ============================== */
export interface PageStubProps {
  /** 页面标题 */
  title: string;
  /** 是否加载中 */
  loading?: boolean;
}

/** 页面占位组件：懒加载期间保证不白屏（COM-002） */
export function PageStub({ title, loading = false }: PageStubProps) {
  return (
    <div className="page">
      <div className="loading-block">
        <div className="spinner lg" />
        <div>{loading ? '页面加载中…' : `${title}`}</div>
      </div>
    </div>
  );
}

/* ============================== Toast 容器（暗色卡片 + Lucide 状态图标） ============================== */
const TOAST_ICONS: Record<ToastLevel, LucideIcon> = {
  success: CircleCheck,
  warning: TriangleAlert,
  error: CircleX,
  info: Info,
};

function ToastContainer() {
  const toasts = useAppStore((s) => s.toasts);
  const removeToast = useAppStore((s) => s.removeToast);

  return (
    <div className="toast-container" role="status" aria-live="polite">
      {toasts.map((t: ToastItem) => {
        const Icon = TOAST_ICONS[t.level] ?? Info;
        return (
          <div
            key={t.id}
            className={`toast ${t.level}${t.leaving ? ' leaving' : ''}`}
            role="button"
            tabIndex={0}
            onClick={() => removeToast(t.id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                removeToast(t.id);
              }
            }}
          >
            <span className="toast-icon">
              <Icon size={16} aria-hidden="true" />
            </span>
            <span className="flex-1">{t.text}</span>
          </div>
        );
      })}
    </div>
  );
}

/* ============================== 应用外壳（路由 Layout，挂载于根路由） ==============================
 * 结构（文档D §1.1.1 五层）：app-shell（列）= 顶栏(48px) + shell-main（行：侧栏 + 主内容 +
 * 右面板）+ statusbar(28px)。Logo 位于顶栏左侧（文档D §1.1.1），侧栏不再重复渲染。
 * 侧栏折叠：64px（仅图标 + 悬停 tooltip）/ 260px（图标+文字），折叠态持久化 localStorage；
 * 窗口 < 1024px 时由 CSS 媒体查询强制 64px 图标模式（文档D §1.1.2 Tablet 档 / app.css §21）。
 * ========================================================================================== */
export function AppShell() {
  const [collapsed, setCollapsed] = useState<boolean>(loadSidebarCollapsed);
  // 功能互斥快照（规格 §6.1）：重量级功能活跃时驱动导航置灰（CROSS-002/003）
  const activeFeature = useAppStore((s) => s.activeFeature);
  const location = useLocation();
  // 漫剧编辑器全屏模式（竞品对齐）：进入项目后隐藏全局外壳
  // （顶栏/左导航/右面板/底部状态栏），工作台独占整窗；返回作品库即恢复
  const mangaProjectOpen = useMangaStore((s) => s.currentProject !== null);
  const mangaFullscreen = mangaProjectOpen && location.pathname.startsWith('/storyboard');

  // 折叠态持久化（读取已在 useState 初始化完成，这里只写）
  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0');
    } catch {
      /* 隐私模式写入失败静默 */
    }
  }, [collapsed]);

  // 漫剧工作台全屏：仅渲染主内容出口 + Toast（竞品编辑器=独立整页）
  if (mangaFullscreen) {
    return (
      <div className="app-shell manga-fullscreen">
        <main className="main-content">
          <Outlet />
        </main>
        <ToastContainer />
      </div>
    );
  }

  return (
    <div className="app-shell">
      {/* 顶层导航栏（48px 通栏，文档D §1.1.1） */}
      <TopBar />

      <div className="shell-main">
        {/* 左侧导航（文档D §1.1.1：收起 64px 仅图标 / 展开 260px 图标+文字） */}
        <aside className={`sidebar${collapsed ? ' collapsed' : ''}`}>
          <nav className="sidebar-nav" aria-label="主导航">
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              // 功能互斥置灰：该路由对应重量级功能且被当前活跃功能阻断时置灰提示
              const feature = ROUTE_FEATURE[item.route];
              const blockedMsg = feature
                ? canSwitchFeature(activeFeature, feature)
                : null;
              const link = (
                <NavLink
                  to={item.path}
                  className={({ isActive }) =>
                    `nav-item${isActive ? ' active' : ''}${blockedMsg ? ' blocked' : ''}`
                  }
                  aria-disabled={blockedMsg ? true : undefined}
                >
                  <span className="nav-icon">
                    <Icon size={18} aria-hidden="true" />
                  </span>
                  <span className="nav-label">{item.label}</span>
                </NavLink>
              );
              // 互斥置灰时悬停提示阻断原因（展开/收起态均提示，CROSS-002）
              if (blockedMsg) {
                return (
                  <Tooltip key={item.route} content={blockedMsg} placement="right">
                    {link}
                  </Tooltip>
                );
              }
              // 收起态悬停显示功能名称 tooltip（§6.1.2 悬停行）；展开态标签已可见，不重复提示
              return collapsed ? (
                <Tooltip key={item.route} content={item.label} placement="right">
                  {link}
                </Tooltip>
              ) : (
                <span key={item.route}>{link}</span>
              );
            })}
          </nav>
          <div className="sidebar-footer">
            <button
              type="button"
              className="sidebar-collapse-btn"
              onClick={() => setCollapsed((c) => !c)}
              title={collapsed ? '展开导航栏' : '收起导航栏'}
              aria-label={collapsed ? '展开导航栏' : '收起导航栏'}
              aria-expanded={!collapsed}
            >
              {collapsed ? (
                <PanelLeftOpen size={16} aria-hidden="true" />
              ) : (
                <PanelLeftClose size={16} aria-hidden="true" />
              )}
            </button>
          </div>
        </aside>

        {/* 主内容区（Hash 路由子页面出口） */}
        <main className="main-content">
          <Outlet />
        </main>

        {/* 右侧面板（320px，按路由动态切换；设置/帮助路由不渲染） */}
        <RightPanel />
      </div>

      {/* 底部状态栏（28px 通栏，§6.1.4 十字段实时） */}
      <BottomStatusBar />

      {/* 全局 Toast 容器 */}
      <ToastContainer />
    </div>
  );
}

/* ============================== 应用根组件 ============================== */
export default function App() {
  // 启动：初始化硬件状态、全局任务订阅、加载设置
  useEffect(() => {
    useHardwareStore.getState().init();
    useTaskStore.getState().init();
    useAppStore.getState().loadSettings();

    return () => {
      useHardwareStore.getState().destroy();
      useTaskStore.getState().destroy();
    };
  }, []);

  return (
    <ErrorBoundary>
      <RouterProvider router={router} />
    </ErrorBoundary>
  );
}
