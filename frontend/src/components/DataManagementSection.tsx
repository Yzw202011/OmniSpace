/**
 * DataManagementSection 数据管理卡片（设置页，2026-09-17 接线四项）
 * --------------------------------------------------------------------------
 * 备份（POST /system/backup）+ 恢复（GET /system/restore/list → POST
 * /system/restore，下次重启生效）+ 项目导出/导入（.omnispace 归档）。
 * 全部服务函数在 systemApi.ts 就绪（含 Zod/TS 类型）。
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Archive,
  DatabaseBackup,
  Download,
  FileUp,
  Loader2,
  RotateCcw,
} from 'lucide-react';
import {
  backup,
  exportProject,
  importProject,
} from '../services/systemApi';
import { get, post } from '../services/api';
import { useAppStore } from '../stores/useAppStore';
import { confirmDialog } from '../stores/useConfirmStore';

interface RestoreBackupItem {
  filename: string;
  size_bytes: number;
  mtime: number;
}

interface RestoreList {
  backups: RestoreBackupItem[];
  pending_restore: boolean;
}

export default function DataManagementSection() {
  const showToast = useAppStore((s) => s.showToast);
  const [busy, setBusy] = useState('');
  const [backups, setBackups] = useState<RestoreBackupItem[]>([]);
  const [pendingRestore, setPendingRestore] = useState(false);
  const [projectId, setProjectId] = useState('');
  const [importPath, setImportPath] = useState('');

  const fetchBackups = useCallback(async () => {
    try {
      const res = await get<unknown>('/system/restore/list');
      const data = res as RestoreList;
      setBackups(data.backups.slice(0, 5));
      setPendingRestore(data.pending_restore);
    } catch {
      /* silent-intent: 后端不可达保持空列表 */
    }
  }, []);

  useEffect(() => {
    void fetchBackups();
  }, [fetchBackups]);

  const handleBackup = async () => {
    setBusy('backup');
    try {
      const res = await backup();
      // BUG-07 修复（2026-10-19）：显示数据库副本真实体积——此前拿
      // settings 快照的 size_bytes（~359B），12MB 库被显示成「0KB」
      const dbSize = res.db_size_bytes ?? res.size_bytes;
      showToast(`备份完成（${(dbSize / 1048576).toFixed(1)}MB）`, 'success');
      void fetchBackups();
    } catch (err) {
      showToast(err instanceof Error ? err.message : '备份失败', 'error');
    } finally {
      setBusy('');
    }
  };

  /** 撤销已安排的恢复（BUG-05 修复配套：此前安排后无任何取消通道） */
  const handleCancelRestore = async () => {
    const go = await confirmDialog({
      level: 'medium',
      title: '撤销恢复安排？',
      lines: ['取消后下次重启不再回滚数据库，当前数据保持不变。'],
      confirmText: '撤销',
    });
    if (!go) return;
    setBusy('cancel-restore');
    try {
      await post('/system/restore/cancel', {});
      showToast('已撤销恢复安排', 'success');
      void fetchBackups();
    } catch (err) {
      showToast(err instanceof Error ? err.message : '撤销失败', 'error');
    } finally {
      setBusy('');
    }
  };

  const handleRestore = async (filename: string) => {
    const go = await confirmDialog({
      level: 'medium',
      title: `确定恢复备份 "${filename}"？`,
      lines: [
        '将从此备份还原数据库。',
        '下次重启 OmniSpace 后生效（当前会话不受影响）。',
        '恢复前会自动把当前数据库再备份一份（可回退）。',
      ],
      confirmText: '恢复',
    });
    if (!go) return;
    setBusy('restore');
    try {
      await post<unknown>('/system/restore', { filename });
      showToast('恢复已安排——下次重启后生效', 'success');
      void fetchBackups();
    } catch (err) {
      showToast(err instanceof Error ? err.message : '恢复失败', 'error');
    } finally {
      setBusy('');
    }
  };

  const handleExport = async () => {
    if (!projectId.trim()) {
      showToast('请填写要导出的项目 ID', 'error');
      return;
    }
    setBusy('export');
    try {
      const res = await exportProject(projectId.trim());
      showToast(`导出完成：${res.archive}（${(res.size_bytes / 1048576).toFixed(1)}MB）`, 'success');
    } catch (err) {
      showToast(err instanceof Error ? err.message : '导出失败', 'error');
    } finally {
      setBusy('');
    }
  };

  const handleImport = async () => {
    if (!importPath.trim()) {
      showToast('请填写 .omnispace 归档文件路径', 'error');
      return;
    }
    const go = await confirmDialog({
      level: 'medium',
      title: `从 "${importPath.trim()}" 导入项目？`,
      confirmText: '导入',
    });
    if (!go) return;
    setBusy('import');
    try {
      const res = await importProject(importPath.trim());
      showToast(`导入成功：项目 ${res.project_id}`, 'success');
      setImportPath('');
    } catch (err) {
      showToast(err instanceof Error ? err.message : '导入失败', 'error');
    } finally {
      setBusy('');
    }
  };

  const fmtSize = (b: number) =>
    b > 1048576 ? `${(b / 1048576).toFixed(1)}MB` : `${(b / 1024).toFixed(0)}KB`;

  return (
    <div className="settings-section">
      <h3><DatabaseBackup size={16} /> 数据管理</h3>

      {/* 备份 */}
      <div className="settings-row" style={{ justifyContent: 'space-between' }}>
        <div>
          <strong>数据库备份</strong>
          <p className="text-secondary text-sm">热备 SQLite 主库到 data/backups/（自动清理未启用；可在「存储清理中心」手动只保留最近 3 份）</p>
        </div>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={busy !== ''}
          onClick={() => void handleBackup()}
        >
          {busy === 'backup' ? <Loader2 size={14} className="animate-spin" /> : <DatabaseBackup size={14} />}
          立即备份
        </button>
      </div>

      {/* 恢复 */}
      <div className="settings-row">
        <strong>从备份恢复</strong>
        {pendingRestore && (
          <>
            <span className="badge warning" style={{ marginLeft: 8 }}>
              待恢复（重启后生效）
            </span>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={busy !== ''}
              onClick={() => void handleCancelRestore()}
            >
              撤销安排
            </button>
          </>
        )}
        {backups.length === 0 ? (
          <p className="text-secondary text-sm">暂无可用备份</p>
        ) : (
          <div style={{ display: 'grid', gap: 'var(--space-1)', marginTop: 4 }}>
            {backups.map((b) => (
              <div key={b.filename} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                <RotateCcw size={12} />
                <span style={{ minWidth: 200 }}>{b.filename}</span>
                <span className="text-secondary">{fmtSize(b.size_bytes)}</span>
                <span className="text-tertiary">
                  {new Date(b.mtime * 1000).toLocaleString()}
                </span>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  disabled={busy !== ''}
                  onClick={() => void handleRestore(b.filename)}
                >
                  恢复
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 项目导出 */}
      <div className="settings-row">
        <strong><Archive size={14} /> 项目导出</strong>
        <p className="text-secondary text-sm">把指定项目打包为 .omnispace 归档（含分镜/资产/关键帧）</p>
        <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
          <input
            type="text"
            className="input"
            placeholder="项目 ID（漫剧/漫画项目列表可查）"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            style={{ flex: 1 }}
          />
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={busy !== '' || !projectId.trim()}
            onClick={() => void handleExport()}
          >
            {busy === 'export' ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
            导出
          </button>
        </div>
      </div>

      {/* 项目导入 */}
      <div className="settings-row">
        <strong><FileUp size={14} /> 项目导入</strong>
        <p className="text-secondary text-sm">从 .omnispace 归档恢复项目到本机</p>
        <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
          <input
            type="text"
            className="input"
            placeholder="归档文件绝对路径（如 E:\backups\project.omnispace）"
            value={importPath}
            onChange={(e) => setImportPath(e.target.value)}
            style={{ flex: 1 }}
          />
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={busy !== '' || !importPath.trim()}
            onClick={() => void handleImport()}
          >
            {busy === 'import' ? <Loader2 size={14} className="animate-spin" /> : <FileUp size={14} />}
            导入
          </button>
        </div>
      </div>
    </div>
  );
}
