/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 漫剧作品库（竞品创作空间式）
 * --------------------------------------------------------------------------
 * 页头：大标题 + 副标题 | 搜索框 + 「新建作品」主 CTA
 * 封面网格：首格大号虚线「+ 新建作品」卡，其后 16:9 渐变封面作品卡
 *   （樱粉→深蓝渐变 + 薄荷绿氛围光，hover 上浮 + 霓虹光环 + 重命名/删除浮现）
 * 交互：新建 → Modal（名称/模板/可选剧本）→ 创建后直入编辑器；
 *   空项目（0 行）由编辑器路由到剧本录入页。
 * 契约：GET/POST /manga/projects、PUT rename、DELETE 删除（真实端点）
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { ChevronRight, Clapperboard, Film, FolderPlus, Pencil, Plus, RotateCcw, Search, Trash2 } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import type { ComicProject } from '@/types';
import { Modal } from '../common/Modal';

function formatTs(ts?: number) {
  if (!ts) return '';
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

function getErrMessage(err: unknown, fallback: string) {
  return err && typeof err === 'object' && 'message' in err ? (err as { message: string }).message : fallback;
}

export default function MangaLibrary() {
  const showToast = useAppStore((s) => s.showToast);
  const projects = useMangaStore((s) => s.projects);
  const loading = useMangaStore((s) => s.projectsLoading);
  const fetchProjects = useMangaStore((s) => s.fetchProjects);
  const createProject = useMangaStore((s) => s.createProject);
  const openProject = useMangaStore((s) => s.openProject);
  const renameProject = useMangaStore((s) => s.renameProject);
  const removeProject = useMangaStore((s) => s.removeProject);

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [useTemplate, setUseTemplate] = useState(false);
  const [workMode, setWorkMode] = useState<'regular' | 'narrative'>('regular');
  const [creating, setCreating] = useState(false);
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');

  const [renameTarget, setRenameTarget] = useState<ComicProject | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ComicProject | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [loadError, setLoadError] = useState(false);

  const loadProjects = () => {
    setLoadError(false);
    fetchProjects().catch(() => {
      setLoadError(true);
      showToast('项目列表加载失败', 'error');
    });
  };

  useEffect(() => {
    loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleOpen = (p: ComicProject) => {
    if (openingId) return;
    setOpeningId(p.project_id);
    openProject(p).catch((err: unknown) => {
      showToast(getErrMessage(err, '项目打开失败'), 'error');
      setOpeningId(null);
    });
  };

  const handleCreate = () => {
    const name = newName.trim();
    if (!name) {
      showToast('请输入项目名称', 'warning');
      return;
    }
    setCreating(true);
    createProject(name, useTemplate ? 'comic_drama' : undefined, workMode)
      .then(() => {
        showToast('项目已创建', 'success');
        setCreateOpen(false);
        setNewName('');
        setUseTemplate(false);
        setWorkMode('regular');
      })
      .catch((err: unknown) => showToast(getErrMessage(err, '项目创建失败'), 'error'))
      .finally(() => setCreating(false));
  };

  const handleRename = () => {
    const name = renameValue.trim();
    if (!renameTarget) return;
    if (!name) {
      showToast('请输入新名称', 'warning');
      return;
    }
    setRenaming(true);
    renameProject(renameTarget.project_id, name)
      .then(() => {
        showToast('已重命名', 'success');
        setRenameTarget(null);
      })
      .catch((err: unknown) => showToast(getErrMessage(err, '重命名失败'), 'error'))
      .finally(() => setRenaming(false));
  };

  const handleDelete = () => {
    if (!deleteTarget) return;
    setDeleting(true);
    removeProject(deleteTarget.project_id)
      .then(() => {
        showToast('项目已删除', 'success');
        setDeleteTarget(null);
      })
      .catch((err: unknown) => showToast(getErrMessage(err, '删除失败'), 'error'))
      .finally(() => setDeleting(false));
  };

  const filtered = keyword.trim()
    ? projects.filter((p) => p.name.toLowerCase().includes(keyword.trim().toLowerCase()))
    : projects;

  return (
    <div className="manga-lib">
      {/* 页头（竞品：标题文案 + 搜索 + 主 CTA） */}
      <div className="manga-lib-header">
        <div>
          <h1>漫剧创作</h1>
          <p>从剧本到成片：AI 分镜 · 素材生成 · 3D 导演台 · 视频合成</p>
        </div>
        <div className="manga-lib-tools">
          <div className="manga-lib-search">
            <Search size={14} />
            <input
              className="input"
              value={keyword}
              maxLength={100}
              placeholder="搜索作品…"
              onChange={(e) => setKeyword(e.target.value)}
            />
          </div>
          <button type="button" className="btn btn-primary" onClick={() => setCreateOpen(true)}>
            <FolderPlus size={15} />
            新建作品
          </button>
        </div>
      </div>

      {/* 封面网格（首格新建大卡 + 作品封面卡） */}
      {loading && projects.length === 0 ? (
        <div className="loading-block" style={{ height: 240 }}>
          <span className="spinner" />
          作品库加载中…
        </div>
      ) : loadError && projects.length === 0 ? (
        <div
          className="flex flex-col items-center justify-center"
          style={{ height: 320, gap: 'var(--space-3)' }}
        >
          <span
            className="flex items-center justify-center"
            style={{
              width: 64,
              height: 64,
              borderRadius: 'var(--radius-lg)',
              background: 'rgba(255, 107, 157, 0.1)',
              color: 'var(--color-primary)',
            }}
            aria-hidden="true"
          >
            <Clapperboard size={32} strokeWidth={1.5} />
          </span>
          <span className="text-secondary" style={{ fontSize: 'var(--font-size-sm)' }}>
            无法连接后端服务
          </span>
          <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
            请确认本地服务已启动（默认端口 5800），启动后点击重试
          </span>
          <button type="button" className="btn btn-primary inline-flex items-center gap-1.5" style={{ marginTop: 'var(--space-2)' }} onClick={loadProjects}>
            <RotateCcw size={14} aria-hidden="true" />
            重试
          </button>
        </div>
      ) : (
        <div className="manga-lib-grid">
          <button type="button" className="manga-create-card" onClick={() => setCreateOpen(true)}>
            <span className="manga-create-icon">
              <Plus size={24} />
            </span>
            <span style={{ fontWeight: 600 }}>新建作品</span>
            <span style={{ fontSize: 'var(--font-size-xs)' }}>从剧本开始，AI 自动切分分镜</span>
          </button>

          {filtered.map((p) => (
            <div
              key={p.project_id}
              role="button"
              tabIndex={0}
              className="manga-cover-card"
              onClick={() => handleOpen(p)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleOpen(p);
              }}
              title={p.name}
            >
              <div className="manga-cover">
                <Film size={36} style={{ opacity: 0.85 }} />
                <span className="manga-cover-name">{p.name}</span>
                <span className="manga-cover-actions" onClick={(e) => e.stopPropagation()}>
                  <button
                    type="button"
                    className="btn-icon"
                    title="重命名"
                    aria-label={`重命名 ${p.name}`}
                    onClick={() => {
                      setRenameTarget(p);
                      setRenameValue(p.name);
                    }}
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    type="button"
                    className="btn-icon"
                    title="删除"
                    aria-label={`删除 ${p.name}`}
                    onClick={() => setDeleteTarget(p)}
                  >
                    <Trash2 size={13} />
                  </button>
                </span>
              </div>
              <div className="manga-cover-body flex items-center gap-2">
                <span className="manga-cover-meta flex-1">
                  {openingId === p.project_id ? '打开中…' : `更新于 ${formatTs(p.updated_at)}`}
                </span>
                <ChevronRight size={14} style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }} />
              </div>
            </div>
          ))}

          {filtered.length === 0 && !loading && keyword.trim() && (
            <div className="text-tertiary" style={{ padding: 'var(--space-5) 0', fontSize: 'var(--font-size-sm)' }}>
              没有匹配「{keyword.trim()}」的作品
            </div>
          )}
        </div>
      )}

      {/* 新建作品 */}
      {createOpen && (
        <Modal title="新建漫剧作品" onClose={() => !creating && setCreateOpen(false)} width={520}>
          <div className="flex flex-col gap-3">
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>作品名称</div>
              <input
                className="input"
                value={newName}
                maxLength={100}
                placeholder="如：樱花学园 · 第一话"
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleCreate();
                }}
              />
            </div>
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>作品类型</div>
              <div className="manga-mode-switch">
                <button
                  type="button"
                  className={workMode === 'regular' ? 'active' : ''}
                  onClick={() => setWorkMode('regular')}
                >
                  <span className="mode-title">普通漫剧</span>
                  <span className="mode-desc">5 步流程 · 角色对话式</span>
                </button>
                <button
                  type="button"
                  className={workMode === 'narrative' ? 'active' : ''}
                  onClick={() => setWorkMode('narrative')}
                >
                  <span className="mode-title">解说漫剧</span>
                  <span className="mode-desc">6 步流程 · 旁白解说式</span>
                </button>
              </div>
            </div>
            <label className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-sm)', cursor: 'pointer' }}>
              <input type="checkbox" checked={useTemplate} onChange={(e) => setUseTemplate(e.target.checked)} />
              使用漫剧模板（预置 5 行分镜）
            </label>
            <p className="text-tertiary" style={{ margin: 0, fontSize: 'var(--font-size-xs)' }}>
              创建后进入编辑器，可在剧本录入页粘贴剧本让 AI 自动切分分镜
            </p>
            <div className="flex gap-3" style={{ justifyContent: 'flex-end' }}>
              <button type="button" className="btn btn-ghost" disabled={creating} onClick={() => setCreateOpen(false)}>
                取消
              </button>
              <button type="button" className="btn btn-primary" disabled={creating || !newName.trim()} onClick={handleCreate}>
                {creating ? '创建中…' : '创建并进入'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* 重命名 */}
      {renameTarget && (
        <Modal title="重命名项目" onClose={() => !renaming && setRenameTarget(null)} width={400}>
          <div className="flex flex-col gap-3">
            <input
              className="input"
              value={renameValue}
              maxLength={100}
              autoFocus
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleRename();
              }}
            />
            <div className="flex gap-3" style={{ justifyContent: 'flex-end' }}>
              <button type="button" className="btn btn-ghost" disabled={renaming} onClick={() => setRenameTarget(null)}>
                取消
              </button>
              <button type="button" className="btn btn-primary" disabled={renaming || !renameValue.trim()} onClick={handleRename}>
                {renaming ? '保存中…' : '保存'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* 删除确认 */}
      {deleteTarget && (
        <Modal title="确认删除" onClose={() => !deleting && setDeleteTarget(null)} width={400}>
          <div className="flex flex-col gap-3">
            <p className="text-secondary" style={{ margin: 0, fontSize: 'var(--font-size-sm)' }}>
              确定要删除项目「{deleteTarget.name}」吗？该操作会同时删除其全部分镜与资产，且无法恢复。
            </p>
            <div className="flex gap-3" style={{ justifyContent: 'flex-end' }}>
              <button type="button" className="btn btn-ghost" disabled={deleting} onClick={() => setDeleteTarget(null)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                style={{ background: 'var(--color-error)', borderColor: 'var(--color-error)' }}
                disabled={deleting}
                onClick={handleDelete}
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
