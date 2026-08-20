/* ==========================================================================
 * InspectorPanel.tsx —— 漫剧编辑器右栏·分镜检查器（竞品式属性面板）
 * --------------------------------------------------------------------------
 * 选中分镜行后出现，聚合该行的全部操作：
 *   ① AI 助手：AI 生成描述（Qwen3-VL 写回）/ 生成预览图（SDXL 异步任务）
 *      / 识别情绪（Qwen3-VL）
 *   ② 关键帧：生成 / 重生成 / 版本回退 / 删除（真实版本管理契约）
 *   ③ 音色绑定入口（打开音色抽屉）
 * AI 前置守卫：行未持久化（本地新建行）时先全量保存再调单行端点。
 * ========================================================================== */

import { useCallback, useEffect, useState } from 'react';
import { Eye, ImagePlus, Mic, RotateCcw, Sparkles, Trash2, Wand2, X } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import {
  aiDescribe,
  deleteKeyframe,
  detectEmotion,
  generateKeyframe,
  getMediaUrl,
  previewStoryboardImage,
  regenerateKeyframe,
  rollbackKeyframe,
} from '@/services/mangaApi';
import { ROW_GEN_STATUS_LABELS } from '@/constants/statusLabels';
import { getErrorMessage, reportActionError, reportBgError } from '@/utils/errors';
import { readPromptPrefix } from './batchOps';

function formatTime(ts?: number) {
  if (!ts) return '';
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

/* 错误消息提取/呈现统一走 @/utils/errors（TASK-P2-08） */

export default function InspectorPanel({ onBack, onOpenVoice }: { onBack?: () => void; onOpenVoice: () => void }) {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const rows = useMangaStore((s) => s.rows);
  const selectedRowId = useMangaStore((s) => s.selectedRowId);
  const setSelectedRow = useMangaStore((s) => s.setSelectedRow);
  const updateRow = useMangaStore((s) => s.updateRow);
  const saveRows = useMangaStore((s) => s.saveRows);
  const keyframes = useMangaStore((s) => (selectedRowId ? s.keyframes[selectedRowId] : undefined));
  const fetchKeyframes = useMangaStore((s) => s.fetchKeyframes);
  const invalidateKeyframes = useMangaStore((s) => s.invalidateKeyframes);

  const row = rows.find((r) => r.id === selectedRowId);

  const [busy, setBusy] = useState<'describe' | 'preview' | 'emotion' | ''>('');
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [busyId, setBusyId] = useState('');

  // 选中行切换：拉取关键帧 + 复位预览图
  useEffect(() => {
    setPreviewUrl(null);
    if (!selectedRowId) return;
    fetchKeyframes(selectedRowId).catch((err) =>
      reportBgError('InspectorPanel.fetchKeyframes', err),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRowId]);

  /** AI 前置：本地新建行先全量持久化（单行端点要求服务端已有该行） */
  const ensurePersisted = useCallback(async () => {
    if (selectedRowId?.startsWith('row_')) {
      await saveRows(useMangaStore.getState().rows);
    }
  }, [selectedRowId, saveRows]);

  const handleDescribe = useCallback(() => {
    if (!currentProject || !row) return;
    if (!row.original_dialogue.trim()) {
      showToast('该行没有台词，AI 描述需要原始台词作为输入', 'warning');
      return;
    }
    setBusy('describe');
    ensurePersisted()
      .then(() => aiDescribe(row.id, currentProject.id, readPromptPrefix()))
      .then((description) => {
        if (description) {
          // 描述词落库失败会让「已生成」toast 变成误导，必须 TOAST 级透出
          updateRow(row.id, { description, is_ai_generated: true }).catch((err) =>
            reportActionError(err, '描述词保存'),
          );
        }
        showToast('AI 描述已生成', 'success');
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, 'AI 描述生成失败'), 'error'))
      .finally(() => setBusy(''));
  }, [currentProject, row, ensurePersisted, updateRow, showToast]);

  const handlePreview = useCallback(() => {
    if (!currentProject || !row) return;
    if (!row.description.trim()) {
      showToast('请先生成或填写画面描述', 'warning');
      return;
    }
    setBusy('preview');
    ensurePersisted()
      .then(() => previewStoryboardImage(row.id, currentProject.id))
      .then((res) => {
        if (res.degraded) showToast(res.degrade_reason || '预览图为降级管线产出', 'warning');
        if (res.image) {
          setPreviewUrl(`data:image/png;base64,${res.image}`);
          showToast('预览图已生成', 'success');
        }
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '预览图生成失败'), 'error'))
      .finally(() => setBusy(''));
  }, [currentProject, row, ensurePersisted, showToast]);

  const handleEmotion = useCallback(() => {
    if (!currentProject || !row) return;
    if (!row.original_dialogue.trim()) {
      showToast('该行没有台词，无法识别情绪', 'warning');
      return;
    }
    setBusy('emotion');
    detectEmotion(row.original_dialogue)
      .then((res) => {
        if (res.degraded) showToast(res.degrade_reason || '情绪识别为降级规则产出', 'warning');
        if (res.emotion) {
          updateRow(row.id, { voice_emotion: res.emotion }).catch((err) =>
            reportActionError(err, '情绪标签保存'),
          );
          showToast(`情绪识别：${res.emotion}`, 'success');
        }
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '情绪识别失败'), 'error'))
      .finally(() => setBusy(''));
  }, [currentProject, row, updateRow, showToast]);

  const handleGenerateKeyframe = useCallback(
    (regenerate: boolean) => {
      if (!currentProject || !selectedRowId) return;
      setGenerating(true);
      ensurePersisted()
        .then(() =>
          regenerate
            ? regenerateKeyframe({ row_id: selectedRowId, project_id: currentProject.id })
            : generateKeyframe({ row_id: selectedRowId, project_id: currentProject.id }),
        )
        .then(() => {
          showToast(regenerate ? '已重新生成新版本' : '关键帧已生成', 'success');
          invalidateKeyframes(selectedRowId);
          return fetchKeyframes(selectedRowId);
        })
        .catch((err: unknown) => showToast(getErrorMessage(err, '关键帧生成失败'), 'error'))
        .finally(() => setGenerating(false));
    },
    [currentProject, selectedRowId, ensurePersisted, invalidateKeyframes, fetchKeyframes, showToast],
  );

  const handleKeyframeOp = useCallback(
    (keyframeId: string, version: number, op: 'rollback' | 'delete') => {
      if (!selectedRowId) return;
      setBusyId(keyframeId);
      const call = op === 'rollback' ? rollbackKeyframe(keyframeId) : deleteKeyframe(keyframeId);
      call
        .then(() => {
          showToast(op === 'rollback' ? `已回退到 v${version}` : `已删除 v${version}`, 'success');
          invalidateKeyframes(selectedRowId);
          return fetchKeyframes(selectedRowId);
        })
        .catch((err: unknown) => showToast(getErrorMessage(err, op === 'rollback' ? '回退失败' : '删除失败'), 'error'))
        .finally(() => setBusyId(''));
    },
    [selectedRowId, invalidateKeyframes, fetchKeyframes, showToast],
  );

  if (!currentProject || !row) return null;

  const versions = keyframes ?? [];
  const hasVersions = versions.length > 0;
  /** 已绑定资产数（多资产契约 asset_ids，asset_id 为兼容回退） */
  const boundAssetCount = (row.asset_ids ?? (row.asset_id ? [row.asset_id] : [])).length;

  return (
    <aside className="manga-ed-inspector">
      <div className="manga-ed-inspector-head">
        <h3 className="manga-ed-inspector-title">
          镜 {row.shot_number}
          <span className={`badge ${row.generation_status === 'done' ? 'success' : row.generation_status === 'error' ? 'error' : row.generation_status === 'generating' ? 'warning' : 'primary'}`} style={{ marginLeft: 6 }}>
            {ROW_GEN_STATUS_LABELS[row.generation_status] ?? row.generation_status}
          </span>
        </h3>
        <button type="button" className="btn-icon" style={{ width: 26, height: 26 }} title="关闭检查器" aria-label="关闭检查器" onClick={() => { setSelectedRow(null); onBack?.(); }}>
          <X size={14} />
        </button>
      </div>

      {/* AI 助手 */}
      <div className="card manga-insp-card">
        <div className="manga-insp-title">
          <Wand2 size={14} style={{ color: 'var(--color-primary)' }} />
          AI 助手
        </div>
        <div className="flex flex-col gap-2">
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy !== ''} onClick={handleDescribe}>
            <Wand2 size={13} />
            {busy === 'describe' ? '生成中…' : 'AI 生成描述'}
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy !== ''} onClick={handlePreview}>
            <Eye size={13} />
            {busy === 'preview' ? '生成中…' : '生成预览图'}
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy !== ''} onClick={handleEmotion}>
            <Sparkles size={13} />
            {busy === 'emotion' ? '识别中…' : '识别情绪'}
          </button>
        </div>
        {row.voice_emotion && (
          <div className="text-secondary" style={{ marginTop: 6, fontSize: 'var(--font-size-xs)' }}>
            当前情绪：{row.voice_emotion}
          </div>
        )}
        {previewUrl && (
          <img
            src={previewUrl}
            alt={`镜${row.shot_number}预览`}
            style={{ width: '100%', borderRadius: 'var(--radius-md)', marginTop: 8, display: 'block', border: '1px solid var(--color-border-light)' }}
          />
        )}
      </div>

      {/* 关键帧 */}
      <div className="card manga-insp-card">
        <div className="manga-insp-title">
          <ImagePlus size={14} style={{ color: 'var(--color-accent)' }} />
          关键帧
          <div className="flex gap-1" style={{ marginLeft: 'auto' }}>
            <button type="button" className="btn btn-primary btn-sm" disabled={generating} onClick={() => handleGenerateKeyframe(false)} title="按分镜描述生成关键帧（SDXL 文生图）">
              {generating ? '生成中…' : '生成'}
            </button>
            <button type="button" className="btn btn-secondary btn-sm" disabled={generating || !hasVersions} onClick={() => handleGenerateKeyframe(true)} title="重新生成：产出新版本，旧版保留可回退">
              重生成
            </button>
          </div>
        </div>
        {!hasVersions ? (
          <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
            暂无关键帧，点击「生成」按分镜描述产出首版
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {versions.map((k) => (
              <div
                key={k.keyframe_id}
                style={{
                  borderRadius: 'var(--radius-md)',
                  overflow: 'hidden',
                  border: k.is_current ? '1px solid var(--color-primary)' : '1px solid var(--color-border-light)',
                  boxShadow: k.is_current ? '0 0 0 1px var(--color-primary)' : undefined,
                }}
              >
                {k.file_path ? (
                  <img
                    src={getMediaUrl(k.file_path, `${k.version}-${k.created_at}`)}
                    alt={`v${k.version}`}
                    loading="lazy"
                    style={{ width: '100%', aspectRatio: '16/9', objectFit: 'cover', background: 'var(--color-input-bg)', display: 'block' }}
                  />
                ) : (
                  <div className="flex items-center justify-center" style={{ width: '100%', aspectRatio: '16/9', background: 'var(--color-input-bg)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
                    {k.status === 'error' ? '生成失败' : '无图像'}
                  </div>
                )}
                <div style={{ padding: 'var(--space-2)' }}>
                  <div className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-xs)' }}>
                    <span style={{ fontWeight: 600 }}>v{k.version}</span>
                    {k.is_current && <span className="badge primary">当前</span>}
                    <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--color-text-tertiary)' }}>{formatTime(k.created_at)}</span>
                  </div>
                  {k.error && (
                    <div className="ellipsis" title={k.error} style={{ marginTop: 2, fontSize: 10, color: 'var(--color-error)' }}>
                      {k.error}
                    </div>
                  )}
                  <div className="flex gap-1 mt-2">
                    {!k.is_current && (
                      <button type="button" className="btn btn-secondary btn-sm flex-1" disabled={busyId === k.keyframe_id} onClick={() => handleKeyframeOp(k.keyframe_id, k.version, 'rollback')} title="回退：将此版本置为当前">
                        <RotateCcw size={11} />
                        回退
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm flex-1"
                      style={{ color: 'var(--color-error)' }}
                      disabled={busyId === k.keyframe_id}
                      onClick={() => handleKeyframeOp(k.keyframe_id, k.version, 'delete')}
                      title={k.is_current ? '删除当前版本（自动回退上一版）' : '删除此版本'}
                    >
                      <Trash2 size={11} />
                      删除
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 绑定与音色（多资产契约：asset_ids 列表，分镜表资产列管理） */}
      <div className="card manga-insp-card">
        <div className="manga-insp-title">
          <Mic size={14} style={{ color: 'var(--color-highlight)' }} />
          绑定与音色
        </div>
        <div className="text-secondary" style={{ fontSize: 'var(--font-size-xs)', marginBottom: 6 }}>
          绑定资产：{boundAssetCount > 0
            ? `已绑定 ${boundAssetCount} 个（分镜表资产列点击缩略图/+ 管理）`
            : '未绑定（分镜表资产列点击 + 绑定）'}
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenVoice}>
          <Mic size={13} />
          打开音色绑定
        </button>
      </div>
    </aside>
  );
}
