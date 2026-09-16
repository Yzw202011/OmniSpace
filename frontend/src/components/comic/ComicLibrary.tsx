// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 漫画作品库（漫画模块 M1，2026-09-07）
 * --------------------------------------------------------------------------
 * /paint 入口的库视图：只看 project_type='comic' 的项目（漫剧库互不可见）。
 * 新建 = 名称 + 画风卡（预置 ART_STYLES + 自定义风格），不预置分镜行——
 * 漫画页的分格由工作台「添加分格」自由创建（漫剧模板分镜不适用）。
 * ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import { BookOpenText, Check, Palette, Plus, Trash2 } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import * as mangaApi from '@/services/mangaApi';
import { ART_STYLES, type ComicProject, type CustomArtStyle } from '@/types';
import { getErrorMessage, reportBgError } from '@/utils/errors';
import { Modal } from '../common/Modal';

function formatTs(ts?: number) {
  if (!ts) return '';
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

const STYLE_LABEL = (key?: string): string =>
  ART_STYLES.find((s) => s.key === key)?.label ?? (key ? '自定义' : '未选择');

interface Props {
  onOpen: (project: ComicProject) => void;
}

export default function ComicLibrary({ onOpen }: Props) {
  const showToast = useAppStore((s) => s.showToast);

  const [projects, setProjects] = useState<ComicProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [artStyle, setArtStyle] = useState(ART_STYLES[0].key);
  const [customStyles, setCustomStyles] = useState<CustomArtStyle[]>([]);
  const [creating, setCreating] = useState(false);

  const [deleteTarget, setDeleteTarget] = useState<ComicProject | null>(null);
  const [deleting, setDeleting] = useState(false);

  // 卸载防护（审计 09-10 P2-9）：卸载后不再 setState/toast
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);

  const loadProjects = () => {
    setLoadError(false);
    setLoading(true);
    mangaApi.listProjects('comic')
      .then((items) => {
        if (!mountedRef.current) return;
        setProjects(items);
        setLoading(false);
      })
      .catch(() => {
        setLoading(false);
        setLoadError(true);
        if (mountedRef.current) showToast('漫画作品列表加载失败', 'error');
      });
  };

  useEffect(() => {
    loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 新建弹窗打开时拉取自定义风格（非阻塞；失败仅隐藏自定义区）
  useEffect(() => {
    if (!createOpen) return;
    mangaApi.listArtStyles()
      .then(setCustomStyles)
      .catch((err: unknown) => reportBgError('comic.listArtStyles', err));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [createOpen]);

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) {
      showToast('请填写漫画名称', 'warning');
      return;
    }
    setCreating(true);
    try {
      const res = await mangaApi.createProject(name, 'comic', undefined, 'regular', artStyle);
      setCreating(false);
      setCreateOpen(false);
      setNewName('');
      onOpen({
        project_id: res.project_id,
        name,
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
        work_mode: 'regular',
        art_style: artStyle,
        project_type: 'comic',
      });
    } catch (err) {
      setCreating(false);
      showToast(getErrorMessage(err, '创建失败'), 'error');
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await mangaApi.deleteProject(deleteTarget.project_id);
      setDeleting(false);
      setDeleteTarget(null);
      showToast('已删除漫画作品', 'success');
      loadProjects();
    } catch (err) {
      setDeleting(false);
      showToast(getErrorMessage(err, '删除失败'), 'error');
    }
  };

  return (
    <div className="page comic-library">
      {/* 页头 */}
      <div className="comic-lib-header">
        <div>
          <h1 className="comic-lib-title"><BookOpenText size={22} /> AI 漫画</h1>
          <p className="comic-lib-sub">
            分格漫画创作：建角色 → 写分格 → 一键出图（角色跨格一致性由参考图锚保障；PuLID 身份锁当前仅用于漫剧关键帧链）
          </p>
        </div>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => setCreateOpen(true)}
        >
          <Plus size={16} /> 新建漫画
        </button>
      </div>

      {/* 列表 */}
      {loading ? (
        <div className="loading-block"><div className="spinner lg" /><div>加载中…</div></div>
      ) : loadError ? (
        <div className="empty-hint">
          列表加载失败。<button type="button" className="btn btn-ghost" onClick={loadProjects}>重试</button>
        </div>
      ) : projects.length === 0 ? (
        <div className="comic-empty card">
          <Palette size={36} style={{ opacity: 0.4 }} />
          <div className="comic-empty-title">还没有漫画作品</div>
          <p className="text-secondary">
            新建一部漫画 → 在工作台创建角色与分格 → 每格写下画面描述即可出图。
            换画风后重新生成分格即得新风格。
          </p>
          <button type="button" className="btn btn-primary" onClick={() => setCreateOpen(true)}>
            <Plus size={16} /> 新建第一部漫画
          </button>
        </div>
      ) : (
        <div className="comic-lib-grid">
          <button type="button" className="comic-card comic-card-new" onClick={() => setCreateOpen(true)}>
            <Plus size={28} />
            <span>新建漫画</span>
          </button>
          {projects.map((p) => (
            <div
              key={p.project_id}
              className="comic-card"
              role="button"
              tabIndex={0}
              onClick={() => onOpen(p)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onOpen(p);
                }
              }}
            >
              <div className="comic-card-cover" data-style={p.art_style ?? ''}>
                <span className="comic-card-style">{STYLE_LABEL(p.art_style)}</span>
              </div>
              <div className="comic-card-body">
                <div className="comic-card-name" title={p.name}>{p.name}</div>
                <div className="comic-card-meta">更新于 {formatTs(p.updated_at)}</div>
              </div>
              <div className="comic-card-actions">
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  title="删除作品"
                  onClick={(e) => {
                    e.stopPropagation();
                    setDeleteTarget(p);
                  }}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 新建弹窗 */}
      {createOpen && (
        <Modal title="新建漫画" onClose={() => !creating && setCreateOpen(false)} width={520}>
          <div className="flex flex-col gap-3">
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>漫画名称</div>
              <input
                className="input"
                value={newName}
                maxLength={100}
                placeholder="如：校园四格 · 第一话"
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void handleCreate();
                }}
              />
            </div>
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>
                漫画画风（生成时注入；后续可随时切换并重新生成分格）
              </div>
              <div className="manga-style-grid">
                {ART_STYLES.map((s) => (
                  <button
                    key={s.key}
                    type="button"
                    className={`manga-style-card${artStyle === s.key ? ' active' : ''}`}
                    onClick={() => setArtStyle(s.key)}
                    title={s.desc}
                  >
                    <span className="style-thumb">
                      <img
                        src={s.thumb}
                        alt={s.label}
                        loading="lazy"
                        onError={(e) => { e.currentTarget.style.display = 'none'; }}
                      />
                      {s.hot && <span className="style-hot">热门</span>}
                      {artStyle === s.key && <span className="style-check"><Check size={12} /></span>}
                    </span>
                    <span className="style-name">{s.label}</span>
                  </button>
                ))}
                {customStyles.map((cs) => (
                  <button
                    key={cs.key}
                    type="button"
                    className={`manga-style-card custom${artStyle === cs.key ? ' active' : ''}`}
                    onClick={() => setArtStyle(cs.key)}
                    title={cs.prompt || cs.name}
                  >
                    <span className="style-thumb">
                      <Palette size={22} style={{ color: 'var(--color-primary)' }} />
                      {artStyle === cs.key && <span className="style-check"><Check size={12} /></span>}
                    </span>
                    <span className="style-name">{cs.name}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="flex justify-end gap-2 mt-1">
              <button type="button" className="btn" onClick={() => setCreateOpen(false)} disabled={creating}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void handleCreate()}
                disabled={creating || !newName.trim()}
              >
                {creating ? '创建中…' : '创建并进入'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* 删除确认 */}
      {deleteTarget && (
        <Modal title="删除漫画作品" onClose={() => !deleting && setDeleteTarget(null)} width={420}>
          <div className="flex flex-col gap-3">
            <p>
              确定删除「<b>{deleteTarget.name}</b>」？分格、关键帧与项目内角色资产将被清理
              （资产转入全局库保留）。此操作不可撤销。
            </p>
            <div className="flex justify-end gap-2">
              <button type="button" className="btn" onClick={() => setDeleteTarget(null)} disabled={deleting}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-danger"
                onClick={() => void handleDelete()}
                disabled={deleting}
              >
                {deleting ? '删除中…' : '确认删除'}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
