/**
 * PluginSection 插件管理卡片（设置页，2026-09-16 用户拍板「插件导入」）
 * --------------------------------------------------------------------------
 * 清单 / 导入（.CuteMamen 包 + 可选 .py 源码）/ 启停 / 删除；
 * 信任徽章三档：出厂·已审查（蓝）/ 用户·纯数据（绿）/ 用户·含源码（橙）。
 * 含源码档导入前强制大白话确认（进程内插件与后端同权限，规范 §1）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Database,
  Loader2,
  Package,
  Puzzle,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  Upload,
} from 'lucide-react';
import {
  deletePlugin,
  disablePlugin,
  enablePlugin,
  importPlugin,
  listPlugins,
  type PluginInfo,
} from '../services/pluginApi';
import { useAppStore } from '../stores/useAppStore';
import { isApiError } from '../services/api';

/** 信任徽章配色（CSS 变量令牌，禁止硬编码 hex） */
const BADGE_STYLE: Record<string, { color: string; icon: typeof ShieldCheck }> = {
  factory: { color: 'var(--color-accent)', icon: ShieldCheck },
  user_data: { color: 'var(--color-success)', icon: Database },
  user_source: { color: 'var(--color-warning)', icon: ShieldAlert },
};

export default function PluginSection() {
  const showToast = useAppStore((s) => s.showToast);
  const [plugins, setPlugins] = useState<PluginInfo[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pkgFile, setPkgFile] = useState<File | null>(null);
  // 含源码确认门：包内带源码时后端要求确认，前端弹门后重试
  const [pendingConfirm, setPendingConfirm] = useState(false);
  const pkgInputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    setLoadFailed(false);
    try {
      setPlugins(await listPlugins());
    } catch (err) {
      setLoadFailed(true);
      showToast('插件清单加载失败，请重试', 'error');
      if (!isApiError(err)) {
        console.error('[PluginSection] 清单加载失败', err);
      }
    }
  }, [showToast]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /** 文件选择：先快照 File 再清 value（FileList 活引用坑，2026-09-08 教训） */
  const onPickPkg = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] ?? null;
    setPkgFile(file);
    e.target.value = '';
  };

  const doImport = async (confirmSource: boolean) => {
    if (!pkgFile) return;
    setBusy(true);
    try {
      const result = await importPlugin(pkgFile, confirmSource);
      showToast(`插件已导入：${result.name}（${result.trust_label}）`, 'success');
      setPkgFile(null);
      setPendingConfirm(false);
      await refresh();
    } catch (err) {
      if (isApiError(err)) {
        if (err.code === 'PLUGIN_SOURCE_CONFIRM_REQUIRED') {
          setPendingConfirm(true); // 后端要求确认 → 弹确认门
        } else {
          showToast(err.message || '插件导入失败', 'error');
        }
      } else {
        showToast('插件导入失败，请重试', 'error');
        console.error('[PluginSection] 导入失败', err);
      }
    } finally {
      setBusy(false);
    }
  };

  const onImportClick = () => {
    void doImport(false); // 包内是否带源码由后端判定，需要确认会回弹
  };

  const onToggle = async (p: PluginInfo) => {
    setBusy(true);
    try {
      if (p.enabled) {
        await disablePlugin(p.name);
        showToast(`已停用 ${p.name}`, 'success');
      } else {
        await enablePlugin(p.name);
        showToast(`已启用 ${p.name}`, 'success');
      }
      await refresh();
    } catch (err) {
      showToast(isApiError(err) ? err.message : '操作失败，请重试', 'error');
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (p: PluginInfo) => {
    if (!window.confirm(`确定删除插件「${p.name}」？此操作不可恢复。`)) {
      return;
    }
    setBusy(true);
    try {
      await deletePlugin(p.name);
      showToast(`已删除 ${p.name}`, 'success');
      await refresh();
    } catch (err) {
      showToast(isApiError(err) ? err.message : '删除失败，请重试', 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-section card">
      <h2 className="settings-section-title">
        <Puzzle size={16} style={{ verticalAlign: '-2px' }} /> 插件
      </h2>
      <p className="settings-section-desc">
        管理本机插件。蓝徽章=出厂·已审查；绿徽章=用户·纯数据（不引入新代码）；
        橙徽章=用户·含源码（<strong>将在软件内部直接运行，请只安装信任来源</strong>）。
        开发规范见 docs/插件开发规范.md。
      </p>

      {/* 导入区 */}
      <div className="settings-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 'var(--space-2)' }}>
        <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', alignItems: 'center' }}>
          <button
            type="button"
            className="btn btn-outline"
            onClick={() => pkgInputRef.current?.click()}
          >
            <Package size={14} /> {pkgFile ? pkgFile.name : '选择插件包(.CuteMamen)'}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!pkgFile || busy}
            onClick={onImportClick}
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Upload size={14} />}
            导入
          </button>
        </div>
        <input ref={pkgInputRef} type="file" accept=".CuteMamen" hidden onChange={onPickPkg} />
        {pendingConfirm && (
          <div className="settings-row" style={{ border: '1px solid var(--color-warning)', borderRadius: 'var(--radius-md)', padding: 'var(--space-3)' }}>
            <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'flex-start' }}>
              <ShieldAlert size={18} style={{ color: 'var(--color-warning)', flexShrink: 0, marginTop: 2 }} />
              <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
                <span>
                  「{pkgFile?.name}」这个插件包<strong>内含源码</strong>。它将在软件内部直接运行，
                  权限与软件本身相同——<strong>请只安装信任来源的插件</strong>。
                </span>
                <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                  <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void doImport(true)}>
                    我信任此来源，确认导入
                  </button>
                  <button type="button" className="btn btn-outline" disabled={busy} onClick={() => setPendingConfirm(false)}>
                    取消
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 清单区 */}
      {plugins === null ? (
        loadFailed ? (
          <div className="settings-row">
            <span>插件清单加载失败。</span>
            <button type="button" className="btn btn-outline" onClick={() => void refresh()}>
              重试
            </button>
          </div>
        ) : (
          <div className="settings-row">
            <Loader2 size={14} className="animate-spin" /> 加载中…
          </div>
        )
      ) : plugins.length === 0 ? (
        <div className="settings-row">暂无插件。</div>
      ) : (
        <div style={{ display: 'grid', gap: 'var(--space-2)', marginTop: 'var(--space-2)' }}>
          {plugins.map((p) => {
            const badge = BADGE_STYLE[p.origin] ?? BADGE_STYLE.user_data;
            const BadgeIcon = badge.icon;
            return (
              <div
                key={p.name}
                className="settings-row"
                style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 'var(--space-2)' }}
              >
                <div style={{ display: 'grid', gap: 2, minWidth: 220 }}>
                  <span style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
                    <strong>{p.name}</strong>
                    <span
                      style={{ color: badge.color, fontSize: 12, display: 'inline-flex', gap: 4, alignItems: 'center' }}
                    >
                      <BadgeIcon size={12} /> {p.trust_label}
                    </span>
                    {p.state === 'faulty' && (
                      <span style={{ color: 'var(--color-error)', fontSize: 12 }}>故障态</span>
                    )}
                  </span>
                  <span style={{ fontSize: 12, opacity: 0.75 }}>
                    {p.capability || '（无能力描述）'}
                    {p.last_error ? ` · 上次错误：${p.last_error}` : ''}
                  </span>
                </div>
                <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
                  <button
                    type="button"
                    className="btn btn-outline"
                    disabled={busy}
                    onClick={() => void onToggle(p)}
                  >
                    {p.enabled ? '停用' : '启用'}
                  </button>
                  {p.origin === 'user' && (
                    <button
                      type="button"
                      className="btn btn-outline"
                      disabled={busy}
                      style={{ color: 'var(--color-error)' }}
                      onClick={() => void onDelete(p)}
                    >
                      <Trash2 size={14} /> 删除
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
