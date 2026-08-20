/* ==========================================================================
 * ModelManager.tsx —— 模型管理主视图
 * --------------------------------------------------------------------------
 * 按类别分组展示模型（dialog/video/voice/vision/language/3d/auxiliary）
 * 每个模型显示: 名称、用途、大小、参数量、最低显存、状态
 * 导入/校验/卸载/选择操作
 * 手动选择模型（规格 §14 约束10）
 * 数据源：useModelStore（GET /v1/models）；
 * VRAM 使用率：useHardwareStore（/v1/hardware/realtime 遥测，/models/vram 端点不存在）
 * ========================================================================== */

import React, { useState, useEffect } from 'react';
import type { LucideIcon } from 'lucide-react';
import {
  PackageOpen,
  FolderOpen,
  MessageSquare,
  Clapperboard,
  Mic,
  Eye,
  FileText,
  Box,
  Wrench,
  Boxes,
  AlertTriangle,
  Import,
  Check,
} from 'lucide-react';
import { useModelStore } from '@/stores/useModelStore';
import { useHardwareStore } from '@/stores/useHardwareStore';
import { useAppStore } from '@/stores/useAppStore';
import { categoryToFeature } from '@/services/modelApi';
import { MODEL_RUNTIME_STATUS_LABELS } from '@/constants/statusLabels';
import { Modal } from '@/components/common/Modal';
import type { ModelInfo, ModelCategory } from '@/types';

/** 类别 → 中文标签 */
const CATEGORY_LABELS: Record<ModelCategory, string> = {
  dialog: '对话模型',
  video: '视频模型',
  voice: '语音模型',
  vision: '视觉模型',
  language: '语言模型',
  '3d': '3D 模型',
  auxiliary: '辅助模型',
};

/** 类别图标（Lucide SVG；all 为「全部」筛选标签） */
const CATEGORY_ICONS: Record<ModelCategory | 'all', LucideIcon> = {
  all: FolderOpen,
  dialog: MessageSquare,
  video: Clapperboard,
  voice: Mic,
  vision: Eye,
  language: FileText,
  '3d': Box,
  auxiliary: Wrench,
};

/** 类别图标渲染器（统一尺寸与主色，随激活态变色） */
const CategoryIcon: React.FC<{ category: ModelCategory | 'all'; size?: number }> = ({
  category,
  size = 14,
}) => {
  const Icon = CATEGORY_ICONS[category] ?? PackageOpen;
  return <Icon size={size} aria-hidden="true" />;
};

/**
 * 模型管理主视图组件
 * 按类别分组展示模型，支持导入、校验、卸载、选择操作。
 */
export const ModelManager: React.FC = () => {
  const models = useModelStore((s) => s.models);
  const loading = useModelStore((s) => s.loading);
  const fetchModels = useModelStore((s) => s.fetchModels);
  const verifyModel = useModelStore((s) => s.verifyModel);
  const unloadModel = useModelStore((s) => s.unloadModel);
  const selectModel = useModelStore((s) => s.selectModel);
  const loadModel = useModelStore((s) => s.loadModel);
  const deleteModel = useModelStore((s) => s.deleteModel);
  const importModel = useModelStore((s) => s.importModel);

  const [activeCategory, setActiveCategory] = useState<ModelCategory | 'all'>('all');
  const [selectedId, setSelectedId] = useState<string>('');
  const [busy, setBusy] = useState('');
  /** 搜索关键词（MODEL-004：名称/ID/用途模糊匹配） */
  const [searchQuery, setSearchQuery] = useState('');
  /** 排序键（MODEL-002：默认保持后端登记顺序） */
  const [sortKey, setSortKey] = useState<'default' | 'name' | 'size_desc' | 'size_asc' | 'vram'>('default');
  /** 导入模型弹窗 */
  const [importOpen, setImportOpen] = useState(false);
  const [importPath, setImportPath] = useState('');
  const [importBusy, setImportBusy] = useState(false);
  /** 删除确认弹窗目标 */
  const [deleteTarget, setDeleteTarget] = useState<ModelInfo | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  // VRAM 使用率：优先实时遥测（WS system_status），其次协同轮询快照
  const vramUsage = useHardwareStore(
    (s) => s.realtime?.vram_percent ?? s.synergy?.vram?.percent ?? null,
  );

  // 首次挂载：拉取模型列表
  useEffect(() => {
    fetchModels();
  }, [fetchModels]);

  /** 搜索过滤（MODEL-004）：名称/ID/用途不区分大小写匹配 */
  const kw = searchQuery.trim().toLowerCase();
  const filteredModels = kw
    ? models.filter(
        (m) =>
          m.name.toLowerCase().includes(kw) ||
          m.id.toLowerCase().includes(kw) ||
          (m.purpose || '').toLowerCase().includes(kw),
      )
    : models;

  /** 排序（MODEL-002）：default 保持后端登记顺序，其余为稳定拷贝排序 */
  const sortModels = (list: ModelInfo[]): ModelInfo[] => {
    if (sortKey === 'default') return list;
    const arr = [...list];
    switch (sortKey) {
      case 'name':
        return arr.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'));
      case 'size_desc':
        return arr.sort((a, b) => (b.size_gb || 0) - (a.size_gb || 0));
      case 'size_asc':
        return arr.sort((a, b) => (a.size_gb || 0) - (b.size_gb || 0));
      case 'vram':
        return arr.sort((a, b) => (a.min_vram_gb || 0) - (b.min_vram_gb || 0));
      default:
        return arr;
    }
  };

  /** 按类别分组（搜索过滤后） */
  const groupedModels = filteredModels.reduce<Record<string, ModelInfo[]>>((acc, model) => {
    if (!acc[model.category]) acc[model.category] = [];
    acc[model.category].push(model);
    return acc;
  }, {});
  Object.keys(groupedModels).forEach((cat) => {
    groupedModels[cat] = sortModels(groupedModels[cat]);
  });

  /** 当前显示的模型列表 */
  const displayModels = activeCategory === 'all'
    ? filteredModels
    : sortModels(groupedModels[activeCategory] || []);

  /** 选择模型（规格 §14 约束10：手动选择；PUT /v1/models/select） */
  const handleSelect = async (model: ModelInfo) => {
    const { showToast } = useAppStore.getState();
    if (model.status !== 'ready') {
      showToast('模型未就绪，无法选择', 'warning');
      return;
    }
    // 模型类别映射为后端合法功能名（dialog/paint/video/voice）
    const feature = categoryToFeature(model.category);
    if (!feature) {
      showToast('该类别模型不支持手动选择', 'warning');
      return;
    }
    setBusy(model.id);
    try {
      await selectModel(model.id, feature);
      setSelectedId(model.id);
    } catch {
      showToast('选择模型失败', 'error');
    } finally {
      setBusy('');
    }
  };

  /** 校验模型（SHA256 指纹，POST /v1/models/{id}/verify） */
  const handleVerify = async (model: ModelInfo) => {
    setBusy(model.id);
    const { showToast } = useAppStore.getState();
    try {
      const res = await verifyModel(model.id);
      if (res.verified) {
        showToast(`模型校验通过（SHA256：${res.sha256.slice(0, 16)}…）`, 'success');
      } else {
        showToast('模型校验未通过', 'error');
      }
    } catch {
      showToast('校验请求失败', 'error');
    } finally {
      setBusy('');
    }
  };

  /** 卸载模型（POST /v1/models/unload） */
  const handleUnload = async (model: ModelInfo) => {
    setBusy(model.id);
    try {
      await unloadModel(model.id);
      if (selectedId === model.id) setSelectedId('');
      await fetchModels();
    } catch {
      useAppStore.getState().showToast('卸载失败', 'error');
    } finally {
      setBusy('');
    }
  };

  /** 加载模型到 GPU（POST /v1/models/load；错误码 20xxx 由 toast 如实透出） */
  const handleLoad = async (model: ModelInfo) => {
    setBusy(model.id);
    const { showToast } = useAppStore.getState();
    try {
      await loadModel(model.id);
      showToast(`模型「${model.name}」已加载到显存`, 'success');
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '加载失败';
      showToast(msg, 'error');
    } finally {
      setBusy('');
    }
  };

  /** 删除模型（DELETE /v1/models/{id}：仅从注册表移除并自动卸载，不删磁盘权重） */
  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    const { showToast } = useAppStore.getState();
    try {
      await deleteModel(deleteTarget.id);
      if (selectedId === deleteTarget.id) setSelectedId('');
      showToast(`模型「${deleteTarget.name}」已从注册表移除`, 'success');
      setDeleteTarget(null);
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '删除失败';
      showToast(msg, 'error');
    } finally {
      setDeleteBusy(false);
    }
  };

  /** 导入模型（POST /v1/models/import，body: {path}；路径不存在返回 30001） */
  const handleImport = async () => {
    const path = importPath.trim();
    const { showToast } = useAppStore.getState();
    if (!path) {
      showToast('请输入权重文件或目录路径', 'warning');
      return;
    }
    setImportBusy(true);
    try {
      const model = await importModel({ path });
      showToast(`模型「${model.name || model.id}」导入成功`, 'success');
      setImportOpen(false);
      setImportPath('');
      await fetchModels();
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '导入失败';
      showToast(msg, 'error');
    } finally {
      setImportBusy(false);
    }
  };

  /** 类别列表 */
  const categories: Array<ModelCategory | 'all'> = [
    'all', 'dialog', 'video', 'voice', 'vision', 'language', '3d', 'auxiliary',
  ];

  /** VRAM 警告（>95% 强制线，规格 COM-012）；P1-05：探测失败为 null，不触发告警 */
  const vramWarning = vramUsage != null && vramUsage >= 95;
  const vramFillClass = vramUsage == null ? '' : vramUsage >= 95 ? 'critical' : vramUsage >= 85 ? 'danger' : '';

  return (
    <div className="page model-manager">
      <div className="mm-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h1 className="page-title" style={{ marginBottom: 0 }}><Boxes size={20} aria-hidden="true" /> 模型管理</h1>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-4)', flex: 1, marginLeft: 'var(--space-6)' }}>
          {/* VRAM 监控 */}
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-secondary)' }}>VRAM</span>
          <div className="vram-usage-bar" style={{ flex: 1, maxWidth: 320 }}>
            <div className={`vram-usage-fill ${vramFillClass}`} style={{ width: `${vramUsage == null ? 0 : Math.min(100, vramUsage)}%` }} />
            <div className="vram-warning-line" style={{ left: '95%' }} />
          </div>
          <span style={{ fontSize: 'var(--font-size-sm)', minWidth: 40 }}>{vramUsage == null ? '--' : `${Math.round(vramUsage)}%`}</span>
          {vramWarning && (
            <span style={{ color: 'var(--color-error)', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <AlertTriangle size={13} aria-hidden="true" /> 强制线
            </span>
          )}
        </div>
        <button className="btn btn-primary btn-sm" onClick={() => setImportOpen(true)}>
          <Import size={14} aria-hidden="true" /> 导入模型
        </button>
      </div>

      {/* 搜索 + 排序工具栏（MODEL-004 / MODEL-002） */}
      <div style={{ display: 'flex', gap: 'var(--space-3)', marginBottom: 'var(--space-3)', alignItems: 'center' }}>
        <input
          type="search"
          className="mm-search-input"
          placeholder="搜索模型名称 / ID / 用途…"
          aria-label="搜索模型"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          style={{
            flex: 1,
            maxWidth: 360,
            padding: '6px 10px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--color-input-border)',
            background: 'var(--color-input-bg)',
            color: 'var(--color-text-primary)',
            fontSize: 'var(--font-size-sm)',
          }}
        />
        <label style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-secondary)', display: 'flex', alignItems: 'center', gap: 6 }}>
          排序
          <select
            className="mm-sort-select"
            aria-label="模型排序"
            value={sortKey}
            onChange={(e) => setSortKey(e.target.value as typeof sortKey)}
            style={{
              padding: '6px 8px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid var(--color-input-border)',
              background: 'var(--color-input-bg)',
              color: 'var(--color-text-primary)',
              fontSize: 'var(--font-size-sm)',
            }}
          >
            <option value="default">默认（登记顺序）</option>
            <option value="name">名称</option>
            <option value="size_desc">大小（从大到小）</option>
            <option value="size_asc">大小（从小到大）</option>
            <option value="vram">最低显存需求</option>
          </select>
        </label>
        {kw && (
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)' }}>
            匹配 {filteredModels.length}/{models.length}
          </span>
        )}
      </div>

      {/* 类别筛选 */}
      <div className="model-category-tabs">
        {categories.map((cat) => (
          <button
            key={cat}
            className={`model-category-tab ${activeCategory === cat ? 'active' : ''}`}
            onClick={() => setActiveCategory(cat)}
          >
            <CategoryIcon category={cat} />
            <span>{cat === 'all' ? '全部' : CATEGORY_LABELS[cat]}</span>
            {cat !== 'all' && groupedModels[cat] ? ` (${groupedModels[cat].length})` : ''}
          </button>
        ))}
      </div>

      {/* 模型列表 */}
      {loading && models.length === 0 ? (
        <p style={{ color: 'var(--color-text-secondary)' }}>模型列表加载中…</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
          {activeCategory === 'all' ? (
            (Object.keys(CATEGORY_LABELS) as ModelCategory[]).map((cat) => {
              const catModels = groupedModels[cat] || [];
              if (catModels.length === 0) return null;
              return (
                <section key={cat}>
                  <h3
                    className="card-title"
                    style={{ marginBottom: 'var(--space-2)', display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}
                  >
                    <CategoryIcon category={cat} size={16} />
                    {CATEGORY_LABELS[cat]}（{catModels.length}）
                  </h3>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
                    {catModels.map((model) => renderModelCard(model))}
                  </div>
                </section>
              );
            })
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
              {displayModels.map((model) => renderModelCard(model))}
            </div>
          )}

          {displayModels.length === 0 && (
            <div
              style={{
                textAlign: 'center',
                padding: 'var(--space-8)',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 'var(--space-3)',
              }}
            >
              <span
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  width: 64,
                  height: 64,
                  borderRadius: 'var(--radius-lg)',
                  background: 'rgba(255, 107, 157, 0.1)',
                  color: 'var(--color-primary)',
                }}
              >
                <PackageOpen size={32} strokeWidth={1.5} aria-hidden="true" />
              </span>
              <p style={{ color: 'var(--color-text-secondary)', margin: 0 }}>
                {kw
                  ? `无匹配「${searchQuery.trim()}」的模型`
                  : activeCategory === 'all'
                    ? '暂无模型'
                    : `暂无${CATEGORY_LABELS[activeCategory]}`}
              </p>
              <p style={{ color: 'var(--color-text-tertiary)', fontSize: 'var(--font-size-xs)', margin: 0 }}>
                点击右上角「导入模型」将本地模型目录接入系统，或调整筛选与搜索条件
              </p>
            </div>
          )}
        </div>
      )}

      {/* VRAM 强制线警告 */}
      {vramWarning && (
        <div style={{ padding: 'var(--space-3)', borderRadius: 'var(--radius-sm)', background: 'var(--color-error)', color: 'var(--color-text-primary)', display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
          <AlertTriangle size={15} aria-hidden="true" />
          VRAM 使用率已达 {Math.round(vramUsage)}%，超过 95% 强制线！请卸载不使用的模型释放显存。
        </div>
      )}

      {/* 导入模型弹窗（POST /v1/models/import，后端仅消费 path 字段） */}
      {importOpen && (
        <Modal
          title="导入模型"
          onClose={() => !importBusy && setImportOpen(false)}
          footer={
            <>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => setImportOpen(false)}
                disabled={importBusy}
              >
                取消
              </button>
              <button
                className="btn btn-primary btn-sm"
                onClick={handleImport}
                disabled={importBusy || !importPath.trim()}
              >
                {importBusy ? '导入中…' : '导入'}
              </button>
            </>
          }
        >
          <div className="form-row">
            <label className="form-label" htmlFor="mm-import-path">权重文件或目录路径</label>
            {/* MODEL-013：拖拽区。浏览器安全沙箱不暴露被拖目录的绝对路径，
                故拖拽仅自动带入名称并保留用户已填的路径前缀，如实提示，不伪造路径 */}
            <div
              className="mm-dropzone"
              onDragOver={(e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'copy';
              }}
              onDrop={(e) => {
                e.preventDefault();
                let name = '';
                const item = e.dataTransfer.items?.[0];
                const entry = item?.webkitGetAsEntry?.();
                if (entry) name = entry.name;
                else if (e.dataTransfer.files.length > 0) name = e.dataTransfer.files[0].name;
                if (!name) return;
                setImportPath((prev) => {
                  const prefix = /^([A-Za-z]:[\\/].*[\\/]|models[\\/])/.exec(prev);
                  return prefix ? prefix[1] + name : `models\\${name}`;
                });
                useAppStore.getState().showToast(
                  '已带入拖入项名称；浏览器沙箱无法读取绝对路径，请确认路径前缀',
                  'info',
                );
              }}
              style={{
                border: '1px dashed var(--color-input-border)',
                borderRadius: 'var(--radius-sm)',
                padding: 'var(--space-3)',
                textAlign: 'center',
                fontSize: 'var(--font-size-sm)',
                color: 'var(--color-text-tertiary)',
                marginBottom: 'var(--space-2)',
              }}
            >
              将模型目录 / 权重文件拖到此处（自动带入名称，默认前缀 models\）
            </div>
            <input
              id="mm-import-path"
              className="input"
              placeholder="如 models/qwen3-vl-4b 或 D:\weights\model.safetensors"
              value={importPath}
              disabled={importBusy}
              onChange={(e) => setImportPath(e.target.value)}
            />
            <div className="form-hint">
              相对路径以项目根目录解析；后端将自动推断名称、类别与大小并校验路径存在性。
            </div>
          </div>
        </Modal>
      )}

      {/* 删除确认弹窗（DELETE /v1/models/{id}：仅移除注册表记录，不删磁盘权重） */}
      {deleteTarget && (
        <Modal
          title="删除模型"
          onClose={() => !deleteBusy && setDeleteTarget(null)}
          footer={
            <>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => setDeleteTarget(null)}
                disabled={deleteBusy}
              >
                取消
              </button>
              <button
                className="btn btn-primary btn-sm"
                onClick={handleDelete}
                disabled={deleteBusy}
              >
                {deleteBusy ? '删除中…' : '确认删除'}
              </button>
            </>
          }
        >
          <p className="text-sm">
            确认从模型注册表移除「{deleteTarget.name}」？
            {deleteTarget.loaded ? '该模型当前已加载，删除前将自动卸载。' : ''}
          </p>
          <p className="text-tertiary mt-2" style={{ fontSize: 'var(--font-size-xs)' }}>
            此操作仅移除注册表记录，不会删除磁盘上的权重文件。
          </p>
        </Modal>
      )}
    </div>
  );

  /** 渲染单个模型卡片 */
  function renderModelCard(model: ModelInfo): React.ReactNode {
    const isSelected = selectedId === model.id;
    const isBusy = busy === model.id;
    const statusKey = model.loaded ? 'loaded' : model.status;
    const statusText = model.loaded ? '已加载' : (MODEL_RUNTIME_STATUS_LABELS[model.status] || model.status);
    return (
      <div key={model.id} className={`model-card ${isSelected ? 'selected' : ''}`}>
        <div className="model-info">
          <div className="model-name">
            {model.name}
            {' '}
            <span className={`model-status-badge ${statusKey}`}>{statusText}</span>
            {isSelected && <span className="model-status-badge selected">已选择</span>}
            {!model.downloaded && <span className="model-status-badge not_ready">未下载</span>}
          </div>
          <div className="model-meta">
            <span>{model.purpose || '—'}</span>
            <span>大小 {model.size_gb ? `${model.size_gb.toFixed(1)} GB` : '—'}</span>
            <span>参数量 {model.params || '—'}</span>
            <span>最低显存 {model.min_vram_gb ? `${model.min_vram_gb} GB` : '—'}</span>
          </div>
        </div>
        <div className="model-actions">
          {/* 选择按钮（规格 §14 约束10：手动选择；3d/auxiliary 类别无对应功能，不可选） */}
          <button
            className={`btn btn-sm ${isSelected ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => handleSelect(model)}
            disabled={isBusy || model.status !== 'ready' || !categoryToFeature(model.category)}
            title={
              !categoryToFeature(model.category)
                ? '该类别不支持手动选择'
                : model.status === 'ready'
                  ? '选择此模型'
                  : '模型未就绪'
            }
          >
            {isSelected ? (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><Check size={13} aria-hidden="true" /> 已选择</span>
            ) : (
              '选择'
            )}
          </button>

          {/* 加载（POST /v1/models/load；未下载/已加载置灰） */}
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => handleLoad(model)}
            disabled={isBusy || model.loaded || !model.downloaded}
            title={
              model.loaded
                ? '模型已加载'
                : !model.downloaded
                  ? '模型未下载，无法加载'
                  : '加载模型到显存'
            }
          >
            加载
          </button>

          {/* 校验 */}
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => handleVerify(model)}
            disabled={isBusy || !model.downloaded}
            title="校验模型完整性"
          >
            校验
          </button>

          {/* 卸载 */}
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => handleUnload(model)}
            disabled={isBusy || !model.loaded}
            title="卸载模型释放显存"
          >
            卸载
          </button>

          {/* 删除（DELETE /v1/models/{id}，仅移除注册表记录，弹窗确认） */}
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => setDeleteTarget(model)}
            disabled={isBusy}
            title="从注册表移除（不删除磁盘权重文件）"
            style={{ color: 'var(--color-error)' }}
          >
            删除
          </button>
        </div>
      </div>
    );
  }
};

export default ModelManager;
