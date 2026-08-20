/* ==========================================================================
 * BatchConfirmModal.tsx —— 漫剧编辑器·批量工序确认弹窗（G2 竞品对齐重构）
 * --------------------------------------------------------------------------
 * 支持工序：describe(分镜生词) / keyframe(分镜生图) /
 *   story_narrative(故事生词) / story_keyframe(故事生图) / video_narrative(视频生词)
 * G2 新增：
 *   - 范围选择：缺失生成（默认安全）/ 全部生成
 *   - 模型选择：按任务类型展示本地模型列表（状态/VRAM/速度标签）
 *   - 分辨率选择（生图任务专属）
 *   - 预计耗时（替代积分，基于行数 × 模型速度标签本地估算）
 *   - 执行时透传 model_override / resolution 到后端
 * ========================================================================== */

import { useEffect, useMemo, useState } from 'react';
import { ImagePlus, Settings2, Wand2, Clock, Loader2, Clapperboard, FileText } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import { Modal } from '../../common/Modal';
import PromptModal from './PromptModal';
import {
  batchDescribe,
  batchKeyframes,
  batchStoryNarrative,
  batchStoryKeyframe,
  batchVideoNarrative,
  readPromptCfg,
} from './batchOps';
import type { BatchResult } from './batchOps';
import { listAvailableModels } from '@/services/mangaApi';
import { MODEL_AVAILABLE_STATUS_LABELS } from '@/constants/statusLabels';
import type { AvailableModel, ModelConfig } from '@/types';

/** 执行阶段 */
type Phase = 'confirm' | 'running' | 'done';

/** 工序类型 */
export type BatchKind = 'describe' | 'keyframe' | 'story_narrative' | 'story_keyframe' | 'video_narrative';

export interface BatchConfirmModalProps {
  /** 工序种类 */
  kind: BatchKind;
  onClose: () => void;
}

/** 工序元信息 */
const KIND_META: Record<BatchKind, { title: string; icon: typeof Wand2; taskType: 'dialog' | 'paint' | 'video' }> = {
  describe:         { title: '批量生成分镜描述词', icon: Wand2,       taskType: 'dialog' },
  keyframe:         { title: '批量生成分镜图',     icon: ImagePlus,   taskType: 'paint'  },
  story_narrative:  { title: '故事生词',           icon: FileText,    taskType: 'dialog' },
  story_keyframe:   { title: '故事生图',           icon: ImagePlus,   taskType: 'paint'  },
  video_narrative:  { title: '视频生词',           icon: Clapperboard, taskType: 'dialog' },
};

/** 速度标签 → 基准秒/行 */
const SPEED_BASE_SEC: Record<string, number> = {
  '极速': 8, '快速': 15, '标准': 30, '慢速': 60,
};

export default function BatchConfirmModal({ kind, onClose }: BatchConfirmModalProps) {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const rows = useMangaStore((s) => s.rows);
  const keyframesMap = useMangaStore((s) => s.keyframes);

  const [phase, setPhase] = useState<Phase>('confirm');
  const [processed, setProcessed] = useState(0);
  const [result, setResult] = useState<BatchResult | null>(null);
  const [promptOpen, setPromptOpen] = useState(false);
  const [promptCfg, setPromptCfg] = useState(readPromptCfg);

  // G2 新增状态
  const [scope, setScope] = useState<'all' | 'missing'>('missing');
  const [modelId, setModelId] = useState('');
  const [resolution, setResolution] = useState('2560x1440');
  const [modelsLoading, setModelsLoading] = useState(false);
  const [availableModels, setAvailableModels] = useState<AvailableModel[]>([]);

  const meta = KIND_META[kind];
  const isDescribe = kind === 'describe' || kind === 'story_narrative' || kind === 'video_narrative';
  const isKeyframe = kind === 'keyframe' || kind === 'story_keyframe';

  /** 缺失目标行（原有逻辑） */
  const missingTargets = useMemo(() => {
    if (kind === 'describe') return rows.filter((r) => !r.description.trim());
    if (kind === 'keyframe') return rows.filter((r) => !(keyframesMap[r.id] ?? []).some((k) => k.is_current));
    if (kind === 'story_narrative') return rows.filter((r) => !r.description.trim());
    if (kind === 'story_keyframe') return rows.filter((r) => !(keyframesMap[r.id] ?? []).some((k) => k.is_current));
    if (kind === 'video_narrative') return rows.filter((r) => r.description.trim());
    return rows;
  }, [kind, rows, keyframesMap]);

  /** 根据 scope 过滤目标行 */
  const targets = useMemo(() => {
    if (scope === 'all') {
      // 全部生成 = 所有有输入的行
      return rows.filter((r) => {
        if (r.is_locked) return false;
        if (isDescribe) return r.original_dialogue.trim().length > 0;
        if (isKeyframe) return r.description.trim().length > 0;
        return true;
      });
    }
    return missingTargets;
  }, [scope, rows, missingTargets, isDescribe, isKeyframe]);

  /** 锁定行数（批量自动跳过） */
  const lockedCount = useMemo(() => targets.filter((r) => r.is_locked).length, [targets]);
  /** 前置不满足行数 */
  const noInputCount = useMemo(
    () =>
      targets.filter(
        (r) => !r.is_locked && !(isDescribe ? r.original_dialogue : r.description).trim(),
      ).length,
    [targets, isDescribe],
  );
  const runnableCount = targets.length - lockedCount - noInputCount;

  /** 加载可用模型列表 */
  useEffect(() => {
    setModelsLoading(true);
    listAvailableModels(meta.taskType)
      .then((res) => {
        setAvailableModels(res.items);
        // 默认选中第一个 ready 的模型
        const ready = res.items.find((m) => m.status === 'ready');
        if (ready) setModelId(ready.id);
        else if (res.items.length > 0) setModelId(res.items[0].id);
      })
      .catch(() => showToast('模型列表加载失败', 'error'))
      .finally(() => setModelsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta.taskType]);

  /** 预计耗时（本地估算） */
  const estimatedTime = useMemo(() => {
    if (runnableCount === 0) return null;
    const model = availableModels.find((m) => m.id === modelId);
    const baseSec = model ? (SPEED_BASE_SEC[model.speed_label] ?? 30) : 30;
    const totalSec = baseSec * runnableCount;
    if (totalSec < 60) return `约 ${totalSec} 秒`;
    if (totalSec < 3600) return `约 ${Math.ceil(totalSec / 60)} 分钟`;
    return `约 ${(totalSec / 3600).toFixed(1)} 小时`;
  }, [availableModels, modelId, runnableCount]);

  /** 执行批量工序 */
  const handleRun = () => {
    if (!currentProject || runnableCount === 0 || phase === 'running') return;
    setPhase('running');
    setProcessed(0);
    const onProgress = (done: number) => setProcessed(done);
    const config: ModelConfig = {
      dialog_model: modelId || undefined,
      paint_model: modelId || undefined,
      resolution: isKeyframe ? resolution : undefined,
    };
    const call = (() => {
      switch (kind) {
        case 'describe':         return batchDescribe(targets, currentProject.id, onProgress, config);
        case 'keyframe':         return batchKeyframes(targets, currentProject.id, onProgress, config);
        case 'story_narrative':  return batchStoryNarrative(targets, currentProject.id, onProgress, config);
        case 'story_keyframe':   return batchStoryKeyframe(targets, currentProject.id, onProgress, config);
        case 'video_narrative':  return batchVideoNarrative(targets, currentProject.id, onProgress, config);
        default:                 return batchDescribe(targets, currentProject.id, onProgress, config);
      }
    })();
    call
      .then((res) => {
        setResult(res);
        setPhase('done');
        const parts = [`成功 ${res.done}`];
        if (res.skipped) parts.push(`跳过 ${res.skipped}`);
        if (res.failed) parts.push(`失败 ${res.failed}`);
        if (res.firstError) parts.push(`原因：${res.firstError}`);
        showToast(`${meta.title}完成：${parts.join(' · ')}`, res.failed ? 'warning' : 'success');
      })
      .catch(() => {
        setPhase('confirm');
        showToast('批量执行中断，请重试', 'error');
      });
  };

  /** 执行中禁止关闭；提示词叠层打开时屏蔽 ESC/遮罩 */
  const handleClose = () => {
    if (phase === 'running' || promptOpen) return;
    onClose();
  };

  const IconComp = meta.icon;

  return (
    <>
      <Modal
        title={
          <span className="flex items-center gap-2">
            <IconComp size={16} style={{ color: 'var(--color-primary)' }} />
            {meta.title}
          </span>
        }
        onClose={handleClose}
        maskClosable={phase !== 'running'}
        width={560}
        footer={
          phase === 'confirm' ? (
            <>
              <button type="button" className="btn btn-ghost" onClick={onClose}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={runnableCount === 0}
                title={runnableCount === 0 ? '无可执行行' : `逐行处理 ${targets.length} 行`}
                onClick={handleRun}
              >
                确认生成（{runnableCount} 行）
              </button>
            </>
          ) : phase === 'done' ? (
            <button type="button" className="btn btn-primary" onClick={onClose}>
              完成
            </button>
          ) : undefined
        }
      >
        <div className="flex flex-col gap-4">
          {/* ① 范围选择 */}
          <div className="manga-batch-scope">
            <label className="manga-form-label">生成范围</label>
            <select
              className="manga-select"
              value={scope}
              onChange={(e) => setScope(e.target.value as 'all' | 'missing')}
              disabled={phase === 'running'}
            >
              <option value="missing">缺失生成（仅处理未完成的 {missingTargets.length} 行）</option>
              <option value="all">全部生成（重新处理所有符合条件的行）</option>
            </select>
          </div>

          {/* ② 统计卡片 */}
          <div className="manga-batch-stats">
            <div className="manga-batch-stat">
              <span className="manga-batch-stat-num">{targets.length}</span>
              <span className="manga-batch-stat-label">{isDescribe ? '缺描述词' : '缺分镜图'}</span>
            </div>
            <div className="manga-batch-stat">
              <span className="manga-batch-stat-num">{lockedCount}</span>
              <span className="manga-batch-stat-label">锁定跳过</span>
            </div>
            <div className="manga-batch-stat">
              <span className="manga-batch-stat-num">{noInputCount}</span>
              <span className="manga-batch-stat-label">{isDescribe ? '无台词跳过' : '无描述跳过'}</span>
            </div>
            <div className="manga-batch-stat">
              <span className="manga-batch-stat-num" style={{ color: 'var(--color-primary-300)' }}>
                {runnableCount}
              </span>
              <span className="manga-batch-stat-label">实际执行</span>
            </div>
          </div>

          {targets.length === 0 && (
            <p className="manga-form-tip" style={{ margin: 0, color: 'var(--color-success)' }}>
              当前工序全部完成，无需处理
            </p>
          )}

          {/* ③ 模型选择 */}
          <div className="manga-batch-model">
            <label className="manga-form-label">
              {meta.taskType === 'dialog' ? '推理模型' : meta.taskType === 'paint' ? '生图模型' : '视频模型'}
            </label>
            {modelsLoading ? (
              <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
                <Loader2 size={12} style={{ animation: 'spin 1s linear infinite', verticalAlign: -2 }} /> 加载模型列表…
              </span>
            ) : availableModels.length === 0 ? (
              <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
                暂无可用模型，将使用系统默认
              </span>
            ) : (
              <div className="manga-model-list">
                {availableModels.map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    className={`manga-model-card${modelId === m.id ? ' active' : ''}${m.status !== 'ready' ? ' disabled' : ''}`}
                    onClick={() => m.status === 'ready' && setModelId(m.id)}
                    disabled={m.status !== 'ready'}
                    title={m.notes || m.name}
                  >
                    <span className="model-name">{m.name}</span>
                    <span className={`model-status ${m.status}`}>
                      {MODEL_AVAILABLE_STATUS_LABELS[m.status] ?? m.status}
                    </span>
                    <span className="model-meta">{m.vram_gb}GB · {m.speed_label}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* ④ 分辨率（生图任务专属） */}
          {isKeyframe && (
            <div className="manga-batch-res">
              <label className="manga-form-label">分辨率</label>
              <div className="manga-seg">
                {['2560x1440', '1024x1024', '1024x576', '576x1024'].map((r) => (
                  <button
                    key={r}
                    type="button"
                    className={resolution === r ? 'active' : ''}
                    onClick={() => setResolution(r)}
                    disabled={phase === 'running'}
                  >
                    {r}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* ⑤ 预计耗时 */}
          {estimatedTime && phase === 'confirm' && (
            <div className="manga-batch-estimate">
              <Clock size={14} style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }} />
              <span className="estimate-label">预计耗时</span>
              <span className="estimate-value">{estimatedTime}</span>
            </div>
          )}

          {/* ⑥ 生词：提示词模式 + 设置入口 */}
          {isDescribe && phase === 'confirm' && (
            <div className="manga-batch-prompt">
              <span className="ellipsis" style={{ flex: 1 }} title={promptCfg.mode === 'custom' ? promptCfg.custom : undefined}>
                提示词：{promptCfg.mode === 'custom' && promptCfg.custom.trim()
                  ? `自定义「${promptCfg.custom.trim().slice(0, 40)}${promptCfg.custom.trim().length > 40 ? '…' : ''}」`
                  : '默认（后端内置）'}
              </span>
              <button
                type="button"
                className="manga-taskbar-action"
                style={{ flexShrink: 0 }}
                onClick={() => setPromptOpen(true)}
              >
                <Settings2 size={12} style={{ marginRight: 2, verticalAlign: -2 }} />
                提示词设置
              </button>
            </div>
          )}

          {/* ⑦ 执行进度 */}
          {phase === 'running' && (
            <div className="manga-batch-progress">
              <div className="manga-video-prog" style={{ flex: 1 }}>
                <span
                  className="manga-video-prog-bar"
                  style={{ width: targets.length ? `${Math.round((processed / targets.length) * 100)}%` : '0%' }}
                />
                <span className="manga-video-prog-text">
                  {processed}/{targets.length}
                </span>
              </div>
              <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', flexShrink: 0 }}>
                逐行处理中，请勿关闭…
              </span>
            </div>
          )}

          {/* ⑧ 结果汇总 */}
          {phase === 'done' && result && (
            <div className="manga-batch-result">
              <span className="badge success">成功 {result.done}</span>
              {result.skipped > 0 && <span className="badge warning">跳过 {result.skipped}</span>}
              {result.failed > 0 && <span className="badge error">失败 {result.failed}</span>}
              {result.firstError && (
                <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', flexBasis: '100%', marginTop: 4 }}>
                  原因：{result.firstError}
                </span>
              )}
            </div>
          )}
        </div>
      </Modal>

      {/* 提示词设置叠层 */}
      {promptOpen && (
        <PromptModal
          onClose={() => {
            setPromptOpen(false);
            setPromptCfg(readPromptCfg());
          }}
        />
      )}
    </>
  );
}
