// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * StorageCard.tsx —— 存储清理中心（批4 P11，2026-09-19）
 * --------------------------------------------------------------------------
 * 目录用量盘点（条形+大小）+ 白名单一键清理（逐动作独立确认）+ 磁盘
 * 余量。硬边界见后端：模型权重/作品原图绝不出现在清理动作里。
 * ========================================================================== */

import { useCallback, useEffect, useState } from 'react';
import { HardDrive, RefreshCw, Trash2 } from 'lucide-react';
import {
  getStorageInventory, storageCleanup,
  type StorageInventory,
} from '@/services/systemApi';
import { useAppStore } from '@/stores/useAppStore';
import { confirmDialog } from '@/stores/useConfirmStore';
import { getErrorMessage, reportBgError } from '@/utils/errors';

/** 清理动作 → 人话（与后端白名单一一对应） */
const CLEAN_ACTIONS: Array<{ action: string; label: string; lines: string[]; keep?: boolean }> = [
  { action: 'thumbs', label: '清空缩略图缓存',
    lines: ['缩略图可自动重建，安全'] },
  { action: 'temp', label: '清空临时文件',
    lines: ['生成过程的中间产物，安全'] },
  { action: 'exports', label: '清空导出成品',
    lines: ['漫画成册/漫剧导出件将删除', '需要时可在各页重新导出'] },
  { action: 'comfy_h3', label: '清空视频链中间帧',
    lines: ['H3 视频工作目录将删除（成片不受影响）', '下次生成自动重建'] },
  { action: 'backups_keep', label: '只保留最近 3 份备份', keep: true,
    lines: ['更旧的数据库备份与设置快照将删除', '恢复点将变少（保留最近 3 份）'] },
  { action: 'old_logs', label: '清理 30 天前旧日志',
    lines: ['活跃日志不受影响'] },
];

const fmtMB = (bytes: number): string => {
  const mb = bytes / 1024 / 1024;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)}GB` : `${mb.toFixed(1)}MB`;
};

export default function StorageCard() {
  const showToast = useAppStore((s) => s.showToast);
  const [inv, setInv] = useState<StorageInventory | null>(null);
  const [busy, setBusy] = useState('');

  const load = useCallback(() => {
    getStorageInventory()
      .then(setInv)
      .catch((err: unknown) => {
        reportBgError('StorageCard', err);
        setInv(null);
      });
  }, []);

  useEffect(() => { load(); }, [load]);

  const runCleanup = (a: typeof CLEAN_ACTIONS[number]) => {
    void confirmDialog({
      level: a.action === 'backups_keep' ? 'medium' : 'light',
      title: a.label,
      lines: a.lines,
      confirmText: '开始清理',
    }).then((go) => {
      if (!go) return;
      setBusy(a.action);
      storageCleanup(a.action, a.keep ? { keep: 3 } : { days: 30 })
        .then((r) => {
          showToast(`${a.label}完成：删 ${r.deleted} 项，释放 ${r.freed_mb}MB`, 'success');
          load();
        })
        .catch((err: unknown) =>
          showToast(getErrorMessage(err, '清理失败'), 'error'))
        .finally(() => setBusy(''));
    });
  };

  const totalBytes = inv?.targets.reduce((s, t) => s + t.bytes, 0) ?? 0;
  const maxBytes = Math.max(1, ...(inv?.targets.map((t) => t.bytes) ?? [1]));

  return (
    <div className="card hoverable" aria-label="存储空间">
      <h3 className="card-title flex items-center gap-2">
        <HardDrive size={16} aria-hidden="true" /> 存储空间
        <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', fontWeight: 400 }}>
          产物合计 {fmtMB(totalBytes)}
        </span>
        <button type="button" className="btn-icon ml-auto" title="刷新盘点" aria-label="刷新盘点"
                onClick={load}>
          <RefreshCw size={13} />
        </button>
      </h3>

      {/* 磁盘余量 */}
      <div className="flex gap-4 flex-wrap mt-2">
        {(inv?.disks ?? []).map((d) => (
          <span key={d.drive} className="text-sm text-secondary"
                style={{ color: d.free_gb < 32 ? 'var(--color-warning)' : undefined }}>
            {d.drive} 剩 {d.free_gb}GB / {d.total_gb}GB
          </span>
        ))}
      </div>

      {/* 目录用量（条形，倒序） */}
      <div className="flex flex-col gap-1.5 mt-3">
        {(inv?.targets ?? [])
          .filter((t) => t.bytes > 0)
          .sort((a, b) => b.bytes - a.bytes)
          .map((t) => (
            <div key={t.key} className="flex items-center gap-2" title={`${t.dir} · ${t.note}`}>
              <span className="text-secondary text-xs" style={{ width: 96, flexShrink: 0 }}>{t.label}</span>
              <div style={{ flex: 1, height: 8, background: 'var(--color-input-bg)', borderRadius: 4, overflow: 'hidden' }}>
                <div style={{
                  width: `${Math.max(2, (t.bytes / maxBytes) * 100)}%`, height: '100%',
                  background: t.cleanable ? 'var(--color-accent)' : 'var(--color-primary)',
                  opacity: t.cleanable ? 0.9 : 0.45,
                }} />
              </div>
              <span className="text-tertiary text-xs" style={{ width: 64, textAlign: 'right' }}>
                {fmtMB(t.bytes)}
              </span>
            </div>
          ))}
        {inv && inv.targets.every((t) => t.bytes === 0) && (
          <span className="text-tertiary text-sm">暂无产物占用</span>
        )}
      </div>

      {/* 白名单清理动作 */}
      <div className="flex gap-1.5 flex-wrap mt-3">
        {CLEAN_ACTIONS.map((a) => (
          <button key={a.action} type="button" className="btn btn-ghost btn-sm"
                  disabled={busy === a.action}
                  title={a.lines.join('；')}
                  onClick={() => runCleanup(a)}>
            <Trash2 size={12} aria-hidden="true" />
            {busy === a.action ? '清理中…' : a.label}
          </button>
        ))}
      </div>
      <div className="text-tertiary mt-2" style={{ fontSize: 'var(--font-size-xs)' }}>
        作品数据（图片/视频/资产/关键帧/LoRA）不在此清理——请走各模块的正规删除；模型权重永不自动清理。
      </div>
    </div>
  );
}
