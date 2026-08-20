/* ==========================================================================
 * RecordsModal.tsx —— 漫剧编辑器·视频生成记录弹窗（竞品对齐）
 * --------------------------------------------------------------------------
 * 数据源：GET /manga/video/tasks?project_id=（JOIN storyboard_rows 取镜号，
 * created_at 倒序）。列：镜号 | 状态 | 进度/耗时 | 模型 | 创建时间 | 操作。
 *   - 操作：done → 下载 MP4（/manga/video/{taskId}/download）；
 *           error → 重试（按 row_id 找当前行重新发起 /manga/video/generate）
 *   - 状态过滤页签：全部 / 已完成 / 失败
 *   - 存在在途任务（pending/generating）时 3s 自动轮询刷新
 * ========================================================================== */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Download, History, PlayCircle, RefreshCw } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import { getVideoDownloadUrl, listVideoTasks } from '@/services/mangaApi';
import type { VideoTaskRecord } from '@/services/mangaApi';
import { VIDEO_STATUS_LABELS, type VideoTaskStatus } from '@/constants/statusLabels';
import { getErrorMessage } from '@/utils/errors';
import { Modal } from '../../common/Modal';

/** 状态过滤页签（label 复用视频状态文案真源） */
const FILTERS: { key: string; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'done', label: VIDEO_STATUS_LABELS.done },
  { key: 'error', label: VIDEO_STATUS_LABELS.error },
];

/** 状态徽标 */
const STATUS_BADGE: Record<string, string> = {
  done: 'success',
  error: 'error',
  pending: 'warning',
  generating: 'warning',
};

/** 在途任务自动刷新间隔 */
const ACTIVE_POLL_INTERVAL = 3000;

/** 格式化时间戳（秒/毫秒兼容） */
function formatTime(ts?: number) {
  if (!ts) return '—';
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

/** 格式化耗时（ms → 分秒） */
function formatDuration(ms: number) {
  if (!ms || ms <= 0) return '—';
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec} 秒`;
  return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
}

export default function RecordsModal({ onClose }: { onClose: () => void }) {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const generateVideo = useMangaStore((s) => s.generateVideo);
  const rows = useMangaStore((s) => s.rows);

  const [items, setItems] = useState<VideoTaskRecord[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [filter, setFilter] = useState('all');
  /** 重试按钮忙碌（task_id） */
  const [busyId, setBusyId] = useState('');

  const load = useCallback(async () => {
    if (!currentProject) return;
    try {
      const res = await listVideoTasks(currentProject.id);
      setItems(res.items);
      setLoaded(true);
    } catch (err: unknown) {
      showToast(getErrorMessage(err, '视频记录加载失败'), 'error');
      setLoaded(true);
    }
  }, [currentProject, showToast]);

  // 打开即加载
  useEffect(() => {
    void load();
  }, [load]);

  // 存在在途任务时自动轮询（终态后停止）
  const hasActive = items.some((i) => i.status === 'pending' || i.status === 'generating');
  useEffect(() => {
    if (!hasActive) return;
    const timer = setInterval(() => void load(), ACTIVE_POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [hasActive, load]);

  const filtered = useMemo(
    () => (filter === 'all' ? items : items.filter((i) => i.status === filter)),
    [items, filter],
  );

  /** 失败重试：按 row_id 找当前行重新发起生成（新任务独立条目，不改动历史记录） */
  const handleRetry = (task: VideoTaskRecord) => {
    const row = rows.find((r) => r.id === task.row_id);
    if (!row) {
      showToast('对应分镜行已删除，无法重试', 'warning');
      return;
    }
    setBusyId(task.task_id);
    generateVideo(row)
      .then((ok) => {
        if (ok) {
          showToast(`镜 ${row.shot_number} 已重新发起视频生成`, 'success');
          void load();
        }
      })
      .finally(() => setBusyId(''));
  };

  return (
    <Modal
      title={
        <span className="flex items-center gap-2">
          <History size={16} style={{ color: 'var(--color-primary)' }} />
          视频生成记录
          <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', fontWeight: 400 }}>
            共 {items.length} 条
          </span>
        </span>
      }
      onClose={onClose}
      width={680}
    >
      <div className="flex flex-col gap-3">
        {/* 过滤页签 + 刷新 */}
        <div className="flex items-center gap-2">
          <div className="seg" role="tablist" aria-label="记录状态过滤" style={{ flex: 1 }}>
            {FILTERS.map((f) => (
              <button
                key={f.key}
                type="button"
                role="tab"
                aria-selected={filter === f.key}
                className={`seg-item${filter === f.key ? ' active' : ''}`}
                style={{ flex: 1 }}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="btn-icon"
            style={{ width: 28, height: 28 }}
            title="刷新记录"
            aria-label="刷新记录"
            onClick={() => void load()}
          >
            <RefreshCw size={13} />
          </button>
        </div>

        {/* 记录表 */}
        {!loaded ? (
          <div className="loading-block" style={{ height: 140 }}>
            <span className="spinner" />
            记录加载中…
          </div>
        ) : filtered.length === 0 ? (
          <div className="manga-dock-empty" style={{ padding: 'var(--space-8) var(--space-3)' }}>
            {items.length === 0 ? '暂无视频生成记录，在分镜表视频列发起生成' : '该状态下暂无记录'}
          </div>
        ) : (
          <div className="manga-records">
            <div className="manga-records-tr manga-records-head">
              <div className="manga-records-td c-shot">镜号</div>
              <div className="manga-records-td c-status">状态</div>
              <div className="manga-records-td c-cost">耗时</div>
              <div className="manga-records-td c-model">模型</div>
              <div className="manga-records-td c-time">创建时间</div>
              <div className="manga-records-td c-ops">操作</div>
            </div>
            {filtered.map((t) => (
              <div key={t.task_id} className="manga-records-tr">
                <div className="manga-records-td c-shot">
                  <span className="manga-idx-badge">{t.shot_number}</span>
                </div>
                <div className="manga-records-td c-status">
                  <span className={`badge ${STATUS_BADGE[t.status] ?? 'primary'}`}>
                    {VIDEO_STATUS_LABELS[t.status as VideoTaskStatus] ?? t.status}
                  </span>
                  {(t.status === 'pending' || t.status === 'generating') && (
                    <span className="text-tertiary" style={{ fontSize: 10, marginLeft: 4 }}>
                      {Math.round(t.progress * 100)}%
                    </span>
                  )}
                </div>
                <div className="manga-records-td c-cost text-secondary">{formatDuration(t.generation_time_ms)}</div>
                <div className="manga-records-td c-model text-tertiary ellipsis" title={t.model_used || undefined}>
                  {t.model_used || '—'}
                </div>
                <div className="manga-records-td c-time text-tertiary">{formatTime(t.created_at)}</div>
                <div className="manga-records-td c-ops">
                  {t.status === 'done' && (
                    <a
                      className="manga-records-link"
                      href={getVideoDownloadUrl(t.task_id)}
                      download
                      title="下载 MP4"
                    >
                      <Download size={12} />
                      下载
                    </a>
                  )}
                  {t.status === 'error' && (
                    <button
                      type="button"
                      className="manga-row-tool"
                      title="重新发起视频生成"
                      aria-label={`镜 ${t.shot_number} 重试`}
                      disabled={busyId !== ''}
                      onClick={() => handleRetry(t)}
                    >
                      {busyId === t.task_id ? <span className="spinner manga-mini-spin" /> : <PlayCircle size={14} />}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </Modal>
  );
}
