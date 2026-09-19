// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * ConfirmDialog.tsx —— 全局分级确认弹窗（批4 P9，2026-09-19）
 * --------------------------------------------------------------------------
 * 由 AppShell 挂载一份；内容由 useConfirmStore.pending 驱动。
 * 三级样式：light/medium=标准；heavy=红色主按钮+「不可恢复」徽标。
 * ========================================================================== */

import { AlertTriangle } from 'lucide-react';
import { useConfirmStore } from '@/stores/useConfirmStore';

const LEVEL_STYLE: Record<string, { btn: string; badge: string | null }> = {
  light: { btn: 'btn-primary', badge: null },
  medium: { btn: 'btn-primary', badge: null },
  heavy: { btn: 'btn-danger', badge: '不可恢复' },
};

export default function ConfirmDialog() {
  const pending = useConfirmStore((s) => s.pending);
  const settle = useConfirmStore((s) => s._settle);
  if (!pending) return null;
  const style = LEVEL_STYLE[pending.level] ?? LEVEL_STYLE.medium;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={pending.title}
      onClick={() => settle(false)}
      style={{
        position: 'fixed', inset: 0, zIndex: 2100,
        background: 'rgba(0,0,0,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        padding: 'var(--space-4)',
      }}
    >
      <div
        className="card"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 'min(440px, calc(100vw - 32px))',
          padding: 'var(--space-5)',
          display: 'flex', flexDirection: 'column', gap: 'var(--space-3)',
        }}
      >
        <div className="flex items-center gap-2">
          {pending.level === 'heavy' && (
            <AlertTriangle size={18} style={{ color: 'var(--color-error)' }} aria-hidden="true" />
          )}
          <strong style={{ fontSize: 'var(--font-size-base)' }}>{pending.title}</strong>
          {style.badge && (
            <span className="badge error" style={{ marginLeft: 'auto' }}>{style.badge}</span>
          )}
        </div>
        {pending.lines && pending.lines.length > 0 && (
          <ul className="text-sm text-secondary"
              style={{ paddingLeft: 'var(--space-5)', display: 'grid', gap: 6 }}>
            {pending.lines.map((l, i) => <li key={i}>{l}</li>)}
          </ul>
        )}
        <div className="flex justify-end gap-2" style={{ marginTop: 'var(--space-2)' }}>
          <button type="button" className="btn btn-ghost" onClick={() => settle(false)}>
            {pending.cancelText ?? '取消'}
          </button>
          <button type="button" className={`btn ${style.btn}`} onClick={() => settle(true)}>
            {pending.confirmText ?? '确认'}
          </button>
        </div>
      </div>
    </div>
  );
}
