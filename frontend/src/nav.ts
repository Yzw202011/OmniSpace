/* ==========================================================================
 * 导航元数据与分组（P0 重组 2026-09-17，用户拍板 1A）
 * --------------------------------------------------------------------------
 * 从 router.tsx 拆出的纯数据/纯函数模块（不触 DOM，可被 node 环境单测
 * 直测）；router.tsx 原位再导出保持既有 import 路径不变。
 *
 * 分组 = 用户心智三分：创作(用 AI) / 工场(管资源) / 系统(修机器)。
 * 组序固定；组内顺序可拖拽自定义（localStorage omni.sidebar.order，
 * 旧跨组穿插序按「组序优先、组内相对位置」安全解读）。
 * ========================================================================== */
import {
  BookOpen,
  CircleQuestionMark,
  Clapperboard,
  Feather,
  LayoutGrid,
  MessageSquare,
  Package,
  ScrollText,
  Settings,
  Video,
  type LucideIcon,
} from 'lucide-react';

import { mirrorPref } from './services/uiPrefs';

/** 导航分组键 */
export type NavGroup = 'create' | 'forge' | 'system';

export interface NavItemMeta {
  /** 路由名（不含前导斜杠） */
  route: string;
  /** 完整路径（NavLink to） */
  path: string;
  /** Lucide 图标组件 */
  icon: LucideIcon;
  /** 中文标签 */
  label: string;
  /** 导航分组 */
  group: NavGroup;
}

/** 分组呈现次序与标题（组序固定） */
export const NAV_GROUPS: { key: NavGroup; label: string }[] = [
  { key: 'create', label: '创作' },
  { key: 'forge', label: '工场' },
  { key: 'system', label: '系统' },
];

export const NAV_ITEMS: NavItemMeta[] = [
  { route: 'chat', path: '/chat', icon: MessageSquare, label: 'AI对话', group: 'create' },
  { route: 'paint', path: '/paint', icon: LayoutGrid, label: 'AI漫画', group: 'create' },
  { route: 'storyboard', path: '/storyboard', icon: Clapperboard, label: '漫剧创作', group: 'create' },
  { route: 'novel', path: '/novel', icon: Feather, label: '写作台', group: 'create' },
  { route: 'learning', path: '/learning', icon: BookOpen, label: '知识学习', group: 'create' },
  { route: 'models', path: '/models', icon: Package, label: '模型管理', group: 'forge' },
  // P0 正名：原「视频风格」——它是 LoRA 训练工场而非风格浏览页
  { route: 'style', path: '/style', icon: Video, label: '风格训练', group: 'forge' },
  { route: 'settings', path: '/settings', icon: Settings, label: '设置', group: 'system' },
  { route: 'logs', path: '/logs', icon: ScrollText, label: '系统日志', group: 'system' },
  { route: 'help', path: '/help', icon: CircleQuestionMark, label: '帮助', group: 'system' },
];

const SIDEBAR_ORDER_KEY = 'omnispace.sidebar.order';

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
    // 界面偏好镜像（与原 router.tsx 实现一致：本地写成功才镜像后端）
    void mirrorPref(SIDEBAR_ORDER_KEY, routes);
  } catch {
    /* silent-intent: 隐私模式写入失败静默 */
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
      byRoute.delete(it.route);
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

/** 把（可能带自定义序的）平铺导航项按固定组序重组为分组结构。

 * 组序优先、组内顺序按入参相对位置——旧的自定义拖拽序（跨组穿插的
 * 平铺数组）按此规则安全解读，不炸不丢项（P0 兼容承诺）。
 */
export function regroupNavItems(
  flat: NavItemMeta[],
): { key: NavGroup; label: string; items: NavItemMeta[] }[] {
  return NAV_GROUPS.map((g) => ({
    ...g,
    items: flat.filter((it) => it.group === g.key),
  }));
}
