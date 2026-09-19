/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 根组件（App Shell，规格 §6.1.1 主窗口结构）
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

import { Component, useEffect, useRef, useState } from 'react';
import type {
  DragEvent as ReactDragEvent,
  ErrorInfo,
  ReactNode,
} from 'react';
import { RouterProvider, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  PanelLeftClose,
  PanelLeftOpen,
  CircleCheck,
  TriangleAlert,
  CircleX, 
  Info,
  type LucideIcon,
} from 'lucide-react';
import {
  router,
  NAV_ITEMS,
  arrangedNavItems,
  regroupNavItems,
  saveSidebarOrder,
} from './router';
import { trackBehavior } from './services/learningApi';
import Tooltip from './components/common/Tooltip';
import TechParticles from './components/common/TechParticles';
import DaliParticles from './components/common/DaliParticles';
import DaliVerse from './components/common/DaliVerse';
import WarmupModal from './components/common/WarmupModal';
import PaintWarmupModal from './components/common/PaintWarmupModal';
import LicenseGate from './components/common/LicenseGate';
import TopBar from './components/layout/TopBar';
import RightPanel from './components/layout/RightPanel';
import BottomStatusBar from './components/layout/BottomStatusBar';
import RecoveryGuideCard from './components/common/RecoveryGuideCard';
import { useAppStore, type ToastItem, type ToastLevel } from './stores/useAppStore';
import { useDialogStore } from './stores/useDialogStore';
import { useHardwareStore } from './stores/useHardwareStore';
import { useMangaStore } from './stores/useMangaStore';
import { useTaskStore } from './stores/useTaskStore';
import { useSystemHealthStore } from './stores/useSystemHealthStore';
import { useWarmupStore } from './stores/useWarmupStore';
import { releaseForModule, warmupFeature } from './services/modelApi';
import { getWsHub } from './services/ws';
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
  training: 'training', // P1 训练中心（原 style 路由重定向到 /training）
};

/* ============================== 模块切换资源调度（用户裁定 2026-08-21） ==============================
 * 切入任一重量级模块（或进入漫剧项目）时，其他模块 3 秒内释放显存/内存，
 * 优先供应目标模块；Toast 反馈释放进度与结果（后端 /models/release-for-module）。
 * ================================================================================================ */

/** 功能名 → 中文模块名（Toast 展示用） */
const MODULE_LABELS: Record<string, string> = {
  dialog: 'AI对话',
  paint: 'AI漫画',
  video_gen: '漫剧创作',
  training: '知识学习',
};

/** 释放触发序号：仅展示最新一次切换的结果（快速连续切换时过期结果静默） */
let releaseSeq = 0;

/** 触发模块资源释放（fire-and-forget，不阻塞导航） */
async function triggerModuleResourceRelease(feature: string): Promise<void> {
  const label = MODULE_LABELS[feature] ?? feature;
  const seq = ++releaseSeq;
  const { showToast } = useAppStore.getState();
  showToast(`正在释放其他模块资源，优先供应【${label}】…`, 'info');
  try {
    const result = await releaseForModule(feature);
    if (seq !== releaseSeq) return; // 已有更新的切换，忽略过期结果
    if (result.freed_count > 0) {
      const freed = `释放 ${result.freed_vram_gb}GB 显存（${result.freed_count} 个模型）`;
      if (result.completed) {
        showToast(`已${freed}，优先供应【${label}】`, 'success');
      } else {
        showToast(`3秒内完成部分释放：已${freed}，优先供应【${label}】`, 'warning');
      }
    }
  } catch {
    /* silent-intent: 后端不可达（离线/启动中）静默，不打扰导航 */
  }
}

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
            <div className="error-title">应用遇到问题</div>
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
  // 侧栏自定义摆放（2026-09-05 用户需求）：拖拽重排 + localStorage 持久化
  const [navItems, setNavItems] = useState(() => arrangedNavItems());
  const navDragRouteRef = useRef<string | null>(null);
  // 性能模式（2026-09-08 UI 降载方案②）：settings.ui_performance==='lite'
  // → html.ui-lite 全局类（CSS 停动效/去毛玻璃）+ 粒子层不渲染，即时生效
  const uiPerformance = useAppStore((s) => s.settings?.ui_performance);
  const uiLite = uiPerformance === 'lite';
  useEffect(() => {
    document.documentElement.classList.toggle('ui-lite', uiLite);
  }, [uiLite]);
  const isCustomNavOrder = navItems.some(
    (it, i) => it.route !== NAV_ITEMS[i]?.route,
  );

  // 批1 P26（2026-09-19）：全局快捷键——Alt+1~9 按导航序切模块、
  // Alt+0 帮助页、Alt+N 新建对话。选 Alt 系因 Ctrl+数字/Ctrl+N 是
  // 浏览器保留键（切标签页/新窗口），页面拦不住；WebView2 与
  // Chrome/Edge 下 Alt 系可拦截。帮助页快捷键表与此处同步维护。
  const navigate = useNavigate();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const target = e.target as HTMLElement | null;
      if (target?.isContentEditable) return;
      const key = e.key.toLowerCase();
      if (key === 'n') {
        e.preventDefault();
        navigate('/chat');
        void useDialogStore.getState().createSession().then(() => {
          useAppStore.getState().showToast('已新建对话', 'success');
        }).catch(() => { /* 新建失败由对话页自身错误链路兜底 */ });
        return;
      }
      if (/^[0-9]$/.test(e.key)) {
        const paths = NAV_ITEMS.map((it) => it.path);
        const idx = e.key === '0' ? paths.length - 1 : Number(e.key) - 1;
        const path = paths[idx];
        if (path) {
          e.preventDefault();
          navigate(path);
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navigate]);

  const handleNavDragStart =
    (route: string) => (e: ReactDragEvent<HTMLAnchorElement>) => {
      navDragRouteRef.current = route;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', route);
    };
  const handleNavDragOver =
    (route: string) => (e: ReactDragEvent<HTMLAnchorElement>) => {
      e.preventDefault(); // 允许放置
      e.dataTransfer.dropEffect = 'move';
      setNavItems((items) => {
        const from = items.findIndex(
          (it) => it.route === navDragRouteRef.current,
        );
        const to = items.findIndex((it) => it.route === route);
        if (from < 0 || to < 0 || from === to) return items;
        // P0 分组导航：组序固定，仅组内拖拽生效（跨组放下不移动）
        if (items[from].group !== items[to].group) return items;
        const next = [...items];
        const [moved] = next.splice(from, 1);
        next.splice(to, 0, moved);
        return next;
      });
    };
  const handleNavDragEnd = () => {
    navDragRouteRef.current = null;
    setNavItems((items) => {
      saveSidebarOrder(items.map((it) => it.route));
      return items;
    });
  };
  const resetNavOrder = () => {
    setNavItems([...NAV_ITEMS]);
    saveSidebarOrder([]); // 空数组 = 回默认序
  };
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
      /* silent-intent: 隐私模式写入失败静默 */
    }
  }, [collapsed]);

  // 启动即拉取后端设置（2026-09-12 主题回放依赖：loadSettings 内会把
  // 后端 system.settings.theme 回放为当前主题，修复「选洱海月重启变
  // 夜樱」——localStorage 因换端口/壳配置丢失时主题打回默认。此前仅
  // 设置页挂载才拉取，回放无触发点。fire-and-forget，内部自带静默）
  useEffect(() => {
    void useAppStore.getState().loadSettings();
  }, []);

  /* --------------------- 模块切换资源调度（2026-08-21） --------------------- */
  // 上一个重量级模块（跨轻量页面保留：paint→models→storyboard 仍触发释放；
  // 会话首次进入仅记基线静默——启动时通常无已加载模型，避免无意义提示）
  const lastHeavyFeatureRef = useRef<string | null>(null);
  useEffect(() => {
    const route = location.pathname.split('/')[1] || '';
    // 批2 P33：splash「安全模式进入」带 ?safe=1（hash 内查询串）——
    // 解析一次并落会话存储，随后从 URL 抹掉防刷新重复注入
    try {
      const q = window.location.hash.split('?')[1];
      if (q && new URLSearchParams(q).get('safe') === '1') {
        useAppStore.getState().setSafeMode(true);
        const clean = window.location.hash.split('?')[0] || '#/chat';
        window.history.replaceState(null, '', clean);
      }
    } catch {
      /* silent-intent: URL 异常按正常模式进入 */
    }
    const feature = ROUTE_FEATURE[route] ?? null;
    if (!feature) return; // 轻量页面（模型管理/设置/帮助）：不触发
    // 批2 P33（2026-09-19）：安全模式（splash「安全模式进入」）下停一切
    // 自动预热/自动加载——模型只在用户显式操作时才拉起（会话级，状态栏可退出）
    if (useAppStore.getState().safeMode) return;
    const isFirst = lastHeavyFeatureRef.current === null;
    const switched = !isFirst && feature !== lastHeavyFeatureRef.current;
    lastHeavyFeatureRef.current = feature;
    let cancelled = false;
    void (async () => {
      // 模块切换（非首次）：其他模块 3s 内释放，优先供应目标模块
      if (switched) {
        await triggerModuleResourceRelease(feature);
      }
      // 对话模块预热（2026-08-22 思考过长事故）：vLLM 冷启动 ~157s，
      // 进入页面即点火（含首次——首句等待正是主诉），打字/阅读时间
      // 即加载时间。带用户持久化选择的 modelId——引擎默认加载 4b 而
      // 用户选 8b-awq 时发消息才热切换即二次冷启动，预热直接以用户
      // 选择为目标。先等释放完成再点火避免装载竞争显存；预热不持
      // 功能锁，用户切走时 release_for_module 可正常终止。
      // S6 预热延迟（2026-08-28 V77 事故根修）：/chat 是默认路由，
      // 启动/路过即点火 vLLM 与用户随后的生成任务争抢 GPU 算力
      //（质量总督深度降步 36→14）。延迟 3s 点火——路过默认落地页
      //（路由切走，cleanup 置 cancelled）不点火；重量级功能持锁
      // 期间跳过（后端 vllm_service P0 门禁双保险）。
      if (feature === 'dialog') {
        const heavy = useAppStore.getState().activeFeature;
        if (heavy === 'paint' || heavy === 'video_gen' || heavy === 'training') {
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 3000));
        if (cancelled) return;
        try {
          const targetModel = useDialogStore.getState().modelId;
          const r = await warmupFeature('dialog', targetModel);
          if (r.started) {
            // 冷启动进度弹窗（2026-08-23 替代一次性 toast）：
            // 时间渐近进度条 + /dialog/status 就绪校正，见 WarmupModal
            useWarmupStore.getState().begin(targetModel || undefined);
          }
        } catch {
          /* silent-intent: 后端不可达静默 */
        }
      }
      // AI 绘画预热（2026-08-31，用户需求「跟 AI 对话一样的冷启动弹窗」）：
      // 进页面即点火 diffusers 管线（冷启动 10~60s），选参数/浏览时间即
      // 加载时间；3s 延迟防路由路过误点火（与对话预热 S6 同口径），
      // 视频生成/训练持锁期间跳过。弹窗 = PaintWarmupModal，
      // 就绪信号 GET /draw/status loaded（诚实进度）
      if (feature === 'paint') {
        const heavy = useAppStore.getState().activeFeature;
        if (heavy === 'video_gen' || heavy === 'training') {
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 3000));
        if (cancelled) return;
        try {
          const r = await warmupFeature('paint');
          if (r.started) {
            useWarmupStore.getState().begin(undefined, 'paint');
          }
        } catch {
          /* silent-intent: 后端不可达静默 */
        }
      }
    })();
    return () => { cancelled = true; };
  }, [location.pathname]);

  // 进入漫剧项目（currentProject: null → 项目）：漫剧工作台即将进行重型生成，
  // 其他模块 3 秒内释放显存/内存优先供应（用户裁定 2026-08-21）
  const prevProjectOpenRef = useRef(false);
  useEffect(() => {
    if (mangaProjectOpen && !prevProjectOpenRef.current) {
      void triggerModuleResourceRelease('video_gen');
      lastHeavyFeatureRef.current = 'video_gen';
    }
    prevProjectOpenRef.current = mangaProjectOpen;
  }, [mangaProjectOpen]);

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
      {/* 星云粒子环境层（Nebula 主题专属：fixed z-index:-1，不占布局）；
          性能模式下不渲染（GPU/内存大户，2026-09-08 UI 降载方案②） */}
      {!uiLite && <TechParticles />}
      {/* 萤火粒子 + 四句诗句水印（Dali 风花雪月主题专属，2026-09-11 拍板 D2/D4） */}
      {!uiLite && <DaliParticles />}
      <DaliVerse />

      {/* 顶层导航栏（48px 通栏，文档D §1.1.1） */}
      <TopBar />

      <div className="shell-main">
        {/* 左侧导航（文档D §1.1.1：收起 64px 仅图标 / 展开 260px 图标+文字） */}
        <aside className={`sidebar${collapsed ? ' collapsed' : ''}`}>
          <nav className="sidebar-nav" aria-label="主导航">
            {regroupNavItems(navItems).map((group) => (
              <div key={group.key} className="sidebar-group" data-group={group.key}>
                {/* 分组标题（P0 重组）：折叠态由 CSS 隐藏、组间画细分隔线 */}
                <div className="sidebar-group-title" aria-hidden="true">
                  {group.label}
                </div>
                {group.items.map((item) => {
                const Icon = item.icon;
                // 功能互斥置灰：该路由对应重量级功能且被当前活跃功能阻断时置灰提示
                const feature = ROUTE_FEATURE[item.route];
                const blockedMsg = feature
                  ? canSwitchFeature(activeFeature, feature)
                  : null;
                const link = (
                  <NavLink
                    to={item.path}
                    draggable
                    onDragStart={handleNavDragStart(item.route)}
                    onDragOver={handleNavDragOver(item.route)}
                    onDragEnd={handleNavDragEnd}
                    style={{ cursor: 'grab' }}
                    className={({ isActive }) =>
                      `nav-item${isActive ? ' active' : ''}${blockedMsg ? ' blocked' : ''}`
                    }
                    aria-disabled={blockedMsg ? true : undefined}
                    onClick={() => {
                      // 行为学习埋点（fire-and-forget）：导航为纯路由跳转
                      // 不经过 setActiveFeature，在此直接埋模块切换事件
                      if (feature) {
                        trackBehavior('module_switch', {
                          content: feature,
                          context: location.pathname,
                          feature,
                        });
                      }
                    }}
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
              </div>
            ))}
          </nav>
          <div className="sidebar-footer">
            {/* 自定义摆放：顺序与默认不同且展开态时提供恢复入口 */}
            {!collapsed && isCustomNavOrder && (
              <button
                type="button"
                className="sidebar-collapse-btn"
                style={{ fontSize: 11, height: 'auto', padding: '6px 0', marginBottom: 4 }}
                onClick={resetNavOrder}
                title="恢复导航默认排序"
              >
                恢复默认排序
              </button>
            )}
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

      {/* 批2 P33：自愈失败恢复指南卡（fixed 定位，全页面可达） */}
      <RecoveryGuideCard />

      {/* 全局 Toast 容器 */}
      <ToastContainer />

      {/* 对话模型冷启动进度弹窗（全局单例，App 根渲染不随路由卸载） */}
      <WarmupModal />
      {/* AI 绘画模型冷启动进度弹窗（同 store 分流，paint 预热时显示） */}
      <PaintWarmupModal />
      {/* 激活门禁遮罩（P8 补洞）：任何接口报未激活即全屏弹激活窗 */}
      <LicenseGate />
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
    // 字号启动回放（2026-09-15 持久化修复）：初始态已带 localStorage 值，此处落 DOM
    document.documentElement.setAttribute(
      'data-font-size', useAppStore.getState().fontSize);

    return () => {
      useHardwareStore.getState().destroy();
      useTaskStore.getState().destroy();
    };
  }, []);

  // 死信通道复活（2026-09-15 审计修复）：notification / 资源告警 status 系 /
  // quality_degraded 此前零前端订阅——RAM/显存自动卸载、热限频、自动训练
  // 暂停、质量降参用户全无感知。与 useTaskStore 同一 /ws 连接（多订阅共存），
  // 资源类告警 60s 节流防刷屏。
  useEffect(() => {
    const conn = getWsHub();
    conn.connect();
    const lastWarn = new Map<string, number>();
    const throttled = (key: string, msg: string) => {
      const now = Date.now();
      if (now - (lastWarn.get(key) ?? 0) < 60_000) return;
      lastWarn.set(key, now);
      useAppStore.getState().showToast(msg, 'warning');
    };
    const offs = [
      conn.on<{ level?: string; message?: string }>('notification', (d) => {
        if (!d?.message) return;
        useAppStore.getState().showToast(
          d.message,
          d.level === 'error' ? 'error'
            : d.level === 'success' ? 'success' : 'info');
      }),
      // 批2 P31/P32（2026-09-19）：舱壁健康态 + 自愈进度/失败
      conn.on<Partial<import('./stores/useSystemHealthStore').ModuleHealthState> & { module: string }>('module_health', (d) => {
        if (d?.module) useSystemHealthStore.getState().applyHealth(d);
      }),
      conn.on<{ module?: string; kind?: string; step?: string; message?: string }>('self_heal', (d) => {
        if (!d?.module || !d?.kind) return;
        const labels: Record<string, string> = { dialog: 'AI对话', paint: '绘画出图', video: '视频生成', training: '训练' };
        const label = labels[d.module] ?? d.module;
        if (d.kind === 'progress') {
          useAppStore.getState().showToast(`正在自动修复「${label}」…（${d.message ?? ''}）`, 'info');
        } else if (d.kind === 'success') {
          useAppStore.getState().showToast(`「${label}」已自动恢复 ✓`, 'success');
          useSystemHealthStore.getState().closeGuide();
        } else if (d.kind === 'failed') {
          useSystemHealthStore.getState().openGuide({
            module: d.module, label, message: d.message ?? '', ts: Date.now(),
          });
        }
      }),
      conn.on<{ message?: string }>('quality_degraded', (d) => {
        throttled('quality_degraded',
                  d?.message || 'GPU 持续高压，生成参数已自动下调');
      }),
      conn.on<{ event?: string; message?: string }>('task_progress', (d) => {
        const ev = d?.event;
        if (!ev) return;
        if (ev.startsWith('resource_')) {
          throttled(ev, d?.message || '系统资源紧张：部分模型已自动让位卸载');
        } else if (ev === 'thermal_pause') {
          throttled(ev, d?.message || '温度过高：已暂停接收新任务');
        }
      }),
    ];
    return () => offs.forEach((off) => off());
  }, []);

  return (
    <ErrorBoundary>
      <RouterProvider router={router} />
    </ErrorBoundary>
  );
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
