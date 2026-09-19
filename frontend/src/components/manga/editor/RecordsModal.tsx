/* ==========================================================================
 * RecordsModal.tsx —— 漫剧编辑器·生成记录弹窗（视频 + 图片双页签）
 * --------------------------------------------------------------------------
 * 媒体页签（2026-08-24 用户裁定：不仅视频，也要图片生成记录，具体详细）：
 *   - 视频：GET /manga/video/tasks（JOIN storyboard_rows 取镜号，倒序）。
 *     列：镜号|状态|耗时|模型|创建时间|操作（下载/重试）；
 *     状态过滤页签 全部/已完成/失败；在途任务 3s 自动轮询。
 *   - 图片：GET /comic/image/tasks（资产图 meta.history 留痕 + 分镜
 *     关键帧 keyframes 表，created_at 倒序）。列：缩略图|类型|名称/镜号|
 *     引擎/模型|seed|尺寸|时间|操作（下载）。缩略图点击开灯箱大图；
 *     类别过滤页签 全部/资产图/分镜关键帧。
 * ========================================================================== */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Download, History, ImageIcon, PlayCircle, RefreshCw, Trash2, Video } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import {
  deleteVideoHistory,
  getMediaUrl,
  getVideoDownloadUrl,
  listImageTasks,
  listVideoTasks,
  type ImageTaskRecord,
} from '@/services/mangaApi';
import { VIDEO_STATUS_LABELS, type VideoTaskStatus } from '@/constants/statusLabels';
import { getErrorMessage } from '@/utils/errors';
import { Modal } from '../../common/Modal';
import AssetLightbox from './AssetLightbox';

/** 媒体页签：视频 / 图片 */
const MEDIA_TABS: { key: 'video' | 'image'; label: string; icon: typeof Video }[] = [
  { key: 'video', label: '视频', icon: Video },
  { key: 'image', label: '图片', icon: ImageIcon },
];

/** 视频状态过滤页签（label 复用视频状态文案真源） */
const VIDEO_FILTERS: { key: string; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'done', label: VIDEO_STATUS_LABELS.done },
  { key: 'error', label: VIDEO_STATUS_LABELS.error },
];

/** 图片类别过滤页签 */
const IMAGE_FILTERS: { key: string; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'asset', label: '资产图' },
  { key: 'keyframe', label: '分镜关键帧' },
];

/** 状态徽标 */
const STATUS_BADGE: Record<string, string> = {
  done: 'success',
  error: 'error',
  pending: 'warning',
  generating: 'warning',
};

/** 资产类型徽标文案 */
const KIND_LABELS: Record<string, string> = {
  character: '角色',
  scene: '场景',
  prop: '道具',
};

/** 资产生成动作文案 */
const ACTION_LABELS: Record<string, string> = {
  generate: '首次生成',
  regenerate: '重新生成',
  view: '单视图',
};

/** 视图名文案（view 动作） */
const VIEW_LABELS: Record<string, string> = {
  front: '正面',
  side: '侧面',
  back: '背面',
  closeup: '特写',
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

/** 格式化尺寸（宽高显示，缺失返回 —） */
function formatSize(w?: number | null, h?: number | null) {
  return w && h ? `${w}×${h}` : '—';
}

export default function RecordsModal({ onClose }: { onClose: () => void }) {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const generateVideo = useMangaStore((s) => s.generateVideo);
  const rows = useMangaStore((s) => s.rows);

  /** 媒体页签（视频/图片） */
  const [media, setMedia] = useState<'video' | 'image'>('video');
  /** 视频记录 */
  const [videoItems, setVideoItems] = useState<Awaited<ReturnType<typeof listVideoTasks>>['items']>([]);
  /** 图片记录 */
  const [imageItems, setImageItems] = useState<ImageTaskRecord[]>([]);
  const [loaded, setLoaded] = useState(false);
  /** 过滤页签（视频状态 / 图片类别） */
  const [videoFilter, setVideoFilter] = useState('all');
  const [imageFilter, setImageFilter] = useState('all');
  /** 重试按钮忙碌（task_id） */
  const [busyId, setBusyId] = useState('');
  /** 灯箱（null 关闭；srcs 为点击时快照，idx 为 srcs 索引） */
  const [lightbox, setLightbox] = useState<{ srcs: string[]; idx: number } | null>(null);

  const load = useCallback(async () => {
    if (!currentProject) return;
    try {
      if (media === 'video') {
        const res = await listVideoTasks(currentProject.id);
        setVideoItems(res.items);
      } else {
        const res = await listImageTasks(currentProject.id, {
          category: imageFilter as 'all' | 'asset' | 'keyframe',
        });
        setImageItems(res.items);
      }
      setLoaded(true);
    } catch (err: unknown) {
      showToast(getErrorMessage(err, '记录加载失败'), 'error');
      setLoaded(true);
    }
  }, [currentProject, media, imageFilter, showToast]);

  // 打开/切页签/换过滤即加载
  useEffect(() => {
    setLoaded(false);
    void load();
  }, [load]);

  // 视频在途任务自动轮询（终态后停止）
  const hasActive = videoItems.some((i) => i.status === 'pending' || i.status === 'generating');
  useEffect(() => {
    if (media !== 'video' || !hasActive) return;
    const timer = setInterval(() => void load(), ACTIVE_POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [media, hasActive, load]);

  const filteredVideo = useMemo(
    () => (videoFilter === 'all' ? videoItems : videoItems.filter((i) => i.status === videoFilter)),
    [videoItems, videoFilter],
  );

  /** 失败重试：按 row_id 找当前行重新发起生成（新任务独立条目，不改动历史记录） */
  const handleRetry = (task: Awaited<ReturnType<typeof listVideoTasks>>['items'][number]) => {
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

  /** 删除视频记录（2026-08-31 删除机制补全：DB 行 + MP4 文件级联，不可恢复） */
  const handleDeleteVideo = (task: Awaited<ReturnType<typeof listVideoTasks>>['items'][number]) => {
    if (!window.confirm(`确定删除镜 ${task.shot_number} 的视频记录？视频文件将一并删除，此操作不可恢复`)) return;
    setBusyId(task.task_id);
    deleteVideoHistory(task.task_id)
      .then(() => {
        showToast('视频记录已删除', 'success');
        void load();
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '视频记录删除失败'), 'error'))
      .finally(() => setBusyId(''));
  };

  /** 图片记录行类型徽标文案（角色/场景/道具 + 动作；关键帧=镜号+版本） */
  const imageTypeLabel = (t: ImageTaskRecord): string =>
    t.category === 'keyframe'
      ? '关键帧'
      : `${KIND_LABELS[t.kind ?? ''] ?? t.kind}·${ACTION_LABELS[t.action ?? ''] ?? t.action}`;

  /** 记录行媒体 url（路径逐段 encodeURIComponent——中文资产目录必需；
   *  created_at 作缓存破除参数，覆盖写同路径时不命中旧图） */
  const recordUrl = (t: ImageTaskRecord): string =>
    t.file_path ? getMediaUrl(t.file_path, t.created_at) : '';

  return (
    <Modal
      title={
        <span className="flex items-center gap-2">
          <History size={16} style={{ color: 'var(--color-primary)' }} />
          生成记录
          <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', fontWeight: 400 }}>
            {media === 'video'
              ? `视频共 ${videoItems.length} 条`
              : `图片共 ${imageItems.length} 条`}
          </span>
        </span>
      }
      onClose={onClose}
      width={760}
    >
      <div className="flex flex-col gap-3">
        {/* 媒体页签：视频 / 图片（胶囊滑块控件，data-active 驱动指示块位移） */}
        <div className="manga-media-seg" role="tablist" aria-label="记录媒体类型" data-active={media}>
          {MEDIA_TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button
                key={t.key}
                type="button"
                role="tab"
                aria-selected={media === t.key}
                className={`manga-media-seg-item${media === t.key ? ' active' : ''}`}
                onClick={() => setMedia(t.key)}
              >
                <Icon size={12} />
                {t.label}
              </button>
            );
          })}
        </div>

        {/* 过滤页签 + 刷新 */}
        <div className="flex items-center gap-2">
          <div className="seg" role="tablist" aria-label={media === 'video' ? '视频状态过滤' : '图片类别过滤'} style={{ flex: 1 }}>
            {(media === 'video' ? VIDEO_FILTERS : IMAGE_FILTERS).map((f) => {
              const active = media === 'video' ? videoFilter === f.key : imageFilter === f.key;
              const onClick = () => (media === 'video' ? setVideoFilter(f.key) : setImageFilter(f.key));
              return (
                <button
                  key={f.key}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  className={`seg-item${active ? ' active' : ''}`}
                  style={{ flex: 1 }}
                  onClick={onClick}
                >
                  {f.label}
                </button>
              );
            })}
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
        ) : media === 'video' ? (
          /* ── 视频记录表 ── */
          filteredVideo.length === 0 ? (
            <div className="manga-dock-empty" style={{ padding: 'var(--space-8) var(--space-3)' }}>
              {videoItems.length === 0 ? '暂无视频生成记录，在分镜表视频列发起生成' : '该状态下暂无记录'}
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
              {filteredVideo.map((t) => (
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
                    {/* 终态任务可删（运行中后端拒绝）；删除=DB记录+MP4级联清理 */}
                    {t.status !== 'pending' && t.status !== 'generating' && (
                      <button
                        type="button"
                        className="manga-row-tool"
                        title="删除该记录及视频文件（不可恢复）"
                        aria-label={`镜 ${t.shot_number} 删除视频记录`}
                        disabled={busyId !== ''}
                        onClick={() => handleDeleteVideo(t)}
                      >
                        {busyId === t.task_id ? <span className="spinner manga-mini-spin" /> : <Trash2 size={14} />}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )
        ) : (
          /* ── 图片记录表（资产图 + 分镜关键帧） ── */
          imageItems.length === 0 ? (
            <div className="manga-dock-empty" style={{ padding: 'var(--space-8) var(--space-3)' }}>
              暂无图片生成记录，在资产面板「AI 生图」或分镜表图片列发起生成
            </div>
          ) : (
            <div className="manga-records manga-records-img">
              <div className="manga-records-tr manga-records-head">
                <div className="manga-records-td i-thumb">预览</div>
                <div className="manga-records-td i-type">类型</div>
                <div className="manga-records-td i-name">名称 / 镜号</div>
                <div className="manga-records-td i-engine">引擎 / 模型</div>
                <div className="manga-records-td i-seed">seed / 尺寸</div>
                <div className="manga-records-td c-cost">耗时</div>
                <div className="manga-records-td c-time">时间</div>
                <div className="manga-records-td c-ops">操作</div>
              </div>
              {imageItems.map((t) => {
                const url = recordUrl(t);
                return (
                <div key={t.record_id} className="manga-records-tr">
                  <div className="manga-records-td i-thumb">
                    {url ? (
                      <button
                        type="button"
                        className="manga-records-thumb"
                        title="点击放大预览"
                        aria-label={`预览 ${t.name ?? `镜 ${t.shot_number ?? ''}`}`}
                        onClick={() => setLightbox({ srcs: [url], idx: 0 })}
                      >
                        <img src={url} alt={t.name ?? `镜 ${t.shot_number}`} loading="lazy" />
                      </button>
                    ) : (
                      <span className="text-tertiary">—</span>
                    )}
                  </div>
                  <div className="manga-records-td i-type">
                    <span className={`badge ${t.category === 'keyframe' ? 'primary' : 'success'}`}>
                      {imageTypeLabel(t)}
                    </span>
                  </div>
                  <div className="manga-records-td i-name">
                    {t.category === 'keyframe' ? (
                      <>
                        <span className="manga-idx-badge">{t.shot_number || '—'}</span>
                        <span className="text-tertiary" style={{ fontSize: 10 }}>
                          v{t.version}
                          {t.is_current ? ' · 当前' : ''}
                        </span>
                      </>
                    ) : (
                      <>
                        <span className="text-secondary ellipsis" title={t.view ? `${t.name}（${VIEW_LABELS[t.view] ?? t.view}）` : t.name}>
                          {t.name || '—'}
                        </span>
                        {t.view && (
                          <span className="text-tertiary" style={{ fontSize: 10 }}>
                            {VIEW_LABELS[t.view] ?? t.view}
                          </span>
                        )}
                      </>
                    )}
                  </div>
                  <div className="manga-records-td i-engine text-tertiary ellipsis" title={t.model ? `${t.engine} / ${t.model}` : t.model}>
                    {t.category === 'keyframe' ? (
                      <span title={t.prompt || undefined} className="ellipsis">
                        {t.status === 'error' ? '生成失败' : (t.prompt || '—')}
                      </span>
                    ) : (
                      `${t.engine || '—'} / ${t.model || '—'}`
                    )}
                  </div>
                  <div className="manga-records-td i-seed text-tertiary" style={{ fontFamily: 'var(--font-family-mono)', fontSize: 10 }}>
                    {t.category === 'asset' ? (
                      <>
                        <div>{t.seed != null ? t.seed : '—'}</div>
                        <div>{formatSize(t.width, t.height)}</div>
                      </>
                    ) : (
                      '—'
                    )}
                  </div>
                  <div className="manga-records-td c-cost text-secondary">
                    {formatDuration(t.elapsed_ms ?? 0)}
                  </div>
                  <div className="manga-records-td c-time text-tertiary">{formatTime(t.created_at)}</div>
                  <div className="manga-records-td c-ops">
                    {url && (
                      <a
                        className="manga-records-link"
                        href={url}
                        download
                        title="下载图片"
                      >
                        <Download size={12} />
                        下载
                      </a>
                    )}
                  </div>
                </div>
                );
              })}
            </div>
          )
        )}
      </div>

      {/* 灯箱大图预览 */}
      {lightbox && (
        <AssetLightbox
          srcs={lightbox.srcs}
          initialIndex={lightbox.idx}
          onClose={() => setLightbox(null)}
        />
      )}
    </Modal>
  );
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
