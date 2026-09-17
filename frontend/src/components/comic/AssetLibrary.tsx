/* ==========================================================================
 * AssetLibrary.tsx —— 漫画资产库（P2 拆解，自 ComicWorkspace 抽出 2026-09-17）
 * --------------------------------------------------------------------------
 * 角色/场景/道具大图完整显示 + 项目/全局双域 + 上传/AI 生成/删除
 * （2026-09-08 用户令原功能，逻辑零改动原样迁移）。
 * 对外口：pid / open / onClose / showToast / onAssetsChanged（工作台资产
 * 刷新回调）——工作台仅保留 assetLibOpen 开关与触发按钮。
 * ========================================================================== */
import React, { useEffect, useState } from 'react';
import { Trash2 } from 'lucide-react';

import { Modal } from '../common/Modal';
import * as mangaApi from '@/services/mangaApi';
import { getErrorMessage } from '@/utils/errors';
import type { ComicAsset } from '@/types';
import type { ToastLevel } from '@/stores/useAppStore';

interface Props {
  pid: string;
  open: boolean;
  onClose: () => void;
  showToast: (text: string, level?: ToastLevel) => void;
  /** 库内增删后刷新工作台资产（refreshAssets） */
  onAssetsChanged: () => Promise<void> | void;
}

const AssetLibrary: React.FC<Props> = ({
  pid, open, onClose, showToast, onAssetsChanged,
}) => {
  const [libKind, setLibKind] = useState<'character' | 'scene' | 'prop'>('character');
  const [libScope, setLibScope] = useState<'project' | 'global'>('project');
  const [libAssets, setLibAssets] = useState<ComicAsset[]>([]);
  const [libLoading, setLibLoading] = useState(false);
  const [libDeleting, setLibDeleting] = useState<string | null>(null);
  // 库内 AI 生成表单（角色走四视图链，场景/道具走单图）
  const [libGenOpen, setLibGenOpen] = useState(false);
  const [libGenName, setLibGenName] = useState('');
  const [libGenPrompt, setLibGenPrompt] = useState('');
  const [libGenBusy, setLibGenBusy] = useState(false);
  // 库内上传（名称+文件，kind 跟随当前页签）
  const [libUpName, setLibUpName] = useState('');
  const [libUpFile, setLibUpFile] = useState<File | null>(null);
  const [libUpBusy, setLibUpBusy] = useState(false);

  const libKindLabel = libKind === 'character' ? '角色' : libKind === 'scene' ? '场景' : '道具';

  const loadLib = async () => {
    setLibLoading(true);
    try {
      // 全局域按产品面隔离（2026-09-08 用户令）：漫画页只见 face=comic 全局资产
      setLibAssets(await mangaApi.listAssets(
        pid, libKind, libScope, libScope === 'global' ? 'comic' : undefined));
    } catch (err) {
      showToast(getErrorMessage(err, '资产库加载失败'), 'error');
    } finally {
      setLibLoading(false);
    }
  };

  useEffect(() => {
    if (open) void loadLib();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, libKind, libScope]);

  const libDelete = async (assetId: string, name: string) => {
    if (libDeleting) return;
    setLibDeleting(assetId);
    try {
      await mangaApi.deleteAsset(assetId);
      setLibDeleting(null);
      setLibAssets((ls) => ls.filter((a) => a.asset_id !== assetId));
      await onAssetsChanged();
      showToast(`资产「${name}」已删除`, 'success');
    } catch (err) {
      setLibDeleting(null);
      showToast(getErrorMessage(err, '删除失败'), 'error');
    }
  };

  const libUpload = async () => {
    if (!libUpFile) {
      showToast('请选择图片（png/jpg/webp，≤10MB）', 'warning');
      return;
    }
    setLibUpBusy(true);
    try {
      await mangaApi.uploadAsset(pid, libUpName.trim() || '未命名资产', libUpFile, libKind);
      setLibUpBusy(false);
      setLibUpName('');
      setLibUpFile(null);
      await loadLib();
      await onAssetsChanged();
      showToast(`${libKindLabel}已上传`, 'success');
    } catch (err) {
      setLibUpBusy(false);
      showToast(getErrorMessage(err, '上传失败'), 'error');
    }
  };

  const libGenerate = async () => {
    const name = libGenName.trim();
    const prompt = libGenPrompt.trim();
    if (!name || !prompt) {
      showToast('名称与形象描述都要填写', 'warning');
      return;
    }
    setLibGenBusy(true);
    try {
      if (libKind === 'character') {
        await mangaApi.generateTurnaround({ project_id: pid, name, prompt });
      } else {
        await mangaApi.generateAsset({ project_id: pid, kind: libKind, name, prompt });
      }
      setLibGenBusy(false);
      setLibGenOpen(false);
      setLibGenName('');
      setLibGenPrompt('');
      await loadLib();
      await onAssetsChanged();
      showToast(`${libKindLabel}「${name}」已生成`, 'success');
    } catch (err) {
      setLibGenBusy(false);
      showToast(getErrorMessage(err, `${libKindLabel}生成失败`), 'error');
    }
  };

  return (
    <>
      {open && (
        <Modal title="资产库" onClose={onClose} width={860}>
          <div className="comic-asset-lib">
            <div className="comic-lib-toolbar">
              <div className="comic-lib-tabs">
                {([['character', '角色'], ['scene', '场景'], ['prop', '道具']] as const).map(([k, label]) => (
                  <button
                    key={k}
                    type="button"
                    className={libKind === k ? 'active' : ''}
                    onClick={() => setLibKind(k)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="comic-lib-scope">
                {(['project', 'global'] as const).map((sc) => (
                  <button
                    key={sc}
                    type="button"
                    className={libScope === sc ? 'active' : ''}
                    onClick={() => setLibScope(sc)}
                    title={sc === 'global' ? '全局资产跨项目复用（删除项目时自动转入）' : '本项目资产'}
                  >
                    {sc === 'project' ? '本项目' : '全局'}
                  </button>
                ))}
              </div>
            </div>

            {(libKind !== 'character' || libScope === 'global') && (
              <div className="comic-lib-addrow">
                <input
                  className="input"
                  style={{ maxWidth: 160 }}
                  placeholder={`${libKindLabel}名称`}
                  value={libUpName}
                  maxLength={100}
                  onChange={(e) => setLibUpName(e.target.value)}
                />
                <input
                  className="input"
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  onChange={(e) => setLibUpFile(e.target.files?.[0] ?? null)}
                />
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={libUpBusy || !libUpFile}
                  onClick={() => void libUpload()}
                >
                  {libUpBusy ? '上传中…' : `上传${libKindLabel}图`}
                </button>
                <button type="button" className="btn btn-sm" onClick={() => setLibGenOpen(true)}>
                  AI 生成{libKindLabel}
                </button>
              </div>
            )}
            {libKind === 'character' && libScope === 'project' && (
              <div className="comic-lib-addrow">
                <span className="text-secondary" style={{ fontSize: 11 }}>
                  角色 AI 生成走四视图（人脸锁），请用工作台「新建角色」；此处可上传手画/外部角色图
                </span>
                <input
                  className="input"
                  style={{ maxWidth: 160 }}
                  placeholder="角色名称"
                  value={libUpName}
                  maxLength={100}
                  onChange={(e) => setLibUpName(e.target.value)}
                />
                <input
                  className="input"
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  onChange={(e) => setLibUpFile(e.target.files?.[0] ?? null)}
                />
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={libUpBusy || !libUpFile}
                  onClick={() => void libUpload()}
                >
                  {libUpBusy ? '上传中…' : '上传角色图'}
                </button>
              </div>
            )}

            {libLoading ? (
              <div className="loading-block"><div className="spinner" /><div>资产加载中…</div></div>
            ) : libAssets.length === 0 ? (
              <div className="comic-lib-empty text-secondary">
                {libScope === 'global' ? '全局库（漫画页）还没有资产——删除漫画项目时其资产自动转入此处；与漫剧全局库互不可见' : `还没有${libKindLabel}资产`}
              </div>
            ) : (
              <div className="comic-lib-grid">
                {libAssets.map((a) => {
                  const hasTurnaround = Boolean(
                    (a.meta as { turnaround?: unknown } | null)?.turnaround);
                  return (
                    <div key={a.asset_id} className="comic-lib-card">
                      <div className="comic-lib-imgwrap">
                        <img
                          src={mangaApi.getMediaUrl(a.file_path, a.created_at)}
                          alt={a.name}
                          loading="lazy"
                        />
                        {a.kind === 'character' && (
                          <span className={`comic-lib-badge${hasTurnaround ? ' ok' : ''}`}>
                            {hasTurnaround ? '四视图 ✓' : '单图'}
                          </span>
                        )}
                      </div>
                      <div className="comic-lib-meta">
                        <div className="comic-lib-name" title={a.name}>{a.name}</div>
                        <div className="comic-char-prompt" title={a.prompt}>{a.prompt || '（描述词由 AI 自动补写中）'}</div>
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          disabled={libDeleting === a.asset_id}
                          onClick={() => void libDelete(a.asset_id, a.name)}
                        >
                          <Trash2 size={13} />
                          {libDeleting === a.asset_id ? '删除中…' : '删除'}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            <p className="text-secondary" style={{ fontSize: 11, marginTop: 8 }}>
              图片完整显示不裁切。在分格的「＋ 引用资产」里引用：角色作一致性锚，场景/道具作画面参考。
            </p>
          </div>
        </Modal>
      )}

      {/* 资产库 AI 生成（场景/道具单图；角色提示走四视图入口） */}
      {libGenOpen && (
        <Modal title={`AI 生成${libKindLabel}`} onClose={() => !libGenBusy && setLibGenOpen(false)} width={460}>
          <div className="flex flex-col gap-3">
            <input
              className="input"
              placeholder={`${libKindLabel}名称`}
              value={libGenName}
              maxLength={100}
              onChange={(e) => setLibGenName(e.target.value)}
            />
            <textarea
              className="input"
              rows={4}
              maxLength={2000}
              placeholder={libKind === 'scene'
                ? '场景描述，如：黄昏的天台，铁丝网围栏，远处城市天际线，暖色天空'
                : '道具描述，如：一本泛黄的旧日记本，封面有铜锁，微微发光'}
              value={libGenPrompt}
              onChange={(e) => setLibGenPrompt(e.target.value)}
            />
            <div className="flex justify-end gap-2">
              <button type="button" className="btn" onClick={() => setLibGenOpen(false)} disabled={libGenBusy}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void libGenerate()}
                disabled={libGenBusy || !libGenName.trim() || !libGenPrompt.trim()}
              >
                {libGenBusy ? '生成中…（约1~3分钟）' : '生成'}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
};

export default AssetLibrary;
