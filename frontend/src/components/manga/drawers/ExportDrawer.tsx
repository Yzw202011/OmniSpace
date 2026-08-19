/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 导出抽屉（工作台右侧）
 * --------------------------------------------------------------------------
 * 全量真实导出路径：
 *   - 分镜表：GET /manga/storyboard/{pid}/export?format=json|csv
 *   - 资产包：POST /comic/asset/export-pack → zip 经 /manga/media 下载
 *   - 合并包：POST /comic/export/bundle → 分镜+资产+关键帧+视频 单 zip
 *   - 导演台状态：POST /director/export → JSON 存档
 * ========================================================================== */

import { useCallback, useState } from 'react';
import { Download, FileJson, FileSpreadsheet, Package, PackageOpen } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import * as mangaApi from '@/services/mangaApi';
import DrawerFrame from './DrawerFrame';

/** 触发浏览器下载（blob 内容） */
function downloadContent(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/** 触发浏览器下载（远程 URL，经 /manga/media 白名单回读） */
function downloadUrl(filename: string, url: string): void {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

interface ExportDrawerProps {
  onClose: () => void;
}

export function ExportDrawer({ onClose }: ExportDrawerProps) {
  const showToast = useAppStore((s) => s.showToast);
  const rows = useMangaStore((s) => s.rows);
  const assets = useMangaStore((s) => s.assets);
  const currentProject = useMangaStore((s) => s.currentProject);

  const [busy, setBusy] = useState('');

  const errMsg = (err: unknown, fallback: string): string =>
    err && typeof err === 'object' && 'message' in err
      ? (err as { message: string }).message
      : fallback;

  // 分镜表导出（JSON/CSV）
  const handleExportStoryboard = useCallback(
    (format: 'json' | 'csv') => {
      if (!currentProject) return;
      setBusy(`storyboard-${format}`);
      mangaApi
        .exportStoryboard(currentProject.id, format)
        .then((res) => {
          const content =
            format === 'csv' ? (res.content ?? '') : JSON.stringify(res.rows ?? [], null, 2);
          if (!content.trim()) {
            showToast('分镜表为空，无可导出内容', 'warning');
            return;
          }
          downloadContent(
            `storyboard_${currentProject.name}.${format}`,
            content,
            format === 'csv' ? 'text/csv;charset=utf-8' : 'application/json;charset=utf-8',
          );
          showToast(`分镜表已导出（${format.toUpperCase()}，共 ${res.total} 行）`, 'success');
        })
        .catch((err) => showToast(errMsg(err, '分镜表导出失败'), 'error'))
        .finally(() => setBusy(''));
    },
     
    [currentProject, showToast],
  );

  // 资产包导出
  const handleExportAssets = useCallback(() => {
    if (!currentProject) return;
    setBusy('assets');
    mangaApi
      .exportAssetPack(currentProject.id)
      .then((res) => {
        downloadUrl(`assets_${currentProject.name}.zip`, mangaApi.getMediaUrl(res.file_path));
        showToast(`资产包已导出（${res.total ?? assets.length} 个资产）`, 'success');
      })
      .catch((err) => showToast(errMsg(err, '资产包导出失败'), 'error'))
      .finally(() => setBusy(''));
     
  }, [currentProject, assets.length, showToast]);

  // 合并包导出
  const handleExportBundle = useCallback(() => {
    if (!currentProject) return;
    setBusy('bundle');
    mangaApi
      .exportBundle(currentProject.id)
      .then((res) => {
        downloadUrl(`bundle_${currentProject.name}.zip`, mangaApi.getMediaUrl(res.file_path));
        const c = res.contents;
        showToast(
          `合并包已导出：分镜 ${c.storyboard_rows} 行 / 资产 ${c.assets} / 关键帧 ${c.keyframes} / 视频 ${c.videos}`,
          'success',
        );
      })
      .catch((err) => showToast(errMsg(err, '合并包导出失败'), 'error'))
      .finally(() => setBusy(''));
     
  }, [currentProject, showToast]);

  // 导演台状态导出
  const handleExportDirector = useCallback(() => {
    setBusy('director');
    mangaApi
      .exportDirectorStage()
      .then((res) => {
        downloadContent(
          `director_stage_${currentProject?.name ?? 'default'}.json`,
          JSON.stringify(res, null, 2),
          'application/json;charset=utf-8',
        );
        showToast('导演台状态已导出', 'success');
      })
      .catch((err) => showToast(errMsg(err, '导演台状态导出失败'), 'error'))
      .finally(() => setBusy(''));
     
  }, [currentProject, showToast]);

  return (
    <DrawerFrame title="导出" onClose={onClose}>
      <div className="flex flex-col gap-3">
        {/* 分镜表 */}
        <div className="card" style={{ padding: 'var(--space-4)' }}>
          <div className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600 }}>
            <FileSpreadsheet size={15} className="text-[var(--color-primary)]" />
            分镜表
          </div>
          <div style={{ margin: '4px 0 var(--space-3)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
            导出当前项目分镜表（{rows.length} 行）
          </div>
          <div className="flex gap-2">
            <button type="button" className="btn btn-primary btn-sm flex-1" disabled={!!busy} onClick={() => handleExportStoryboard('json')}>
              <FileJson size={13} />
              {busy === 'storyboard-json' ? '导出中…' : 'JSON'}
            </button>
            <button type="button" className="btn btn-secondary btn-sm flex-1" disabled={!!busy} onClick={() => handleExportStoryboard('csv')}>
              {busy === 'storyboard-csv' ? '导出中…' : 'CSV'}
            </button>
          </div>
        </div>

        {/* 资产包 */}
        <div className="card" style={{ padding: 'var(--space-4)' }}>
          <div className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600 }}>
            <Package size={15} className="text-[var(--color-primary)]" />
            资产包
          </div>
          <div style={{ margin: '4px 0 var(--space-3)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
            全部资产（{assets.length} 个）打包为 zip
          </div>
          <button
            type="button"
            className="btn btn-primary btn-sm btn-block"
            disabled={!!busy || assets.length === 0}
            onClick={handleExportAssets}
          >
            <Download size={13} />
            {busy === 'assets' ? '打包中…' : '导出资产包'}
          </button>
        </div>

        {/* 项目合并包 */}
        <div className="card" style={{ padding: 'var(--space-4)' }}>
          <div className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600 }}>
            <PackageOpen size={15} className="text-[var(--color-primary)]" />
            项目合并包
          </div>
          <div style={{ margin: '4px 0 var(--space-3)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
            分镜 + 资产 + 关键帧 + 视频合并为单个 zip，用于归档或迁移
          </div>
          <button type="button" className="btn btn-primary btn-sm btn-block" disabled={!!busy} onClick={handleExportBundle}>
            <Download size={13} />
            {busy === 'bundle' ? '打包中…' : '导出合并包'}
          </button>
        </div>

        {/* 导演台状态 */}
        <div className="card" style={{ padding: 'var(--space-4)' }}>
          <div className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600 }}>
            <FileJson size={15} className="text-[var(--color-primary)]" />
            导演台状态
          </div>
          <div style={{ margin: '4px 0 var(--space-3)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
            场景/机位/角色占位状态 JSON 存档
          </div>
          <button type="button" className="btn btn-secondary btn-sm btn-block" disabled={!!busy} onClick={handleExportDirector}>
            {busy === 'director' ? '导出中…' : '导出导演台状态'}
          </button>
        </div>
      </div>
    </DrawerFrame>
  );
}

export default ExportDrawer;
