/* ==========================================================================
 * UpgradeSection —— 设置页「软件升级」卡片
 * --------------------------------------------------------------------------
 * 2026-09-22 标准批 U1-U5 根修：
 *   U1 拖拽导入修复（原 8px 隐形 div → 整卡片拖放区+视觉反馈）
 *   U2 升级过程反馈（消费 /upgrade/status 的 phase 字段显示阶段名）
 *   U3 超时兜底（10 分钟无响应 → 显示重开指引，不再永久白屏）
 *   U5 确认信息增强（展开显示包大小/日期/更新说明）
 * 原有功能保留：当前版本卡+导入（按钮/拖拽/文件夹）+两步确认+全屏等待页。
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
import { reportActionError, reportBgError } from '@/utils/errors';

const MAX_IMPORT_MB = 2048;
/** U3: 超时兜底——10 分钟无响应提示用户手动重开 */
const UPGRADE_TIMEOUT_MS = 10 * 60 * 1000;

/** U2: 阶段名映射（updater state.json 的 phase → 用户可读文案） */
const PHASE_LABELS: Record<string, string> = {
  handed_over: '正在启动升级…',
  handover_wait: '等待服务退出…',
  backing_up: '备份当前版本…',
  applying: '正在替换文件…',
  verifying_start: '验证新版本启动…',
  success: '升级完成，正在重启…',
  rolled_back: '升级失败，已自动恢复旧版',
  failed: '升级失败',
};

function phaseLabel(phase: string | undefined, backendDown: boolean): string {
  /* Bug1 根修（2026-09-22）：后端下线期间 phase 冻结——诚实显示推断态 */
  if (backendDown) return '升级进行中（后端已下线，正在后台操作）';
  if (!phase) return '准备中…';
  return PHASE_LABELS[phase] ?? phase;
}

/** U5: 文件大小格式化 */
function fmtMB(bytes: number | undefined): string {
  if (!bytes || bytes <= 0) return '';
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

/** U5: 日期格式化 */
function fmtDate(created: string | undefined): string {
  if (!created) return '';
  return created.split('T')[0] ?? created;
}

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
  /** U2: 升级阶段（从 /upgrade/status 的 state.phase 读取） */
  const [phase, setPhase] = useState('');
  /** Bug1: 后端下线标记（连续 poll 失败时 true，阶段显示切为推断态） */
  const [backendDown, setBackendDown] = useState(false);
  /** U3: 超时标记 */
  const [timedOut, setTimedOut] = useState(false);
  /** U1: 拖拽视觉反馈 */
  const [dragOver, setDragOver] = useState(false);
  /** U5: 展开详情的包名 */
  const [expandedFile, setExpandedFile] = useState('');
  const fileRef = useRef<HTMLInputElement | null>(null);
  const startTimeRef = useRef(0);
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
      .catch((err: unknown) => reportBgError('升级状态探测', err));
    void refresh();
  }, [refresh]);

  /** 升级等待页：每 3s 探测后端心跳 + U2 阶段 + U3 超时 */
  useEffect(() => {
    if (!upgradingTo) return;
    const oldBuild = current?.build ?? '';
    startTimeRef.current = Date.now();
    setTimedOut(false);

    const timer = window.setInterval(async () => {
      /* U3: 超时检查 */
      if (Date.now() - startTimeRef.current > UPGRADE_TIMEOUT_MS) {
        window.clearInterval(timer);
        setTimedOut(true);
        return;
      }
      try {
        const s = await getUpgradeStatus();
        setBackendDown(false); /* Bug1: 后端复活，恢复精确阶段显示 */
        /* U2: 更新阶段显示 */
        if (s.state?.phase) setPhase(s.state.phase);
        if (s.current.build && s.current.build !== oldBuild) {
          window.clearInterval(timer);
          showToast(`升级完成，已更新到 v${upgradingTo}`, 'success');
          window.location.reload();
        }
      } catch {
        /* Bug1 根修：后端下线→阶段冻结——诚实标记并显示推断态 */
        setBackendDown(true);
        /* silent-intent: 升级期间后端下线属预期，继续等待 */
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
    setPhase('');
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
    <div
      className="settings-section card"
      /* U1: 整个卡片都是拖放区 */
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
        setDragOver(true);
      }}
      onDragLeave={(e) => {
        /* 问题6 根修：仅当真正离开卡片时才取消高亮（子元素间不闪） */
        if (!e.currentTarget.contains(e.relatedTarget as Node)) {
          setDragOver(false);
        }
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        const f = e.dataTransfer.files?.[0];
        if (f) void doImport(f);
      }}
      style={dragOver ? {
        outline: '2px dashed var(--color-primary)',
        outlineOffset: '-4px',
        background: 'var(--color-input-bg)',
      } : undefined}
    >
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
        把 .upg 升级包拖入此卡片（或点击按钮导入），验证通过后可一键升级。
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
        <span style={{ opacity: dragOver ? 1 : 0.7, color: dragOver ? 'var(--color-primary)' : undefined }}>
          {dragOver ? '松开鼠标导入升级包' : '或将 .upg 文件拖入此卡片'}
        </span>
      </div>

      {!loaded ? (
        <div className="settings-loading">加载中…</div>
      ) : packages.length === 0 ? (
        <div className="settings-empty"><p>updates 目录暂无升级包。</p></div>
      ) : (
        packages.map((p) => {
          const badge = badgeFor(p);
          const m = p.manifest;
          const isExpanded = expandedFile === p.file;
          return (
            <div key={p.file}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0' }}>
                <span
                  title={p.reason || badge.text}
                  style={{ color: badge.color, flexShrink: 0, fontWeight: 600 }}
                >
                  {badge.text}
                </span>
                <span
                  style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer' }}
                  onClick={() => setExpandedFile(isExpanded ? '' : p.file)}
                  title="点击展开/收起详情"
                >
                  {p.file}
                  {m ? ` → v${m.to_version}` : ''}
                </span>
                {/* U5: 展开指示器 */}
                <span
                  style={{ cursor: 'pointer', opacity: 0.6, fontSize: 12, flexShrink: 0 }}
                  onClick={() => setExpandedFile(isExpanded ? '' : p.file)}
                >
                  {isExpanded ? '▲ 收起' : '▼ 详情'}
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
              {/* U5: 展开详情 */}
              {isExpanded && (
                <div style={{
                  padding: '8px 12px', margin: '0 0 6px 0',
                  background: 'var(--color-input-bg)', borderRadius: 'var(--radius-sm, 6px)',
                  fontSize: 13, lineHeight: 1.8,
                }}>
                  {m && (
                    <>
                      <div>目标版本：v{m.to_version}{m.to_build_id ? `（${m.to_build_id}）` : ''}</div>
                      <div>替换文件：{m.files_replace ?? '?'} 个 · 包大小：{fmtMB(m.package_bytes ?? m.payload_bytes)}</div>
                      {m.created && <div>打包日期：{fmtDate(m.created)}</div>}
                      {m.from_min && m.from_max && (
                        <div>兼容范围：v{m.from_min} ~ v{m.from_max}</div>
                      )}
                      {m.contains_migration && (
                        <div style={{ color: 'var(--color-warning)' }}>⚠ 含数据库迁移，升级时自动快照</div>
                      )}
                      {p.notes && (
                        <div style={{ marginTop: 4, whiteSpace: 'pre-wrap', opacity: 0.85 }}>
                          更新说明：{p.notes}
                        </div>
                      )}
                    </>
                  )}
                  {p.reason && !p.compatible && (
                    <div style={{ color: 'var(--color-warning)' }}>原因：{p.reason}</div>
                  )}
                </div>
              )}
            </div>
          );
        })
      )}

      {/* U2+U3: 升级等待页（阶段反馈 + 超时兜底） */}
      {upgradingTo && !timedOut && (
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 9999,
            background: 'var(--color-bg, #1A1A2E)', color: 'var(--color-text, #fff)',
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: 12,
          }}
        >
          <h2>正在升级到 v{upgradingTo}</h2>
          {/* U2: 当前阶段 */}
          <p style={{ fontSize: 16, opacity: 0.9 }}>
            {phaseLabel(phase, backendDown)}
          </p>
          {/* 简易进度指示（阶段动画） */}
          <div style={{
            width: 200, height: 4,
            background: 'var(--color-input-bg)',
            borderRadius: 2, overflow: 'hidden',
          }}>
            <div style={{
              width: '40%', height: '100%',
              background: 'var(--color-primary)',
              borderRadius: 2,
              animation: 'upgrade-pulse 2s ease-in-out infinite',
            }} />
          </div>
          <style>{`@keyframes upgrade-pulse { 0%,100% { transform: translateX(-100%);} 50% { transform: translateX(150%);} }`}</style>
          <p style={{ opacity: 0.6, fontSize: 13 }}>请勿关闭窗口或断电。升级完成后本页面会自动刷新。</p>
        </div>
      )}

      {/* U3: 超时兜底页面 */}
      {upgradingTo && timedOut && (
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 9999,
            background: 'var(--color-bg)', color: 'var(--color-text)',
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: 16,
          }}
        >
          <h2 style={{ color: 'var(--color-warning)' }}>⏱ 升级等待超时</h2>
          <p>升级过程已超过 10 分钟未完成。可能是升级仍在后台进行，</p>
          <p>也可能是启动过程中遇到了问题。</p>
          <p style={{ fontWeight: 600, marginTop: 8 }}>
            请关闭本窗口，然后重新双击桌面上的 OmniSpace 图标启动。
          </p>
          <p style={{ opacity: 0.6, fontSize: 13 }}>
            如果重新启动后版本未变化，请联系技术支持。
          </p>
        </div>
      )}
    </div>
  );
}
