// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * StylePage.tsx —— 视频风格页（/style，对接 /style/* 真实端点）
 * --------------------------------------------------------------------------
 * 区块：
 *   1. 素材上传（POST /style/upload，视频后端 FFmpeg 抽帧 → dataset_id）
 *   2. 数据集列表（GET /style/datasets，可选中作为训练集）
 *   3. 风格描述 + LoRA 参数（rank / alpha / learning_rate / epochs）
 *   4. 开始训练（POST /style/train）与进度卡片（2s 轮询 + VRAM/GPU 实时遥测）
 *   5. 风格预览（POST /style/preview → base64 帧；未就绪如实 80013）
 *   6. LoRA 版本列表（GET /style/versions）+ 回滚（POST /style/rollback）
 * 诚实门控：GET /style/status 基座未就绪时顶部横幅说明，训练/预览由后端
 * 如实返回 80010/80013，前端透出不伪造。
 * ========================================================================== */

import React, { useEffect, useRef, useState } from 'react';
import {
  Wand2,
  Film,
  FolderOpen,
  PenLine,
  Clapperboard,
  Upload,
  Loader2,
  RefreshCw,
  Rocket,
  Play,
  Pause,
  X,
  AlertTriangle,
  LineChart,
  Layers,
  Copy,
  Download,
  Combine,
  Bookmark,
} from 'lucide-react';
import { useStyleStore } from '@/stores/useStyleStore';
import { useAppStore } from '@/stores/useAppStore';
import { useHardwareStore } from '@/stores/useHardwareStore';
import * as styleApi from '@/services/styleApi';
import { isApiError } from '@/services/api';
import { TRAIN_STATUS_LABELS } from '@/constants/statusLabels';
import { formatPercent, formatRelativeTime, formatVRAM } from '@utils/format';
import Slider from '@/components/common/Slider';

/** 支持的素材类型 */
const ACCEPT = 'video/mp4,video/webm,video/quicktime,image/png,image/jpeg,image/webp';

/** 学习率候选项（后端钳制边界 1e-7 ~ 1e-3，文档 §8.3.7 默认 2e-5） */
const LR_OPTIONS = [1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4];

/** 内置默认超参（与 useStyleStore 初始值一致；STYLE-011 恢复默认回写此组） */
const PARAM_DEFAULTS = { rank: 16, alpha: 32, learningRate: 2e-5, epochs: 10 } as const;

/** 训练参数预设模板（STYLE-009；custom 表示用户手工偏离预设） */
const PARAM_PRESETS = [
  { key: 'fast', label: '快速试验（Rank 8 / 5 轮 / 高学习率）', rank: 8, alpha: 16, learningRate: 5e-4, epochs: 5 },
  { key: 'balanced', label: '均衡（默认）', ...PARAM_DEFAULTS },
  { key: 'quality', label: '高质量（Rank 32 / 20 轮）', rank: 32, alpha: 64, learningRate: 1e-4, epochs: 20 },
  { key: 'extreme', label: '极致拟合（Rank 64 / 30 轮）', rank: 64, alpha: 128, learningRate: 5e-5, epochs: 30 },
] as const;

/**
 * QLoRA 4bit 训练显存粗略预估（STYLE-010）。
 * 依据：2B 基座 4bit 权重 ≈ 1.5GB；优化器/梯度 ≈ rank 线性；激活随 batch=1 固定。
 * 明确标注为预估值，实际占用以训练进度卡片实时遥测为准（不伪造精确值）。
 */
function estimateVramGb(rank: number, alpha: number, epochs: number): number {
  return 1.5 + rank * 0.11 + alpha * 0.02 + Math.min(epochs, 30) * 0.02 + 0.8;
}

export const StylePage: React.FC = () => {
  const {
    datasetId, datasetName, uploading,
    datasets, datasetsLoaded,
    stylePrompt, rank, alpha, learningRate, epochs,
    task, training,
    versions, versionsLoaded,
    status,
    setStylePrompt, setRank, setAlpha, setLearningRate, setEpochs,
    uploadMaterial, fetchDatasets, selectDataset, fetchStatus,
    startTraining, fetchVersions, rollback, controlTask,
  } = useStyleStore();
  const activeFeature = useAppStore((s) => s.activeFeature);
  const showToast = useAppStore((s) => s.showToast);
  /** VRAM/GPU 实时遥测（WS hardware/realtime；训练进度卡片展示用） */
  const realtime = useHardwareStore((s) => s.realtime);

  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  /** 风格预览状态（frames 为 base64 JPEG；previewError 为后端如实错误文案） */
  const [previewVersion, setPreviewVersion] = useState('');
  const [previewFrames, setPreviewFrames] = useState<string[]>([]);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewError, setPreviewError] = useState('');
  /* ── 版本高级操作（2026-09-17 接线）：克隆/导出/融合/模板 ── */
  const [rowBusy, setRowBusy] = useState('');
  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeA, setMergeA] = useState('');
  const [mergeB, setMergeB] = useState('');
  const [mergeWa, setMergeWa] = useState(0.6);
  const [mergeBusy, setMergeBusy] = useState(false);
  const [templates, setTemplates] = useState<styleApi.StyleTemplate[]>([]);

  useEffect(() => {
    styleApi.listStyleTemplates().then(setTemplates).catch(() => setTemplates([]));
  }, []);

  /** 行级：克隆（复制数据集+配置为新训练任务） */
  const handleClone = async (version: string) => {
    setRowBusy(`clone:${version}`);
    try {
      const res = await styleApi.cloneStyleVersion(version);
      showToast(`已克隆为新训练任务（${res.new_task_id.slice(0, 8)}…），到训练区查看`, 'success');
    } catch (err) {
      showToast(isApiError(err) ? err.message : '克隆失败', 'error');
    } finally {
      setRowBusy('');
    }
  };

  /** 行级：导出（tar.gz + SHA256 侧车） */
  const handleExport = async (version: string) => {
    setRowBusy(`export:${version}`);
    try {
      const res = await styleApi.exportStyleVersion(version);
      showToast(`已导出：${String(res.export_path ?? '见 data/generated/exports')}`, 'success');
    } catch (err) {
      showToast(isApiError(err) ? err.message : '导出失败', 'error');
    } finally {
      setRowBusy('');
    }
  };

  /** 两两融合（后端支持 N 版本，UI 收敛两版常用态） */
  const handleMerge = async () => {
    if (!mergeA || !mergeB || mergeA === mergeB) {
      showToast('请选择两个不同版本', 'warning');
      return;
    }
    setMergeBusy(true);
    try {
      await styleApi.mergeStyleVersions(
        [mergeA, mergeB], [mergeWa, Number((1 - mergeWa).toFixed(2))]);
      showToast('融合完成，新版本已生成', 'success');
      setMergeOpen(false);
      fetchVersions();
    } catch (err) {
      showToast(isApiError(err) ? err.message : '融合失败', 'error');
    } finally {
      setMergeBusy(false);
    }
  };

  /** 模板：保存当前表单配置 */
  const handleSaveTemplate = async () => {
    const name = window.prompt('模板名称：', '');
    if (!name) return;
    try {
      await styleApi.saveStyleTemplate({
        name,
        style_prompt: stylePrompt,
        lora_rank: rank,
        lora_alpha: alpha,
        learning_rate: learningRate,
        epochs,
      });
      showToast(`模板「${name}」已保存`, 'success');
      setTemplates(await styleApi.listStyleTemplates());
    } catch (err) {
      showToast(isApiError(err) ? err.message : '保存模板失败', 'error');
    }
  };

  /** 模板：应用到表单 */
  const handleApplyTemplate = (tpl: styleApi.StyleTemplate) => {
    if (tpl.style_prompt != null) setStylePrompt(String(tpl.style_prompt));
    if (tpl.lora_rank != null) setRank(Number(tpl.lora_rank));
    if (tpl.lora_alpha != null) setAlpha(Number(tpl.lora_alpha));
    if (tpl.learning_rate != null) setLearningRate(Number(tpl.learning_rate));
    if (tpl.epochs != null) setEpochs(Number(tpl.epochs));
    showToast(`已应用模板「${tpl.name}」到训练表单`, 'success');
  };

  useEffect(() => {
    fetchVersions();
    fetchDatasets();
    fetchStatus();
  }, [fetchVersions, fetchDatasets, fetchStatus]);

  /** 当前生效版本（预览下拉默认选中项） */
  const currentVersion = versions.find((v) => v.is_current) ?? versions[0];

  /** 版本列表就绪后，预览下拉默认对齐当前生效版本（用户可另行选择对比） */
  useEffect(() => {
    if (!previewVersion && currentVersion) {
      setPreviewVersion(currentVersion.version);
    }
  }, [currentVersion, previewVersion]);

  /** 训练中（本页任务或全局 training 锁）→ 提示其他 AI 功能置灰 */
  const globalTraining = activeFeature === 'training';
  const loss =
    task?.hyperparams && typeof (task.hyperparams as Record<string, unknown>).loss === 'number'
      ? ((task.hyperparams as Record<string, unknown>).loss as number)
      : null;
  /** 训练充分性下限（服务状态未加载时回退 4，对齐后端 MIN_STYLE_SAMPLES） */
  const minSamples = status?.min_samples ?? 4;

  const handleFile = (file: File | undefined) => {
    if (!file) return;
    uploadMaterial(file);
  };

  /** 生成风格预览（POST /style/preview；80013/80012 如实透出） */
  const handlePreview = async () => {
    setPreviewBusy(true);
    setPreviewError('');
    setPreviewFrames([]);
    try {
      const res = await styleApi.previewStyle(previewVersion || undefined, 8);
      setPreviewFrames(res.frames);
      if (res.frames.length === 0) {
        setPreviewError('预览接口返回了空帧序列');
      }
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '风格预览失败';
      setPreviewError(msg);
    } finally {
      setPreviewBusy(false);
    }
  };

  return (
    <div className="page">
      <h1 className="page-title"><Wand2 size={20} aria-hidden="true" /> 视频风格</h1>
      <p className="page-subtitle">上传素材，训练专属视频风格 LoRA（基座 LTX-2，QLoRA 4bit）</p>

      {/* 训练中横幅：其他 AI 功能已置灰 */}
      {(training || globalTraining) && (
        <div
          className="card mt-5"
          role="alert"
          style={{
            borderLeft: '3px solid var(--color-warning)',
            background: 'var(--color-warning-bg)',
          }}
        >
          <span className="inline-flex items-start gap-2">
            <AlertTriangle size={16} aria-hidden="true" className="shrink-0 mt-0.5" />
            风格 LoRA 训练中，其他 AI 功能（对话 / 绘画 / 视频生成）已置灰，训练完成后自动恢复。
          </span>
        </div>
      )}

      {/* 基座未就绪诚实横幅（GET /style/status：LTX-2 权重未随包分发时如实说明，不伪造可用态） */}
      {status && !status.base_ready && (
        <div
          className="card mt-5"
          role="alert"
          style={{
            borderLeft: '3px solid var(--color-error)',
            background: 'var(--color-error-bg)',
          }}
        >
          <span className="inline-flex items-start gap-2">
            <AlertTriangle size={16} aria-hidden="true" className="shrink-0 mt-0.5" />
            风格训练基座（{status.base_model}）未就绪：{status.base_reason || '权重文件缺失'}。
            素材上传与数据集管理可用；训练 / 预览按钮已置灰（B2），权重就位后自动恢复。
          </span>
        </div>
      )}

      <div className="flex flex-col gap-4 mt-5">
        {/* 1. 素材上传 */}
        <section className="card hoverable" aria-label="训练素材">
          <h3 className="card-title"><Film size={16} aria-hidden="true" /> 训练素材</h3>
          <div
            role="button"
            tabIndex={0}
            aria-label="上传视频或图片素材"
            className="flex-center flex-col gap-2"
            style={{
              padding: 'var(--space-8)',
              border: `2px dashed ${dragOver ? 'var(--color-primary)' : 'var(--color-border)'}`,
              borderRadius: 'var(--radius-lg)',
              background: dragOver ? 'var(--color-primary-50)' : 'var(--color-surface-secondary)',
              cursor: uploading || training ? 'not-allowed' : 'pointer',
              opacity: training ? 0.6 : 1,
              transition: 'all var(--duration-hover) var(--ease-out)',
            }}
            onClick={() => !uploading && !training && inputRef.current?.click()}
            onKeyDown={(e) => { if (e.key === 'Enter') inputRef.current?.click(); }}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              if (!training) handleFile(e.dataTransfer.files?.[0]);
            }}
          >
            <span style={{ fontSize: 32, color: 'var(--color-primary)', lineHeight: 1, display: 'flex' }} aria-hidden="true">
              {uploading ? <Loader2 size={32} className="animate-spin" /> : <Upload size={32} />}
            </span>
            <div className="text-sm">
              {uploading
                ? '正在上传素材…'
                : datasetName
                  ? `已上传：${datasetName}（可重新上传覆盖）`
                  : '拖拽或点击上传视频 / 图片素材'}
            </div>
            <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
              支持 MP4 / WebM / MOV / PNG / JPG / WebP，视频将自动提取关键帧
            </div>
          </div>
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPT}
            hidden
            onChange={(e) => {
              handleFile(e.target.files?.[0]);
              e.target.value = '';
            }}
          />
        </section>

        {/* 2. 数据集列表（GET /style/datasets，点击选中作为训练集；P1-11） */}
        <section className="card hoverable" aria-label="数据集列表">
          <div className="flex items-center justify-between mb-3">
            <h3 className="card-title" style={{ marginBottom: 0 }}><FolderOpen size={16} aria-hidden="true" /> 数据集列表</h3>
            <button className="btn btn-ghost btn-sm" onClick={fetchDatasets}><RefreshCw size={14} aria-hidden="true" /> 刷新</button>
          </div>
          {!datasetsLoaded ? (
            <div className="loading-block"><div className="spinner" /></div>
          ) : datasets.length === 0 ? (
            <div className="text-secondary text-sm">
              暂无数据集。上传视频 / 图片素材后，后端将自动抽帧构建数据集并出现在此处。
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {datasets.map((d) => {
                const selected = d.dataset_id === datasetId;
                const sufficient = d.frame_count >= minSamples;
                return (
                  <div
                    key={d.dataset_id}
                    role="button"
                    tabIndex={0}
                    aria-pressed={selected}
                    className="flex items-center gap-3"
                    style={{
                      padding: 'var(--space-3) var(--space-4)',
                      border: `1px solid ${selected ? 'var(--color-primary)' : 'var(--color-border-light)'}`,
                      borderRadius: 'var(--radius-lg)',
                      background: selected ? 'var(--color-primary-50)' : 'transparent',
                      cursor: training ? 'not-allowed' : 'pointer',
                      opacity: training ? 0.6 : 1,
                    }}
                    onClick={() => !training && selectDataset(d.dataset_id, d.dataset_id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !training) selectDataset(d.dataset_id, d.dataset_id);
                    }}
                  >
                    <div className="flex-1" style={{ minWidth: 0 }}>
                      <div className="flex items-center gap-2">
                        <strong className="text-sm mono">{d.dataset_id}</strong>
                        {selected && <span className="badge info">训练集</span>}
                        {sufficient ? (
                          <span className="badge success">样本充足</span>
                        ) : (
                          <span className="badge warning">样本不足（{d.frame_count}/{minSamples}）</span>
                        )}
                      </div>
                      <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
                        {d.frame_count} 帧样本
                        {d.updated_at ? ` · 更新于 ${formatRelativeTime(d.updated_at * 1000)}` : ''}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          <div className="text-tertiary mt-3" style={{ fontSize: 'var(--font-size-xs)' }}>
            训练充分性下限：{minSamples} 帧样本（不足时后端将拒绝训练并如实返回 80011）。
          </div>
        </section>

        {/* 3. 风格描述 + LoRA 参数 */}
        <section className="card hoverable" aria-label="风格与参数">
          <h3 className="card-title"><PenLine size={16} aria-hidden="true" /> 风格描述与 LoRA 参数</h3>
          <div className="form-row">
            <label className="form-label">风格描述</label>
            <textarea
              className="input"
              rows={3}
              value={stylePrompt}
              disabled={training}
              placeholder="如：樱花水彩风，柔和粉色调，笔触松散，高亮度…"
              onChange={(e) => setStylePrompt(e.target.value)}
            />
          </div>
          {/* STYLE-009 预设模板 + STYLE-011 恢复默认 */}
          <div className="form-row" style={{ display: 'flex', gap: 'var(--space-3)', alignItems: 'flex-end' }}>
            <div style={{ flex: 1 }}>
              <label className="form-label" htmlFor="style-preset">参数预设模板</label>
              <select
                id="style-preset"
                className="input"
                disabled={training}
                value=""
                onChange={(e) => {
                  const p = PARAM_PRESETS.find((x) => x.key === e.target.value);
                  if (!p) return;
                  setRank(p.rank);
                  setAlpha(p.alpha);
                  setLearningRate(p.learningRate);
                  setEpochs(p.epochs);
                }}
              >
                <option value="" disabled>
                  选择预设以一键填充参数…
                </option>
                {PARAM_PRESETS.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={training}
              onClick={() => {
                setRank(PARAM_DEFAULTS.rank);
                setAlpha(PARAM_DEFAULTS.alpha);
                setLearningRate(PARAM_DEFAULTS.learningRate);
                setEpochs(PARAM_DEFAULTS.epochs);
              }}
            >
              恢复默认
            </button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            <Slider label="LoRA Rank" min={1} max={64} value={rank} disabled={training} onChange={setRank} />
            <Slider label="LoRA Alpha" min={1} max={128} value={alpha} disabled={training} onChange={setAlpha} />
            <Slider label="训练轮次 Epochs" min={1} max={50} value={epochs} disabled={training} onChange={setEpochs} />
            {/* 学习率：常用数量级候选（后端钳制边界 1e-7 ~ 1e-3，文档 §8.3.7 默认 2e-5） */}
            <div className="form-row" style={{ marginBottom: 0 }}>
              <label className="form-label" htmlFor="style-lr">学习率 Learning Rate</label>
              <select
                id="style-lr"
                className="input"
                value={learningRate}
                disabled={training}
                onChange={(e) => setLearningRate(parseFloat(e.target.value))}
              >
                {LR_OPTIONS.map((lr) => (
                  <option key={lr} value={lr}>
                    {lr.toExponential(0).replace('e', '×10^')}
                    {lr === 2e-5 ? '（推荐）' : ''}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="text-tertiary mt-2" style={{ fontSize: 'var(--font-size-xs)' }}>
            Batch Size 由后端按显存安全线固定为 1（梯度累积 4），不提供手工调节；越界超参由服务层钳制到硬件安全边界。
          </div>
          {/* STYLE-010：显存预估（粗略公式，明确标注预估性质） */}
          <div
            className="mt-2"
            style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-secondary)' }}
            data-testid="vram-estimate"
          >
            预估训练峰值显存 ≈ <strong>{estimateVramGb(rank, alpha, epochs).toFixed(1)} GB</strong>
            （QLoRA 4bit 粗略预估，随参数联动；实际占用以训练进度实时遥测为准）
          </div>
          <button
            className="btn btn-primary btn-block mt-3"
            disabled={training || uploading || !datasetId
              || (status != null && !status.base_ready)}
            onClick={startTraining}
          >
            {training ? (
              <span className="inline-flex items-center gap-2"><Loader2 size={16} className="animate-spin" aria-hidden="true" /> 训练中…</span>
            ) : (
              <span className="inline-flex items-center gap-2"><Rocket size={16} aria-hidden="true" /> 开始训练</span>
            )}
          </button>
          {!datasetId && !training && (
            <div className="text-tertiary mt-2 text-sm">请先在上方上传素材或选择数据集。</div>
          )}
        </section>

        {/* 4. 训练进度卡片 */}
        {task && (
          <section className="card hoverable" aria-label="训练进度">
            <div className="flex items-center justify-between mb-3">
              <h3 className="card-title" style={{ marginBottom: 0 }}><LineChart size={16} aria-hidden="true" /> 训练进度</h3>
            <div className="flex items-center gap-2">
              <span className={`badge ${task.status === 'done' ? 'success' : task.status === 'error' ? 'error' : 'info'}`}>
                {TRAIN_STATUS_LABELS[task.status] ?? task.status}
              </span>
              {/* 训练控制（STYLE-017 后端已在：暂停=epoch 检查点挂起/恢复/取消） */}
              {(task.status === 'running' || task.status === 'pending' || task.status === 'paused') && (
                <>
                  {task.status === 'paused' ? (
                    <button
                      className="btn btn-secondary btn-sm"
                      onClick={() => void controlTask('resume')}
                      title="从 epoch 检查点恢复训练"
                    >
                      <Play size={14} aria-hidden="true" /> 恢复
                    </button>
                  ) : (
                    <button
                      className="btn btn-secondary btn-sm"
                      onClick={() => void controlTask('pause')}
                      title="挂起到 epoch 检查点（不丢进度）"
                    >
                      <Pause size={14} aria-hidden="true" /> 暂停
                    </button>
                  )}
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => void controlTask('cancel')}
                    title="取消训练（下个 epoch 检查点中断）"
                  >
                    <X size={14} aria-hidden="true" /> 取消
                  </button>
                </>
              )}
            </div>
            </div>
            <div className="progress">
              <div
                className="progress-bar"
                style={{ width: `${Math.round((task.progress ?? 0) * 100)}%` }}
              />
            </div>
            <div className="flex flex-wrap gap-4 mt-3 text-secondary" style={{ fontSize: 'var(--font-size-sm)' }}>
              <span>进度：{formatPercent(task.progress ?? 0, true)}</span>
              {loss !== null && <span>Loss：{loss.toFixed(4)}</span>}
              {task.created_at && <span>创建：{formatRelativeTime(task.created_at)}</span>}
            </div>
            {/* VRAM/GPU 实时遥测（WS hardware/realtime；无数据时如实显示 --） */}
            <div className="flex flex-wrap gap-4 mt-2 text-secondary" style={{ fontSize: 'var(--font-size-sm)' }}>
              <span>
                显存：
                {realtime?.vram_used_mb != null
                  ? `${formatVRAM(realtime.vram_used_mb)} / ${formatVRAM(realtime.vram_total_mb)}`
                  : '--'}
                {realtime?.vram_percent != null && `（${formatPercent(realtime.vram_percent)}）`}
              </span>
              <span>
                GPU 利用率：
                {realtime?.gpu_util_pct != null ? formatPercent(realtime.gpu_util_pct) : '--'}
              </span>
              {realtime?.gpu_temp_celsius != null && <span>GPU 温度：{Math.round(realtime.gpu_temp_celsius)}℃</span>}
            </div>
            {task.status === 'error' && task.error && (
              <div className="mt-3 text-sm" style={{ color: 'var(--color-error)' }}>
                错误：{task.error}
              </div>
            )}
          </section>
        )}

        {/* 5. 风格预览（POST /style/preview → base64 帧；未就绪如实透出 80013） */}
        <section className="card hoverable" aria-label="风格预览">
          <div className="flex items-center justify-between mb-3">
            <h3 className="card-title" style={{ marginBottom: 0 }}><Clapperboard size={16} aria-hidden="true" /> 风格预览</h3>
            <div className="flex items-center gap-2">
              {/* 预览版本：默认当前生效版本，可切换已训练版本对比 */}
              <select
                className="input"
                style={{ width: 'auto', padding: 'var(--space-1) var(--space-2)' }}
                value={previewVersion}
                disabled={previewBusy || versions.length === 0}
                onChange={(e) => setPreviewVersion(e.target.value)}
                aria-label="预览版本"
              >
                {versions.length === 0 ? (
                  <option value="">暂无已训练版本</option>
                ) : (
                  versions.map((v) => (
                    <option key={v.version} value={v.version}>
                      {v.name}
                      {v.is_current ? '（当前）' : ''}
                    </option>
                  ))
                )}
              </select>
              <button
                className="btn btn-secondary btn-sm"
                disabled={previewBusy || versions.length === 0
                  || (status != null && !status.base_ready)}
                onClick={handlePreview}
              >
                {previewBusy ? (
                  <span className="inline-flex items-center gap-2"><Loader2 size={14} className="animate-spin" aria-hidden="true" /> 生成中…</span>
                ) : (
                  <span className="inline-flex items-center gap-2"><Play size={14} aria-hidden="true" /> 生成预览</span>
                )}
              </button>
            </div>
          </div>
          {previewError ? (
            <div className="text-sm" role="alert" style={{ color: 'var(--color-error)' }}>
              {previewError}
            </div>
          ) : previewFrames.length > 0 ? (
            <div
              className="grid gap-2"
              style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))' }}
            >
              {previewFrames.map((frame, i) => (
                <img
                  key={i}
                  src={frame.startsWith('data:') ? frame : `data:image/jpeg;base64,${frame}`}
                  alt={`风格预览帧 ${i + 1}`}
                  style={{
                    width: '100%',
                    aspectRatio: '16 / 9',
                    objectFit: 'cover',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--color-border-light)',
                  }}
                />
              ))}
            </div>
          ) : (
            <div className="text-secondary text-sm">
              {previewBusy
                ? '正在按预览版本生成风格化帧序列…'
                : '选择版本并点击「生成预览」，将展示该风格 LoRA 的 base64 预览帧序列。'}
            </div>
          )}
        </section>

        {/* 6. LoRA 版本列表 */}
        <section className="card hoverable" aria-label="LoRA 版本">
          <div className="flex items-center justify-between mb-3">
            <h3 className="card-title" style={{ marginBottom: 0 }}><Layers size={16} aria-hidden="true" /> LoRA 版本</h3>
          <div className="flex gap-2">
            {versions.length >= 2 && (
              <button className="btn btn-outline btn-sm" onClick={() => setMergeOpen((v) => !v)}>
                <Combine size={14} aria-hidden="true" /> 融合
              </button>
            )}
            <button className="btn btn-ghost btn-sm" onClick={fetchVersions}><RefreshCw size={14} aria-hidden="true" /> 刷新</button>
          </div>
          </div>

          {/* 融合面板（两版线性加权，后端支持 N 版） */}
          {mergeOpen && versions.length >= 2 && (
            <div className="flex gap-2 items-center flex-wrap mb-3" style={{ padding: 'var(--space-2)', border: '1px solid var(--color-border-light)', borderRadius: 'var(--radius-md)' }}>
              <select className="form-select" style={{ width: 100 }} value={mergeA} onChange={(e) => setMergeA(e.target.value)} aria-label="融合版本 A">
                {versions.map((v) => <option key={v.version} value={v.version}>{v.version}</option>)}
              </select>
              <span className="text-secondary text-sm">×</span>
              <input type="number" min={0.1} max={0.9} step={0.1} value={mergeWa}
                     style={{ width: 64 }} className="form-input"
                     onChange={(e) => setMergeWa(Number(e.target.value) || 0.5)}
                     aria-label="A 权重" />
              <span className="text-secondary text-sm">+</span>
              <select className="form-select" style={{ width: 100 }} value={mergeB} onChange={(e) => setMergeB(e.target.value)} aria-label="融合版本 B">
                {versions.map((v) => <option key={v.version} value={v.version}>{v.version}</option>)}
              </select>
              <span className="text-secondary text-sm">×</span>
              <span className="text-secondary text-sm">{(1 - mergeWa).toFixed(2)}</span>
              <button className="btn btn-primary btn-sm" disabled={mergeBusy} onClick={() => void handleMerge()}>
                {mergeBusy ? '融合中…' : '执行融合'}
              </button>
            </div>
          )}
          {!versionsLoaded ? (
            <div className="loading-block"><div className="spinner" /></div>
          ) : versions.length === 0 ? (
            <div className="text-secondary text-sm">
              暂无风格 LoRA 版本。完成一次训练后，新版本将出现在此处。
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {versions.map((v, i) => {
                const isCurrent = v.is_current ?? i === 0;
                return (
                  <div
                    key={v.version}
                    className="flex items-center gap-3"
                    style={{
                      padding: 'var(--space-3) var(--space-4)',
                      border: '1px solid var(--color-border-light)',
                      borderRadius: 'var(--radius-lg)',
                    }}
                  >
                    <div className="flex-1" style={{ minWidth: 0 }}>
                      <div className="flex items-center gap-2">
                        <strong className="text-sm">{v.name}</strong>
                        {isCurrent && <span className="badge success">当前版本</span>}
                        {!v.has_adapter && <span className="badge warning">权重不完整</span>}
                      </div>
                      <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
                        {v.style_prompt || '风格 LoRA'}
                        {v.data_count !== undefined && ` · ${v.data_count} 样本`}
                        {v.created_at ? ` · ${formatRelativeTime(v.created_at * 1000)}` : ''}
                      </div>
                    </div>
                    {/* 质量分（后端未评估下发时显示 --） */}
                    <span
                      className="mono text-sm"
                      title="质量评分"
                      style={{
                        color: v.quality_score != null
                          ? (v.quality_score >= 80 ? 'var(--color-success)' : 'var(--color-warning)')
                          : 'var(--color-text-tertiary)',
                      }}
                    >
                      {v.quality_score != null ? `${Math.round(v.quality_score)}分` : '-- 分'}
                    </span>
                    <button
                      className="btn btn-secondary btn-sm"
                      disabled={isCurrent}
                      onClick={() => rollback(v.version)}
                    >
                      回滚
                    </button>
                    <button
                      className="btn btn-outline btn-sm"
                      disabled={rowBusy === `clone:${v.version}`}
                      title="复制此版本的数据集与配置为新训练任务"
                      onClick={() => void handleClone(v.version)}
                    >
                      <Copy size={13} aria-hidden="true" /> {rowBusy === `clone:${v.version}` ? '…' : '克隆'}
                    </button>
                    <button
                      className="btn btn-outline btn-sm"
                      disabled={rowBusy === `export:${v.version}`}
                      title="导出 tar.gz（含 adapter + meta + SHA256 校验）"
                      onClick={() => void handleExport(v.version)}
                    >
                      <Download size={13} aria-hidden="true" /> {rowBusy === `export:${v.version}` ? '…' : '导出'}
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        {/* 7. 风格模板（STYLE-032，2026-09-17 接线） */}
        <section className="card hoverable" aria-label="风格模板">
          <div className="flex items-center justify-between mb-3">
            <h3 className="card-title" style={{ marginBottom: 0 }}><Bookmark size={16} aria-hidden="true" /> 风格模板</h3>
            <button className="btn btn-outline btn-sm" onClick={() => void handleSaveTemplate()}>
              把当前配置存为模板
            </button>
          </div>
          {templates.length === 0 ? (
            <div className="text-secondary text-sm">
              暂无模板——调好训练参数后点「把当前配置存为模板」，下次一键复用。
            </div>
          ) : (
            <div className="flex flex-wrap gap-2">
              {templates.map((t) => (
                <button
                  key={String(t.name)}
                  className="btn btn-outline btn-sm"
                  title={String(t.style_prompt ?? '')}
                  onClick={() => handleApplyTemplate(t)}
                >
                  {String(t.name)}
                </button>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
};

export default StylePage;
