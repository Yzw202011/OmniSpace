/* ==========================================================================
 * UpgradeSection —— 设置页「软件升级」卡片（升级机制批2，2026-09-11）
 * --------------------------------------------------------------------------
 * docs/升级机制方案-2026-09-08.md §3.1：
 *   当前版本卡 + 导入区（拖拽/选择 .upg）+ 包列表（状态徽章）+
 *   两步确认开始升级 + 全屏等待页（D2=A：升级完自动刷新进新版）。
 * 安全：开始升级需两步确认（后端再验 confirm==="UPGRADE"）；
 *   导入/删除只动 updates/ 目录；所有失败走 reportActionError 人话呈现。
 * ========================================================================== */
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  deleteUpgradePackage,
  getUpgradePackages,
  getUpgradeStatus,
  importUpgradePackage,
  openUpgradeFolder,
  startUpgrade,
  type UpgradePackageEntry,
  type UpgradePackagesResp,
} from '@/services/upgradeApi';
import { useAppStore } from '@/stores/useAppStore';
import { reportActionError } from '@/utils/errors';

const MAX_IMPORT_MB = 2048;

/** 徽章文案与配色（CSS 变量，禁硬编码色值） */
function badgeFor(p: UpgradePackageEntry): { text: string; color: string } {
  if (!p.signature_ok) return { text: '签名无效', color: 'var(--color-error)' };
  if (p.compatible) return { text: '可升级', color: 'var(--color-success)' };
  return { text: '版本不兼容', color: 'var(--color-warning)' };
}

export default function UpgradeSection() {
  const [current, setCurrent] = useState<{ version: string; build: string; edition: string } | null>(null);
  const [packages, setPackages] = useState<UpgradePackageEntry[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [importing, setImporting] = useState(false);
  const [confirmFile, setConfirmFile] = useState('');
  const [starting, setStarting] = useState(false);
  const [upgradingTo, setUpgradingTo] = useState('');
  const fileRef = useRef<HTMLInputElement | null>(null);
  const showToast = useCallback(
    (msg: string, level: 'success' | 'error' | 'info') =>
      useAppStore.getState().showToast(msg, level),
    []);

  const refresh = useCallback(async (): Promise<UpgradePackagesResp | null> => {
    try {
      const resp = await getUpgradePackages();
      setPackages(resp.packages);
      setCurrent(resp.current);
      setLoaded(true);
      return resp;
    } catch (err) {
      reportActionError(err, '读取升级包列表');
      return null;
    }
  }, []);

  useEffect(() => {
    void getUpgradeStatus()
      .then((s) => setCurrent(s.current))
      .catch(() => undefined);
    void refresh();
  }, [refresh]);

  /** 升级等待页：每 3s 探测后端心跳，恢复且版本变化 → 自动刷新（D2=A） */
  useEffect(() => {
    if (!upgradingTo) return;
    const oldBuild = current?.build ?? '';
    const timer = window.setInterval(async () => {
      try {
        const s = await getUpgradeStatus();
        if (s.current.build && s.current.build !== oldBuild) {
          window.clearInterval(timer);
          showToast(`升级完成，已更新到 v${upgradingTo}`, 'success');
          window.location.reload();
        }
      } catch {
        /* 升级期间后端下线属预期，继续等待 */
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [upgradingTo, current?.build, showToast]);

  const doImport = async (file: File): Promise<void> => {
    if (!file.name.endsWith('.upg')) {
      showToast('请选择 .upg 升级包文件', 'error');
      return;
    }
    if (file.size > MAX_IMPORT_MB * 1024 * 1024) {
      showToast(`文件超过 ${MAX_IMPORT_MB}MB 上限，疑似不是升级包`, 'error');
      return;
    }
    setImporting(true);
    try {
      const r = await importUpgradePackage(file);
      if (r.signature_ok && r.compatible) {
        showToast(`已导入 v${r.manifest?.to_version ?? '?'} 升级包，可开始升级`, 'success');
      } else if (!r.signature_ok) {
        showToast('导入完成，但签名验证失败：包可能被篡改或非官方出品', 'error');
      } else {
        showToast(`导入完成，但版本不兼容：${r.reason}`, 'error');
      }
      await refresh();
    } catch (err) {
      reportActionError(err, '导入升级包');
    } finally {
      setImporting(false);
    }
  };

  const doStart = async (name: string): Promise<void> => {
    setStarting(true);
    try {
      await startUpgrade(name);
      setUpgradingTo(
        packages.find((p) => p.file === name)?.manifest?.to_version ?? '');
    } catch (err) {
      reportActionError(err, '开始升级');
      setConfirmFile('');
    } finally {
      setStarting(false);
    }
  };

  const doDelete = async (name: string): Promise<void> => {
    try {
      await deleteUpgradePackage(name);
      showToast('已删除升级包', 'success');
      await refresh();
    } catch (err) {
      reportActionError(err, '删除升级包');
    }
  };

  return (
    <div className="settings-section card">
      <div className="settings-section-header">
        <h2 className="settings-section-title">软件升级</h2>
        <button
          className="btn btn-secondary btn-sm"
          onClick={() => { void openUpgradeFolder().catch((e) => reportActionError(e, '打开文件夹')); }}
        >
          打开 updates 文件夹
        </button>
      </div>
      <p className="settings-section-desc">
        当前版本：{current ? `v${current.version}（${current.edition === 'release' ? '正式版' : '开发版'}）` : '读取中…'}。
        把从正规渠道下载的 .upg 升级包拖入下方（或放进 updates 文件夹），验证通过后可一键升级。
      </p>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
        <input
          ref={fileRef}
          type="file"
          accept=".upg"
          style={{ display: 'none' }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void doImport(f);
            e.target.value = '';
          }}
        />
        <button
          className="btn btn-secondary btn-sm"
          disabled={importing}
          onClick={() => fileRef.current?.click()}
        >
          {importing ? '导入中…' : '导入升级包'}
        </button>
        <span style={{ opacity: 0.7 }}>或将 .upg 文件拖入此卡片</span>
      </div>

      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          const f = e.dataTransfer.files?.[0];
          if (f) void doImport(f);
        }}
        style={{ minHeight: 8 }}
      />

      {!loaded ? (
        <div className="settings-loading">加载中…</div>
      ) : packages.length === 0 ? (
        <div className="settings-empty"><p>updates 目录暂无升级包。</p></div>
      ) : (
        packages.map((p) => {
          const badge = badgeFor(p);
          const m = p.manifest;
          return (
            <div key={p.file} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0' }}>
              <span
                title={p.reason || badge.text}
                style={{ color: badge.color, flexShrink: 0, fontWeight: 600 }}
              >
                {badge.text}
              </span>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {p.file}
                {m ? ` → v${m.to_version}（替换 ${m.files_replace ?? '?'} 文件）` : ''}
              </span>
              {p.compatible && (
                confirmFile === p.file ? (
                  <>
                    <button
                      className="btn btn-sm"
                      style={{ background: 'var(--color-error)', color: '#fff' }}
                      disabled={starting}
                      onClick={() => { void doStart(p.file); }}
                    >
                      {starting ? '正在启动…' : '确认升级'}
                    </button>
                    <button
                      className="btn btn-secondary btn-sm"
                      onClick={() => setConfirmFile('')}
                    >
                      取消
                    </button>
                  </>
                ) : (
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={() => setConfirmFile(p.file)}
                  >
                    开始升级
                  </button>
                )
              )}
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => { void doDelete(p.file); }}
              >
                删除
              </button>
            </div>
          );
        })
      )}

      {upgradingTo && (
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 9999,
            background: 'var(--color-bg, #1A1A2E)', color: 'var(--color-text, #fff)',
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: 12,
          }}
        >
          <h2>正在升级到 v{upgradingTo}</h2>
          <p>请勿关闭窗口或断电。升级完成后本页面会自动刷新进入新版本。</p>
        </div>
      )}
    </div>
  );
}
