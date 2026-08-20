/* ==========================================================================
 * VideoConfirmModal.tsx —— 漫剧编辑器·生成视频确认弹窗（竞品 yl.man-tui.com 对齐）
 * --------------------------------------------------------------------------
 * 竞品确认层内容对齐：处理任务 N 条视频 / 视频模型选择 / 画幅 / 时长 /
 *   取消 / 确认生成。
 *
 * 参数换算（竞品视频引擎约束，注释说明）：
 *   - 时长 → 帧数：按 16fps 且帧数 ≡ 1 (mod 8)：
 *       4s = 65 帧、8s = 129 帧、11s = 177 帧、15s = 241 帧
 *   - 画幅 → 分辨率：宽高均须被 32 整除：
 *       横屏 16:9 → 1024×576；竖屏 9:16 → 576×1024
 *
 * 现状与降级说明（如实标注）：
 *   useMangaStore.generateVideo(row) 当前签名仅接收分镜行，payload 由 store
 *   内部构造（storyboard_row_id / description / screenshot_4in1），尚不支持
 *   自定义模型/分辨率/帧数透传。本弹窗的模型/画幅/时长选择经 ModelConfigModal
 *   同源 localStorage 持久化为默认配置，待生成管线开放参数后直接生效；
 *   弹窗底部固定展示「当前视频引擎将按自身约束对齐分辨率与帧数」。
 *
 * 行筛选：由调用方（MangaWorkspace 工序⑤）统计缺视频的分镜行传入 rowIds，
 *   本组件内部不再自行筛选，仅做 id → 行映射。
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { Clapperboard, Loader2 } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import { listAvailableModels } from '@/services/mangaApi';
import { MODEL_AVAILABLE_STATUS_LABELS } from '@/constants/statusLabels';
import type { AvailableModel } from '@/types';
import { Modal } from '../../common/Modal';
import { VIDEO_DURATION_OPTIONS, loadModelConfig } from '@/constants/modelConfig';

/** 时长（秒）→ 帧数：按 16fps 且帧数 ≡ 1 (mod 8) */
const DURATION_TO_FRAMES: Record<number, number> = { 4: 65, 8: 129, 11: 177, 15: 241 };

/** 画幅 → 分辨率：宽高均须被 32 整除 */
const ASPECT_TO_RESOLUTION: Record<'16:9' | '9:16', { width: number; height: number }> = {
  '16:9': { width: 1024, height: 576 },
  '9:16': { width: 576, height: 1024 },
};

export interface VideoConfirmModalProps {
  /** 是否打开 */
  open: boolean;
  /** 待生成分镜行 ID（调用方已筛选缺视频行） */
  rowIds: string[];
  onClose: () => void;
}

export default function VideoConfirmModal({ open, rowIds, onClose }: VideoConfirmModalProps) {
  const showToast = useAppStore((s) => s.showToast);
  const rows = useMangaStore((s) => s.rows);
  const generateVideo = useMangaStore((s) => s.generateVideo);

  const [videoModel, setVideoModel] = useState('');
  const [aspect, setAspect] = useState<'16:9' | '9:16'>('16:9');
  const [duration, setDuration] = useState(4);
  const [models, setModels] = useState<AvailableModel[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // 每次打开：回填模型配置默认值（loadModelConfig）+ 拉取视频模型列表
  useEffect(() => {
    if (!open) return;
    const cfg = loadModelConfig();
    setVideoModel(cfg.videoModel);
    setAspect(cfg.aspect);
    setDuration(cfg.duration);
    setSubmitting(false);
    let alive = true;
    setModelsLoading(true);
    listAvailableModels('video')
      .then((res) => {
        if (alive) setModels(res.items);
      })
      .catch(() => {
        if (alive) showToast('模型列表加载失败', 'error');
      })
      .finally(() => {
        if (alive) setModelsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, showToast]);

  if (!open) return null;

  // 待处理行：rowIds 已由调用方筛选，此处仅做 id → 行映射（保持分镜行序）
  const targetRows = rows.filter((r) => rowIds.includes(r.id));
  // 参数换算结果（本地预览；实际对齐由视频引擎约束决定，见底部降级说明）
  const frames = DURATION_TO_FRAMES[duration] ?? duration * 16 + 1;
  const resolution = ASPECT_TO_RESOLUTION[aspect];
  // 已保存的视频模型当前不在列表 → 追加占位项，如实显示已保存值
  const savedMissing = videoModel !== '' && !models.some((m) => m.id === videoModel);

  /** 确认生成：逐行调 store.generateVideo（自带功能锁/任务注册/轮询/失败 toast） */
  const handleConfirm = () => {
    if (submitting || targetRows.length === 0) return;
    setSubmitting(true);
    void (async () => {
      let submitted = 0;
      for (const row of targetRows) {
        // 白名单静默（三分法第 4 条）：失败已由 store 内部 toast 透出，
        // 此 catch 仅把 rejection 收敛成 false 供计数，不得二次呈现
        const ok = await generateVideo(row).catch(() => false);
        if (ok) submitted += 1;
      }
      setSubmitting(false);
      if (submitted > 0) {
        showToast(`已提交 ${submitted} 条视频生成任务`, 'success');
        onClose();
      }
      // submitted === 0：功能锁被其他模块占用等，保留弹窗供用户稍后重试
    })();
  };

  /** 提交中禁止关闭（遮罩/ESC/取消键同步屏蔽） */
  const handleClose = () => {
    if (submitting) return;
    onClose();
  };

  return (
    <Modal
      title={
        <span className="flex items-center gap-2">
          <Clapperboard size={16} style={{ color: 'var(--color-primary)' }} />
          生成视频确认
        </span>
      }
      onClose={handleClose}
      maskClosable={!submitting}
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={handleClose} disabled={submitting}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={submitting || rowIds.length === 0}
            title={rowIds.length === 0 ? '无待生成分镜行' : `逐行提交 ${rowIds.length} 条视频生成任务`}
            onClick={handleConfirm}
          >
            {submitting ? '提交中…' : `确认生成（${rowIds.length} 条）`}
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {/* ① 处理任务统计 */}
        <div className="manga-video-summary">
          处理任务 <strong>{rowIds.length}</strong> 条视频
          <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
            （逐行提交，进度见顶部任务条）
          </span>
        </div>

        {/* ② 视频模型选择（默认读模型配置） */}
        <div>
          <label className="manga-form-label">视频模型</label>
          {modelsLoading ? (
            <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
              <Loader2 size={12} style={{ animation: 'spin 1s linear infinite', verticalAlign: -2 }} /> 加载模型列表…
            </span>
          ) : (
            <select
              className="manga-modal-select"
              value={videoModel}
              aria-label="视频模型"
              disabled={submitting}
              onChange={(e) => setVideoModel(e.target.value)}
            >
              <option value="">系统默认（自动选择）</option>
              {models.map((m) => (
                <option key={m.id} value={m.id} disabled={m.status !== 'ready'}>
                  {`${m.name}（${MODEL_AVAILABLE_STATUS_LABELS[m.status]}）`}
                </option>
              ))}
              {savedMissing && (
                <option value={videoModel} disabled>
                  {`${videoModel}（已保存，当前不可用）`}
                </option>
              )}
            </select>
          )}
        </div>

        {/* ③ 画幅 + ④ 时长（默认读模型配置） */}
        <div className="manga-modelcfg-grid">
          <div>
            <label className="manga-form-label">画幅</label>
            <select
              className="manga-modal-select"
              value={aspect}
              aria-label="画幅"
              disabled={submitting}
              onChange={(e) => setAspect(e.target.value === '9:16' ? '9:16' : '16:9')}
            >
              <option value="16:9">横屏 16:9</option>
              <option value="9:16">竖屏 9:16</option>
            </select>
          </div>
          <div>
            <label className="manga-form-label">时长</label>
            <select
              className="manga-modal-select"
              value={String(duration)}
              aria-label="时长"
              disabled={submitting}
              onChange={(e) => setDuration(Number(e.target.value))}
            >
              {VIDEO_DURATION_OPTIONS.map((d) => (
                <option key={d} value={String(d)}>
                  {d} 秒
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* ⑤ 参数换算预览（16fps / 帧数≡1 mod 8 / 宽高被 32 整除） */}
        <p className="manga-form-tip" style={{ margin: 0 }}>
          对应参数：{resolution.width}×{resolution.height} · {frames} 帧（16fps）
        </p>

        {/* ⑥ 降级说明：现有 generateVideo 链路不支持自定义分辨率/帧数透传 */}
        <p className="manga-form-tip" style={{ margin: 0 }}>
          当前视频引擎将按自身约束对齐分辨率与帧数
        </p>
      </div>
    </Modal>
  );
}
