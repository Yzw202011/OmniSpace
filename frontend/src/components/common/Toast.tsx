/**
 * Toast 通知容器
 * OmniSpace AI v2.3.1 — Sakura 暗色主题
 * --------------------------------------------------------------------------
 * 四种级别 success / warning / error / info，3.6 秒自动消失（点击可立即关闭）。
 * 提供 ToastProvider 上下文与 useToast 钩子，供任意子组件全局调用。
 * 样式复用 app.css §10 的 .toast / .toast-container（暗色卡片 #16213E +
 * 左侧 4px 语义色边 + 右侧滑入 300ms，§6.3.3 动效），图标为 Lucide（§6.3.4）。
 * 规格 §9 通用组件。
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { CircleCheck, TriangleAlert, CircleX, Info, type LucideIcon } from 'lucide-react';

/** 通知级别 */
export type ToastLevel = 'success' | 'warning' | 'error' | 'info';

/** 单条通知项 */
export interface ToastItem {
  id: number;
  text: string;
  level: ToastLevel;
  /** 是否正在退出（播放退出动画） */
  leaving?: boolean;
}

/** Toast 上下文值 */
export interface ToastContextValue {
  /** 推送一条通知 */
  push: (text: string, level?: ToastLevel) => void;
  /** 移除一条通知 */
  remove: (id: number) => void;
}

/** 级别 → Lucide 图标（着色由 app.css 的 .toast.<level> .toast-icon 负责） */
const TOAST_ICONS: Record<ToastLevel, LucideIcon> = {
  success: CircleCheck,
  warning: TriangleAlert,
  error: CircleX,
  info: Info,
};

/** 自动消失时长（ms） */
const AUTO_DISMISS_MS = 3600;

/** 退出动画时长（对齐 app.css toast-out 的 --duration-modal-out 150ms，留 10ms 余量） */
const EXIT_MS = 160;

const ToastContext = createContext<ToastContextValue | null>(null);

/** 单条 Toast（类名即语义：app.css §10 统一着色与动效，无内联色值） */
function ToastRow({ item, onRemove }: { item: ToastItem; onRemove: (id: number) => void }) {
  const Icon = TOAST_ICONS[item.level] ?? Info;
  return (
    <div
      role="status"
      className={`toast ${item.level}${item.leaving ? ' leaving' : ''}`}
      onClick={() => onRemove(item.id)}
    >
      <span className="toast-icon">
        <Icon size={16} aria-hidden="true" />
      </span>
      <span className="flex-1">{item.text}</span>
    </div>
  );
}

/** Toast 提供者：包裹应用后，子组件可通过 useToast 推送通知 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const seqRef = useRef(0);
  const timersRef = useRef<Record<number, ReturnType<typeof setTimeout>>>({});

  const remove = useCallback((id: number) => {
    // 先标记退出动画（app.css toast-out 150ms），EXIT_MS 后真正移除
    setToasts((list) => list.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
    timersRef.current[id] = setTimeout(() => {
      setToasts((list) => list.filter((t) => t.id !== id));
      delete timersRef.current[id];
    }, EXIT_MS);
  }, []);

  const push = useCallback(
    (text: string, level: ToastLevel = 'info') => {
      const id = ++seqRef.current;
      // 最多保留 5 条
      setToasts((list) => [...list, { id, text, level }].slice(-5));
      timersRef.current[id] = setTimeout(() => remove(id), AUTO_DISMISS_MS);
    },
    [remove],
  );

  // 卸载时清理所有定时器
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      Object.values(timers).forEach(clearTimeout);
    };
  }, []);

  return (
    <ToastContext.Provider value={{ push, remove }}>
      {children}
      <div className="toast-container" role="region" aria-label="通知">
        {toasts.map((t) => (
          <ToastRow key={t.id} item={t} onRemove={remove} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/** useToast 钩子：在 ToastProvider 内调用以获取 push / remove */
export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    // 容错：未在 Provider 内时返回 no-op，避免子组件崩溃
    return { push: () => {}, remove: () => {} };
  }
  return ctx;
}

export default ToastProvider;
