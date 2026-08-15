/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 视频生成抽屉（工作台右侧，四工序产出口）
 * --------------------------------------------------------------------------
 * - 逐镜发起：POST /manga/video/generate（video_gen 功能互斥 + 2s 轮询，
 *   进度在顶部任务条实时展示；降级管线如实标注 BK-014）
 * - 批量关键帧：POST /manga/keyframe/batch（逐行串行，聚合成功/失败明细）
 * - 行状态徽标：generation_status 真实反映
 * ========================================================================== */

import { useCallback, useState } from 'react';
import { Images, PlayCircle } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import * as mangaApi from '@/services/mangaApi';
import type { StoryboardGenerationStatus, StoryboardRow } from '@/types';
import DrawerFrame from './DrawerFrame';

const STATUS_LABELS: Record<StoryboardGenerationStatus, string> = {
  pending: '待生成',
  generating: '生成中',
  done: '已完成',
  error: '失败',
  skipped: '已跳过',
};

const STATUS_BADGE: Record<StoryboardGenerationStatus, string> = {
  pending: 'neutral',
  generating: 'warning',
  done: 'success',
  error: 'error',
  skipped: 'neutral',
};

interface VideoDrawerProps {
  onClose: () => void;
}

export function VideoDrawer({ onClose }: VideoDrawerProps) {
  const showToast = useAppStore((s) => s.showToast);
  const rows = useMangaStore((s) => s.rows);
  const currentProject = useMangaStore((s) => s.currentProject);
  const generateVideo = useMangaStore((s) => s.generateVideo);
  const videoTasks = useMangaStore((s) => s.videoTasks);
  const setSelectedRow = useMangaStore((s) => s.setSelectedRow);
  const selectedRowId = useMangaStore((s) => s.selectedRowId);

  const [batchKfRunning, setBatchKfRunning] = useState(false);

  // 行级生成中判定（有活跃任务即禁用重复发起）
  const rowBusy = useCallback(
    (row: StoryboardRow): boolean =>
      videoTasks.some(
        (t) => t.row_id === row.id && (t.status === 'generating' || t.status === 'pending'),
      ),
    [videoTasks],
  );

  // 批量关键帧（全部行逐行串行）
  const handleBatchKeyframes = useCallback(() => {
    if (!currentProject || rows.length === 0) return;
    setBatchKfRunning(true);
    mangaApi
      .batchGenerateKeyframes(
        rows.map((r) => r.id),
        currentProject.id,
      )
      .then((res) => {
        const store = useMangaStore.getState();
        rows.forEach((r) => store.invalidateKeyframes(r.id));
        if (res.failed.length > 0) {
          showToast(
            `批量关键帧完成：成功 ${res.success_count}/${res.total}，失败 ${res.failed.length}（${res.failed[0].message}）`,
            'warning',
          );
        } else {
          showToast(`批量关键帧完成，成功 ${res.success_count} 镜`, 'success');
        }
      })
      .catch((err) => {
        const msg =
          err && typeof err === 'object' && 'message' in err
            ? (err as { message: string }).message
            : '批量关键帧失败';
        showToast(msg, 'error');
      })
      .finally(() => setBatchKfRunning(false));
  }, [currentProject, rows, showToast]);

  const doneCount = rows.filter((r) => r.generation_status === 'done').length;

  return (
    <DrawerFrame title="视频生成" subtitle={`· ${doneCount}/${rows.length} 已完成`} onClose={onClose}>
      <button
        type="button"
        className="btn btn-secondary btn-sm btn-block mb-3"
        disabled={batchKfRunning || rows.length === 0}
        onClick={handleBatchKeyframes}
        title="为全部分镜行逐行生成关键帧（串行，聚合成功/失败明细）"
      >
        <Images size={13} />
        {batchKfRunning ? '批量生成中…' : '批量生成关键帧'}
      </button>

      <div className="flex flex-col gap-2">
        {[...rows]
          .sort((a, b) => a.shot_number - b.shot_number)
          .map((row) => {
            const busy = rowBusy(row);
            const selected = row.id === selectedRowId;
            return (
              <div
                key={row.id}
                className="card"
                style={{
                  padding: 'var(--space-3)',
                  border: selected
                    ? '1px solid var(--color-primary-200)'
                    : '1px solid var(--color-border-light)',
                }}
              >
                <div className="flex items-center gap-2">
                  <span style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600, color: 'var(--color-primary)', flexShrink: 0 }}>
                    镜 {row.shot_number}
                  </span>
                  <span className={`badge ${STATUS_BADGE[row.generation_status]}`}>
                    {STATUS_LABELS[row.generation_status]}
                  </span>
                  <button
                    type="button"
                    className="btn-icon"
                    style={{ marginLeft: 'auto', width: 24, height: 24 }}
                    title="在分镜表中选中该行"
                    onClick={() => setSelectedRow(row.id)}
                  >
                    ⌖
                  </button>
                </div>
                <div
                  className="ellipsis"
                  title={row.description || row.original_dialogue}
                  style={{ marginTop: 4, fontSize: 'var(--font-size-xs)', color: 'var(--color-text-secondary)' }}
                >
                  {row.description || row.original_dialogue || '（空）'}
                </div>
                <button
                  type="button"
                  className="btn btn-primary btn-sm btn-block mt-2"
                  disabled={busy}
                  onClick={() => void generateVideo(row)}
                  title={busy ? '该分镜视频生成中' : '为该分镜生成视频'}
                >
                  <PlayCircle size={13} />
                  {busy ? '生成中…' : '生成视频'}
                </button>
              </div>
            );
          })}
      </div>
    </DrawerFrame>
  );
}

export default VideoDrawer;
