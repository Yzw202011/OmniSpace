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
import { Check, ChevronRight, Clapperboard, Film, FolderPlus, ListChecks, Pencil, Plus, RotateCcw, Search, Trash2, X } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import type { ComicProject } from '@/types';
import { getErrorMessage } from '@/utils/errors';
import { Modal } from '../common/Modal';

function formatTs(ts?: number) {
  if (!ts) return '';
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
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
  const batchRemoveProjects = useMangaStore((s) => s.batchRemoveProjects);

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

  // 批量管理模式：选中集合 + 批删确认
  const [batchMode, setBatchMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [batchDeleting, setBatchDeleting] = useState(false);

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
      showToast(getErrorMessage(err, '项目打开失败'), 'error');
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
      .catch((err: unknown) => showToast(getErrorMessage(err, '项目创建失败'), 'error'))
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
      .catch((err: unknown) => showToast(getErrorMessage(err, '重命名失败'), 'error'))
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
      .catch((err: unknown) => showToast(getErrorMessage(err, '删除失败'), 'error'))
      .finally(() => setDeleting(false));
  };

  const filtered = keyword.trim()
    ? projects.filter((p) => p.name.toLowerCase().includes(keyword.trim().toLowerCase()))
    : projects;

  /* ------------------ 批量管理模式 ------------------ */

  /** 进入/退出批量管理（进入时清空既有选择，退出时同步清理） */
  const toggleBatchMode = () => {
    setBatchMode((v) => !v);
    setSelectedIds(new Set());
  };

  /** 勾选/取消勾选单个项目 */
  const toggleSelect = (projectId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) {
        next.delete(projectId);
      } else {
        next.add(projectId);
      }
      return next;
    });
  };

  /** 全选当前筛选结果 / 取消全选 */
  const selectAll = () => {
    setSelectedIds((prev) =>
      prev.size === filtered.length ? new Set() : new Set(filtered.map((p) => p.project_id)),
    );
  };

  /** 批量删除确认执行：调 store 后按服务端结果收敛本地选择 */
  const handleBatchDelete = () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setBatchDeleting(true);
    batchRemoveProjects(ids)
      .then((res) => {
        if (res.missing_ids.length > 0) {
          showToast(`已删除 ${res.deleted} 个项目，${res.missing_ids.length} 个不存在（已跳过）`, 'warning');
        } else {
          showToast(`已删除 ${res.deleted} 个项目`, 'success');
        }
        setBatchDeleteOpen(false);
        setBatchMode(false);
        setSelectedIds(new Set());
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '批量删除失败'), 'error'))
      .finally(() => setBatchDeleting(false));
  };

  /** 批删确认弹窗内展示的名称列表（最多 5 个 + 省略） */
  const batchDeleteNames = filtered
    .filter((p) => selectedIds.has(p.project_id))
    .map((p) => p.name);
  const batchPreview = batchDeleteNames.slice(0, 5).join('、');
  const batchMore = batchDeleteNames.length - 5;

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
          {projects.length > 0 && (
            <button
              type="button"
              className={`btn ${batchMode ? 'btn-primary' : 'btn-ghost'}`}
              disabled={batchDeleting}
              onClick={toggleBatchMode}
            >
              {batchMode ? <X size={15} /> : <ListChecks size={15} />}
              {batchMode ? '退出管理' : '批量管理'}
            </button>
          )}
          {!batchMode && (
            <button type="button" className="btn btn-primary" onClick={() => setCreateOpen(true)}>
              <FolderPlus size={15} />
              新建作品
            </button>
          )}
        </div>
      </div>

      {/* 批量管理操作条（仅批量模式显示） */}
      {batchMode && (
        <div className="manga-batch-bar">
          <span className="manga-batch-count">
            已选 <strong>{selectedIds.size}</strong> / {filtered.length} 个作品
          </span>
          <button type="button" className="btn btn-ghost" disabled={batchDeleting || filtered.length === 0} onClick={selectAll}>
            <Check size={14} />
            {selectedIds.size === filtered.length && filtered.length > 0 ? '取消全选' : '全选'}
          </button>
          <button
            type="button"
            className="btn manga-batch-delete"
            disabled={batchDeleting || selectedIds.size === 0}
            onClick={() => setBatchDeleteOpen(true)}
          >
            <Trash2 size={14} />
            删除选中{selectedIds.size > 0 ? `（${selectedIds.size}）` : ''}
          </button>
        </div>
      )}

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
        <div className={`manga-lib-grid${batchMode ? ' batching' : ''}`}>
          {!batchMode && (
            <button type="button" className="manga-create-card" onClick={() => setCreateOpen(true)}>
              <span className="manga-create-icon">
                <Plus size={24} />
              </span>
              <span style={{ fontWeight: 600 }}>新建作品</span>
              <span style={{ fontSize: 'var(--font-size-xs)' }}>从剧本开始，AI 自动切分分镜</span>
            </button>
          )}

          {filtered.map((p) => {
            const checked = selectedIds.has(p.project_id);
            return (
              <div
                key={p.project_id}
                role="button"
                tabIndex={0}
                aria-pressed={batchMode ? checked : undefined}
                className={`manga-cover-card${batchMode && checked ? ' selected' : ''}`}
                onClick={() => (batchMode ? toggleSelect(p.project_id) : handleOpen(p))}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    if (batchMode) toggleSelect(p.project_id);
                    else handleOpen(p);
                  }
                }}
                title={p.name}
              >
                <div className="manga-cover">
                  <Film size={36} style={{ opacity: 0.85 }} />
                  <span className="manga-cover-name">{p.name}</span>
                  {batchMode && (
                    <span className={`manga-cover-check${checked ? ' checked' : ''}`} aria-hidden="true">
                      {checked && <Check size={14} />}
                    </span>
                  )}
                  {!batchMode && (
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
                  )}
                </div>
                <div className="manga-cover-body flex items-center gap-2">
                  <span className="manga-cover-meta flex-1">
                    {batchMode
                      ? checked
                        ? '已选中'
                        : '点击勾选'
                      : openingId === p.project_id
                        ? '打开中…'
                        : `更新于 ${formatTs(p.updated_at)}`}
                  </span>
                  {!batchMode && (
                    <ChevronRight size={14} style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }} />
                  )}
                </div>
              </div>
            );
          })}

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
              确定要删除项目「{deleteTarget.name}」吗？其分镜与关键帧将被删除且无法恢复；
              生成的资产将转为全局资产保留，可跨项目继续使用。
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

      {/* 批量删除确认 */}
      {batchDeleteOpen && (
        <Modal title="批量删除确认" onClose={() => !batchDeleting && setBatchDeleteOpen(false)} width={440}>
          <div className="flex flex-col gap-3">
            <p className="text-secondary" style={{ margin: 0, fontSize: 'var(--font-size-sm)' }}>
              确定要删除选中的 <strong>{selectedIds.size}</strong> 个项目吗？其分镜与关键帧将被删除且无法恢复；
              各项目生成的资产将转为全局资产保留，可跨项目继续使用。
            </p>
            <p className="text-tertiary" style={{ margin: 0, fontSize: 'var(--font-size-xs)', wordBreak: 'break-all' }}>
              {batchPreview}
              {batchMore > 0 ? ` 等 ${batchDeleteNames.length} 个项目` : ''}
            </p>
            <div className="flex gap-3" style={{ justifyContent: 'flex-end' }}>
              <button type="button" className="btn btn-ghost" disabled={batchDeleting} onClick={() => setBatchDeleteOpen(false)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary manga-batch-delete"
                disabled={batchDeleting}
                onClick={handleBatchDelete}
              >
                {batchDeleting ? '删除中…' : `确认删除（${selectedIds.size}）`}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
